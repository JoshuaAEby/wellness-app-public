import hmac
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from app.config import settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

USERS: dict[str, str] = {
    "owner": settings.app_password,
}


def secret_equals(given: str | None, expected: str | None) -> bool:
    """Constant-time string comparison for passwords and tokens (2026-10-21).

    `==` returns at the first differing byte, so response timing leaks how much of a
    guess is right. Over a LAN that is hard to exploit, but it costs nothing to close.
    An empty expected value never matches (a blank secret must not mean "open").
    """
    if not expected or given is None:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def bearer_matches(authorization: str | None, secret: str | None) -> bool:
    """`Authorization: Bearer <secret>` check, constant-time."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return secret_equals(authorization[len("Bearer "):], secret)


# ── Login throttle (2026-10-21) ───────────────────────────────────────────────
# Single-user app, single process: an in-memory window is enough. Every request arrives
# from Caddy, so the client IP is the proxy's — throttle on failures overall, not per IP.
# After LOGIN_MAX_FAILURES failures inside LOGIN_WINDOW_S, login refuses (429) until the
# oldest failure ages out. A successful login does not clear the window, so a guesser
# cannot reset it by also knowing nothing.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 15 * 60
_login_failures: deque = deque()


def login_locked_for() -> int:
    """Seconds until login is allowed again (0 = allowed)."""
    now = time.monotonic()
    while _login_failures and now - _login_failures[0] > LOGIN_WINDOW_S:
        _login_failures.popleft()
    if len(_login_failures) < LOGIN_MAX_FAILURES:
        return 0
    return int(LOGIN_WINDOW_S - (now - _login_failures[0])) + 1


def record_login_failure() -> None:
    _login_failures.append(time.monotonic())


def authenticate_user(username: str, password: str) -> str | None:
    """Return username if credentials are valid, else None."""
    expected = USERS.get(username)
    if expected is None or not secret_equals(password, expected):
        return None
    return username


def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> str:
    """Decode a JWT and return the username, or raise HTTPException."""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
        username: str = payload.get("sub")
        if username is None or username not in USERS:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return username
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    return decode_token(token)
