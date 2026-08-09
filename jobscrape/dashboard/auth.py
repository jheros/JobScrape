"""A lightweight admin/other-visitor distinction for the dashboard.

The dashboard has no login on any route by default (see `docs/DOCKER.md`) --
this adds exactly one: a single shared passcode (`JOBSCRAPE_ADMIN_PASSCODE`)
that marks one browser as "admin" via a signed cookie. Everyone else is a
plain visitor -- view-only everywhere, plus editing the blocked-companies
list (the one thing product wants any visitor able to do). This is NOT a
real accounts system (one shared secret, one role) -- it's the minimum that
lets a public-ish link be handed out without handing out the ability to
delete data, change API keys, or reconfigure schedules.

`JOBSCRAPE_ADMIN_PASSCODE` blank (the default) means this is switched off
entirely -- every visitor is treated as admin, exactly the behavior before
this existed, so an existing single-user deployment is never locked out by
an update that adds this.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request

from .. import config

COOKIE_NAME = "js_admin"

#: How long a successful login stays valid. Long-lived on purpose -- this is
#: a convenience distinction, not a security boundary that needs frequent
#: re-proof, and the passcode itself is the thing that actually gates entry.
SESSION_SECONDS = 180 * 24 * 3600


def _sign(message: str) -> str:
    # The passcode itself is the HMAC key -- it's already meant to be secret,
    # and reusing it avoids a second secret to configure and keep in sync.
    return hmac.new(
        config.ADMIN_PASSCODE.encode(), message.encode(), hashlib.sha256
    ).hexdigest()


def make_token() -> str:
    """A signed `expiry.signature` token proving the passcode was entered,
    without embedding or re-checking the passcode itself on every request."""
    expiry = str(int(time.time()) + SESSION_SECONDS)
    return f"{expiry}.{_sign(expiry)}"


def _token_is_valid(token: str) -> bool:
    expiry, _, signature = token.partition(".")
    if not expiry or not signature:
        return False
    if not hmac.compare_digest(signature, _sign(expiry)):
        return False
    try:
        return int(expiry) > time.time()
    except ValueError:
        return False


def check_passcode(passcode: str) -> bool:
    """Constant-time compare -- a login form is exactly the kind of
    user-facing string-equality check timing attacks target."""
    if not config.ADMIN_PASSCODE:
        return False
    return hmac.compare_digest(passcode, config.ADMIN_PASSCODE)


def is_admin(request: Request) -> bool:
    if not config.ADMIN_PASSCODE:
        return True
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return _token_is_valid(token)


def admin_configured() -> bool:
    return bool(config.ADMIN_PASSCODE)
