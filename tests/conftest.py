"""Shared test fixtures.

Every test in this suite now runs against a real `jobscrape_test` Postgres
database (`config.TEST_DATABASE_URL`), not SQLite — keeping SQLite around for
tests would reintroduce exactly the dialect divergence the Postgres migration
exists to end. Running the suite requires Postgres reachable at that URL.

Isolation is one outer transaction per test, held open for the test's
duration and rolled back at teardown — never a real commit. Every write any
code makes during a test — through `db.session()`, `db.connect()`, or a
connection a test uses directly — lands in that one transaction: reads see it
immediately (read-your-own-writes, same as a real commit would give), but
nothing ever actually persists, so no test needs its own cleanup and nothing
leaks into the next test. This works because `db.connect()` is monkeypatched
to always return the same live connection for the duration of a test, with
`.commit()`/`.close()` swallowed — real cleanup happens once, at teardown,
via a rollback on the underlying SQLAlchemy connection those calls never
touch.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from jobscrape import config, db


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    engine = create_engine(config.TEST_DATABASE_URL, poolclass=NullPool, future=True)
    raw_conn = engine.connect()
    raw_conn.begin()

    shared = db.Connection(raw_conn)
    # DDL is transactional in Postgres, so applying the schema inside this
    # same not-yet-committed transaction is visible to everything the test
    # does afterward, and vanishes with everything else at rollback.
    shared.execute(db.SCHEMA)
    for statement in filter(None, (s.strip() for s in db._strip_sql_comments(db.INDEXES).split(";"))):
        shared.execute(statement)
    # `jobscrape_test` is a persistent database, not recreated per run — a
    # column added to `SCHEMA` after that database's `jobs` table already
    # existed needs the same ALTER `ensure_schema` runs against a real
    # deployment. This fixture applies `SCHEMA`/`INDEXES` directly rather
    # than going through `ensure_schema`, so it has to call this too.
    db._migrate(shared)

    monkeypatch.setattr(shared, "commit", lambda: None)
    monkeypatch.setattr(shared, "close", lambda: None)
    monkeypatch.setattr(db, "connect", lambda database_url=None: shared)

    yield shared

    raw_conn.rollback()
    raw_conn.close()
    engine.dispose()
