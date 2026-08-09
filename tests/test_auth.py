"""dashboard/auth.py: the admin/other-visitor distinction.

`JOBSCRAPE_ADMIN_PASSCODE` blank (the default) must mean everyone is treated
as admin -- an existing single-user deployment must never be locked out by
an update that adds this. Configuring it turns on a real, signed-cookie gate.
"""

from __future__ import annotations

import time

from fastapi import Request

from jobscrape import config
from jobscrape.dashboard import auth


def _request(cookies: dict[str, str] | None = None) -> Request:
    cookie_header = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
    scope = {
        "type": "http",
        "headers": [(b"cookie", cookie_header.encode())] if cookie_header else [],
    }
    return Request(scope)


def test_no_passcode_configured_means_every_visitor_is_admin(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "")
    assert auth.is_admin(_request()) is True
    assert auth.admin_configured() is False


def test_a_visitor_with_no_cookie_is_not_admin_once_a_passcode_is_set(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    assert auth.is_admin(_request()) is False
    assert auth.admin_configured() is True


def test_a_valid_token_grants_admin(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    token = auth.make_token()
    assert auth.is_admin(_request({auth.COOKIE_NAME: token})) is True


def test_a_tampered_token_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    token = auth.make_token()
    expiry, _, signature = token.partition(".")
    tampered = f"{expiry}.{'0' * len(signature)}"
    assert auth.is_admin(_request({auth.COOKIE_NAME: tampered})) is False


def test_a_token_signed_under_a_different_passcode_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    token = auth.make_token()
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "a-different-passcode")
    assert auth.is_admin(_request({auth.COOKIE_NAME: token})) is False


def test_an_expired_token_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    monkeypatch.setattr(auth, "SESSION_SECONDS", -1)  # already expired the instant it's made
    token = auth.make_token()
    assert auth.is_admin(_request({auth.COOKIE_NAME: token})) is False


def test_a_malformed_token_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    for bad in ("", "no-dot-here", ".", "123.", ".abc", "not-a-number.abc"):
        assert auth.is_admin(_request({auth.COOKIE_NAME: bad})) is False


def test_check_passcode_accepts_the_right_value(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    assert auth.check_passcode("hunter2") is True


def test_check_passcode_rejects_the_wrong_value(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    assert auth.check_passcode("wrong") is False
    assert auth.check_passcode("") is False


def test_check_passcode_always_fails_when_nothing_is_configured(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "")
    assert auth.check_passcode("") is False
    assert auth.check_passcode("anything") is False


def test_make_token_embeds_a_future_expiry(monkeypatch) -> None:
    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    before = time.time()
    token = auth.make_token()
    expiry = int(token.partition(".")[0])
    assert expiry > before
