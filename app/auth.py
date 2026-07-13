"""Identity core: principal, password hashing, sessions, tokens, roles.

Design (specs/011-session-auth-rbac/): a login backend *establishes* a
session (today: username/password; later e.g. OIDC calls the same
establish_session), and everything downstream only ever *consumes* the
session — routes never know how the user signed in. Authentication is
enforced centrally by AuthMiddleware in app/main.py (default-deny on
/api); routes declare authorization with require_role(...).

Secrets at rest: passwords as Argon2id encoded hashes; session cookie
values and cipat_ token secrets only as SHA-256 digests (high-entropy
random strings — entropy, not slow hashing, is the defense there).
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request

from .authstore import AuthStore

COOKIE_NAME = "ci_session"
CSRF_HEADER = "x-requested-with"          # required value: "fetch"
TOKEN_PREFIX = "cipat_"                    # personal access tokens
ROLE_ORDER = {"viewer": 0, "author": 1, "admin": 2}

# Login lockout: LOCK_THRESHOLD consecutive failures lock for LOCK_BASE
# seconds, doubling per further failure, capped at LOCK_CAP.
LOCK_THRESHOLD = 5
LOCK_BASE = 60
LOCK_CAP = 900

TOUCH_INTERVAL = timedelta(seconds=60)     # throttle last-seen writes

_hasher = PasswordHasher()
# Verified for unknown usernames so the 401 path costs the same as a real
# password check (no username oracle, FR-014).
_DUMMY_HASH = _hasher.hash("cash-intel-dummy-password")


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    is_active: bool = True

    def has_role(self, role: str) -> bool:
        return ROLE_ORDER[self.role] >= ROLE_ORDER[role]


def principal_from_row(user_row: dict) -> User:
    return User(
        id=user_row["id"],
        username=user_row["username"],
        display_name=user_row["display_name"],
        role=user_row["role"],
        is_active=user_row["is_active"],
    )


# ── passwords ────────────────────────────────────────────────

def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except VerifyMismatchError:
        return False


def burn_a_hash_check(password: str) -> None:
    """Constant-shape work for the unknown-username login path."""
    verify_password(_DUMMY_HASH, password)


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# ── sessions & tokens (opaque secrets, hashed at rest) ───────

def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def establish_session(store: AuthStore, user_id: int) -> str:
    """Create a session and return the opaque cookie value. The single
    entry point any login backend (password now, OIDC later) must use."""
    value = secrets.token_urlsafe(32)
    store.create_session(user_id, _digest(value))
    return value


def resolve_session(store: AuthStore, cookie_value: str) -> User | None:
    row = store.get_session_user(_digest(cookie_value))
    if not row:
        return None
    last_seen = datetime.fromisoformat(row["last_seen"])
    if datetime.now(timezone.utc) - last_seen > TOUCH_INTERVAL:
        store.touch_session(row["session_id"])
    return principal_from_row(row)


def revoke_session(store: AuthStore, cookie_value: str) -> bool:
    return store.revoke_session(_digest(cookie_value))


def session_id_for(store: AuthStore, cookie_value: str) -> int | None:
    """Row id of the live session behind a cookie value (None if dead)."""
    row = store.get_session_user(_digest(cookie_value))
    return row["session_id"] if row else None


def mint_token(store: AuthStore, user_id: int, name: str) -> tuple[str, dict]:
    """Create a personal access token; the secret is returned exactly once."""
    secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = store.create_token(user_id, name, _digest(secret))
    return secret, row


def resolve_token(store: AuthStore, presented: str) -> User | None:
    if not presented.startswith(TOKEN_PREFIX):
        return None
    row = store.get_token_user(_digest(presented))
    if not row:
        return None
    last_used = row.get("token_last_used_at")
    if not last_used or datetime.now(timezone.utc) - datetime.fromisoformat(last_used) > TOUCH_INTERVAL:
        store.touch_token(row["token_id"])
    return principal_from_row(row)


# ── login lockout (per-account, persisted — survives restarts) ──

def lockout_remaining(user_row: dict) -> int:
    """Seconds until this account may attempt login again (0 = now)."""
    locked_until = user_row.get("locked_until")
    if not locked_until:
        return 0
    remaining = (datetime.fromisoformat(locked_until) - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining))


def register_login_failure(store: AuthStore, user_row: dict) -> None:
    fails = user_row["failed_attempts"] + 1
    locked_until = None
    if fails >= LOCK_THRESHOLD:
        seconds = min(LOCK_BASE * (2 ** (fails - LOCK_THRESHOLD)), LOCK_CAP)
        locked_until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
            timespec="seconds")
    store.set_lockout(user_row["id"], fails, locked_until)


def register_login_success(store: AuthStore, user_row: dict) -> None:
    if user_row["failed_attempts"] or user_row["locked_until"]:
        store.set_lockout(user_row["id"], 0, None)


# ── FastAPI dependencies (authorization; authentication is the
#    middleware's job) ────────────────────────────────────────

def get_current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_role(role: str):
    if role not in ROLE_ORDER:
        raise ValueError(f"unknown role '{role}'")

    def dependency(user: User = Depends(get_current_user)) -> User:
        if not user.has_role(role):
            raise HTTPException(status_code=403, detail=f"requires the {role} role")
        return user

    return dependency
