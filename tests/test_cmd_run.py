"""`cmd_run`'s auto-validation step must not probe boards this run will
never fetch from.

`--sources` scoped to global/aggregator sources only (adzuna, usajobs, ...)
fetches no per-company ATS board at all -- validating every unchecked board
in the registry regardless was pure waste, and it happened on every single
`run` invocation as long as ANY company anywhere was unchecked, however
unrelated to what this run actually touches.
"""

from __future__ import annotations

from typing import Any

from jobscrape import cli, db
from jobscrape.sources import BOARD_SOURCES


def _run_args(**overrides: Any):
    argv = ["run"]
    for key, values in overrides.pop("_repeat", {}).items():
        for v in values:
            argv += [f"--{key.replace('_', '-')}", v]
    args = cli.build_parser().parse_args(argv)
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


async def _stub(*_a: Any, **_kw: Any) -> int:
    return 0


def _sync_stub(*_a: Any, **_kw: Any) -> int:
    """`cmd_export` is the one step `cmd_run` calls without `await`."""
    return 0


def _seed_one_unchecked(conn: db.Connection) -> None:
    db.add_companies(conn, "greenhouse", ["acme"])


async def test_global_only_sources_skip_validation_entirely(
    _isolated_db, monkeypatch,
) -> None:
    conn = _isolated_db
    _seed_one_unchecked(conn)

    calls: list[str] = []

    async def fake_validate(args):
        calls.append("validate")
        return 0

    monkeypatch.setattr(cli, "cmd_discover", _stub)
    monkeypatch.setattr(cli, "cmd_validate", fake_validate)
    monkeypatch.setattr(cli, "cmd_fetch", _stub)
    monkeypatch.setattr(cli, "cmd_export", _sync_stub)

    args = _run_args(_repeat={"sources": ["adzuna"]})
    rc = await cli.cmd_run(args)
    assert rc == 0
    assert calls == [], "a global-only --sources must never trigger validation"


async def test_a_board_source_still_gets_scoped_validation(
    _isolated_db, monkeypatch,
) -> None:
    """The normal, pre-existing case must keep working: `--sources
    greenhouse` scopes validation down to just that ATS, not everything and
    not nothing."""
    conn = _isolated_db
    _seed_one_unchecked(conn)

    seen_ats: list[Any] = []

    async def fake_validate(args):
        seen_ats.append(args.ats)
        return 0

    monkeypatch.setattr(cli, "cmd_discover", _stub)
    monkeypatch.setattr(cli, "cmd_validate", fake_validate)
    monkeypatch.setattr(cli, "cmd_fetch", _stub)
    monkeypatch.setattr(cli, "cmd_export", _sync_stub)

    args = _run_args(_repeat={"sources": ["greenhouse"]})
    rc = await cli.cmd_run(args)
    assert rc == 0
    assert seen_ats == [["greenhouse"]]


async def test_no_sources_restriction_still_validates_unscoped(
    _isolated_db, monkeypatch,
) -> None:
    """No `--sources`/`--ats` at all means the fetch itself is unscoped, so
    validation staying unscoped too is correct, not a regression."""
    conn = _isolated_db
    _seed_one_unchecked(conn)

    seen_ats: list[Any] = []

    async def fake_validate(args):
        seen_ats.append(args.ats)
        return 0

    monkeypatch.setattr(cli, "cmd_discover", _stub)
    monkeypatch.setattr(cli, "cmd_validate", fake_validate)
    monkeypatch.setattr(cli, "cmd_fetch", _stub)
    monkeypatch.setattr(cli, "cmd_export", _sync_stub)

    args = _run_args()
    rc = await cli.cmd_run(args)
    assert rc == 0
    assert seen_ats == [None]
