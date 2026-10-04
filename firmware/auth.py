"""The device token: the one credential for the web interface, the API, streams and RTSP.

It is accepted as
  - ?token=<token> on any URL (stream and snapshot URLs for other programs),
  - HTTP Basic (or, for RTSP, Digest) with user "admin" or "root" and the token as
    password, for clients that want a user name,
  - a session cookie, set by the login page or by opening a page with ?token=. Its value
    is derived from the token, so changing the token logs every browser out.
"""
from __future__ import annotations

import hashlib
import hmac
import time

USERS = ("admin", "root")
REALM = "camera"
COOKIE = "camera_session"
COOKIE_MAX_AGE = 365 * 24 * 3600
FAIL_DELAY = 1.0            # seconds, after a wrong token: slows down guessing


def same(given: str | None, token: str) -> bool:
    """Constant-time comparison; an empty token never matches."""
    return bool(given) and bool(token) and hmac.compare_digest(given.encode(), token.encode())


def session_value(token: str) -> str:
    return hmac.new(token.encode(), b"camera-firmware session", hashlib.sha256).hexdigest()


def user_ok(user: str | None, password: str | None, token: str) -> bool:
    return user in USERS and same(password, token)


def failed() -> None:
    time.sleep(FAIL_DELAY)
