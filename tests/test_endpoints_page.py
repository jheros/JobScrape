"""The Endpoints page and its JSON routes.

The token is write-only: it goes in through create/update and never comes
back out in the page, a JSON response or a log line.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from jobscrape import db, endpoint_sync
from jobscrape.dashboard.app import app

TOKEN = "tok-secret-7f3a"


@pytest.fixture()
def client(_isolated_db):
    with TestClient(app) as test_client:
        yield test_client


def _create(client, **overrides):
    data = {"name": "site", "url": "https://receiver.test/import", "token": TOKEN}
    data.update(overrides)
    return client.post("/api/endpoints", data=data)


def _only(conn):
    rows = db.list_endpoints(conn)
    assert len(rows) == 1
    return rows[0]


def test_create_and_page_shows_it_without_the_token(client, _isolated_db) -> None:
    created = _create(client)
    assert created.status_code == 200, created.text
    assert TOKEN not in created.text
    endpoint_id = created.json()["id"]

    page = client.get("/endpoints")
    assert page.status_code == 200
    assert "receiver.test/import" in page.text
    assert "Token set" in page.text
    assert TOKEN not in page.text
    assert db.get_endpoint(_isolated_db, endpoint_id, with_token=True)["token"] == TOKEN


def test_sidebar_links_to_the_endpoints_page(client) -> None:
    assert 'href="/endpoints"' in client.get("/").text


def test_update_with_a_blank_token_keeps_the_current_one(client, _isolated_db) -> None:
    endpoint_id = _create(client).json()["id"]
    # Exactly what the browser sends when the token field is left alone.
    response = client.post(f"/api/endpoints/{endpoint_id}", data={
        "name": "renamed", "url": "https://other.test/in", "token": "",
    })
    assert response.status_code == 200, response.text
    assert TOKEN not in response.text
    row = db.get_endpoint(_isolated_db, endpoint_id, with_token=True)
    assert (row["name"], row["url"], row["token"]) == ("renamed", "https://other.test/in", TOKEN)

    client.post(f"/api/endpoints/{endpoint_id}", data={
        "name": "renamed", "url": "https://other.test/in", "token": "new-token"})
    assert db.get_endpoint(_isolated_db, endpoint_id, with_token=True)["token"] == "new-token"


def test_toggle_delete_and_reset(client, _isolated_db) -> None:
    endpoint_id = _create(client).json()["id"]
    db.save_endpoint_cursor(_isolated_db, endpoint_id, "2026-01-01 00:00:00+00", "lever", "1")

    assert client.post(f"/api/endpoints/{endpoint_id}/toggle").status_code == 200
    assert _only(_isolated_db)["enabled"] is False
    client.post(f"/api/endpoints/{endpoint_id}/toggle")
    assert _only(_isolated_db)["enabled"] is True

    reset = client.post(f"/api/endpoints/{endpoint_id}/reset")
    assert reset.status_code == 200
    assert _only(_isolated_db)["cursor_changed_at"] is None

    assert client.post(f"/api/endpoints/{endpoint_id}/delete").status_code == 200
    assert db.list_endpoints(_isolated_db) == []


def test_unknown_endpoint_ids_are_404(client) -> None:
    for path in ("", "/toggle", "/delete", "/test", "/reset"):
        data = {"name": "x", "url": "https://a.test/", "token": ""} if path == "" else None
        response = client.post(f"/api/endpoints/999999{path}", data=data)
        assert response.status_code == 404, path


@pytest.mark.parametrize("url", [
    "http://receiver.example.com/import",       # plain http, public host
    "http://8.8.8.8/import",                    # plain http, public IP
    "ftp://receiver.test/import",
    "https://user:pass@receiver.test/import",   # embedded credentials
    "https://user@receiver.test/import",
    "https:///import",                          # no host
    "receiver.test/import",
    "",
])
def test_invalid_urls_are_rejected(client, url) -> None:
    response = _create(client, url=url)
    assert response.status_code == 400
    assert response.json()["error"]


@pytest.mark.parametrize("url", [
    "https://receiver.example.com/import",
    "http://localhost:3000/api/import",
    "http://127.0.0.1:8000/in",
    "http://192.168.1.20/in",
    "http://10.0.0.5:9000/in",
    "http://172.16.4.4/in",
])
def test_allowed_urls(client, url) -> None:
    assert _create(client, url=url).status_code == 200


def test_update_validates_the_url_too(client) -> None:
    endpoint_id = _create(client).json()["id"]
    response = client.post(f"/api/endpoints/{endpoint_id}", data={
        "name": "site", "url": "http://public.example.com/in", "token": ""})
    assert response.status_code == 400


def test_names_are_required_and_unique(client) -> None:
    assert _create(client, name="  ").status_code == 400
    assert _create(client).status_code == 200
    duplicate = _create(client, url="https://b.test/in")
    assert duplicate.status_code == 400
    assert "already" in duplicate.json()["error"]

    other_id = _create(client, name="other").json()["id"]
    clash = client.post(f"/api/endpoints/{other_id}", data={
        "name": "site", "url": "https://b.test/in", "token": ""})
    assert clash.status_code == 400


def test_a_token_is_required_on_create(client) -> None:
    assert _create(client, token="").status_code == 400


def _mock_receiver(monkeypatch, respond):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return respond(request)

    monkeypatch.setattr(endpoint_sync, "_make_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler)))
    return seen


def test_test_connection_reports_success_and_changes_nothing(
    client, _isolated_db, monkeypatch,
) -> None:
    endpoint_id = _create(client).json()["id"]
    db.save_endpoint_cursor(_isolated_db, endpoint_id, "2026-01-01 00:00:00+00", "lever", "1")
    before = db.get_endpoint(_isolated_db, endpoint_id)
    seen = _mock_receiver(monkeypatch, lambda req: httpx.Response(200, json={}))

    response = client.post(f"/api/endpoints/{endpoint_id}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True and response.json()["status"] == 200
    assert TOKEN not in response.text
    assert len(seen) == 1
    assert seen[0].content == b'{"rows": []}'
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert db.get_endpoint(_isolated_db, endpoint_id) == before


def test_test_connection_reports_failure_and_changes_nothing(
    client, _isolated_db, monkeypatch,
) -> None:
    endpoint_id = _create(client).json()["id"]
    before = db.get_endpoint(_isolated_db, endpoint_id)
    # A receiver that echoes the header back must not leak it either.
    _mock_receiver(monkeypatch, lambda req: httpx.Response(
        401, text=f"bad token {req.headers['authorization']}"))

    response = client.post(f"/api/endpoints/{endpoint_id}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and "401" in body["message"]
    assert TOKEN not in response.text
    assert db.get_endpoint(_isolated_db, endpoint_id) == before


def test_test_connection_reports_a_network_error(client, monkeypatch) -> None:
    endpoint_id = _create(client).json()["id"]

    def refuse(req):
        raise httpx.ConnectError("connection refused", request=req)

    _mock_receiver(monkeypatch, refuse)
    body = client.post(f"/api/endpoints/{endpoint_id}/test").json()
    assert body["ok"] is False and "ConnectError" in body["message"]
