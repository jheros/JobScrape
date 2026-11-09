"""`sync_endpoints`: the table and the small CRUD layer the Endpoints page and
`endpoint_sync` share.

The token column is the one thing with a rule beyond plain CRUD: it is
write-only from the dashboard's point of view, so every read path the page
uses must come back WITHOUT it, and only the sender's own read includes it.
"""

from __future__ import annotations

from jobscrape import db


def test_create_and_list_never_return_the_token(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = db.create_endpoint(conn, "site", "https://example.com/import", "s3cret")
    assert endpoint_id

    rows = db.list_endpoints(conn)
    assert [r["name"] for r in rows] == ["site"]
    assert "token" not in rows[0]
    assert rows[0]["has_token"] is True
    assert rows[0]["enabled"] is True

    row = db.get_endpoint(conn, endpoint_id)
    assert "token" not in row
    assert db.get_endpoint(conn, endpoint_id, with_token=True)["token"] == "s3cret"


def test_create_refuses_a_duplicate_name(_isolated_db) -> None:
    conn = _isolated_db
    assert db.create_endpoint(conn, "site", "https://a.example/x", "t")
    assert db.create_endpoint(conn, "site", "https://b.example/x", "t") is None
    assert len(db.list_endpoints(conn)) == 1


def test_update_with_blank_token_keeps_the_current_one(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = db.create_endpoint(conn, "site", "https://a.example/x", "orig")
    assert db.update_endpoint(conn, endpoint_id, name="renamed",
                              url="https://b.example/y", token=None)
    row = db.get_endpoint(conn, endpoint_id, with_token=True)
    assert (row["name"], row["url"], row["token"]) == ("renamed", "https://b.example/y", "orig")

    assert db.update_endpoint(conn, endpoint_id, token="new")
    assert db.get_endpoint(conn, endpoint_id, with_token=True)["token"] == "new"


def test_update_refuses_a_name_another_endpoint_has(_isolated_db) -> None:
    conn = _isolated_db
    db.create_endpoint(conn, "one", "https://a.example/x", "t")
    two = db.create_endpoint(conn, "two", "https://b.example/x", "t")
    assert db.update_endpoint(conn, two, name="one") is False
    assert db.get_endpoint(conn, two)["name"] == "two"


def test_cursor_save_reset_and_sync_status(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = db.create_endpoint(conn, "site", "https://a.example/x", "t")
    db.save_endpoint_cursor(conn, endpoint_id, "2026-08-01 00:00:00.123456+00", "lever", "9")
    row = db.get_endpoint(conn, endpoint_id)
    assert (row["cursor_changed_at"], row["cursor_source"], row["cursor_source_job_id"]) \
        == ("2026-08-01 00:00:00.123456+00", "lever", "9")

    db.record_endpoint_sync(conn, endpoint_id, "error", "HTTP 401")
    row = db.get_endpoint(conn, endpoint_id)
    assert row["last_sync_status"] == "error"
    assert row["last_sync_message"] == "HTTP 401"
    assert row["last_sync_at"] is not None

    db.reset_endpoint_cursor(conn, endpoint_id)
    row = db.get_endpoint(conn, endpoint_id)
    assert row["cursor_changed_at"] is None and row["cursor_source"] is None


def test_enabled_endpoints_and_toggle_and_delete(_isolated_db) -> None:
    conn = _isolated_db
    one = db.create_endpoint(conn, "one", "https://a.example/x", "t1")
    two = db.create_endpoint(conn, "two", "https://b.example/x", "t2")
    assert db.any_endpoint_enabled(conn)

    db.update_endpoint(conn, two, enabled=False)
    enabled = db.enabled_endpoints(conn)
    assert [e["name"] for e in enabled] == ["one"]
    assert enabled[0]["token"] == "t1"
    assert [e["name"] for e in db.enabled_endpoints(conn, name="two")] == []

    db.delete_endpoint(conn, one)
    assert not db.any_endpoint_enabled(conn)


def test_schema_check_notices_a_missing_sync_endpoints_table(_isolated_db) -> None:
    """An existing Docker deployment only gets new tables when
    `_schema_is_current` says something is missing."""
    conn = _isolated_db
    assert db._schema_is_current(conn)
    conn.execute("DROP TABLE sync_endpoints")
    assert not db._schema_is_current(conn)
