"""SQLite persistence for identity: users, sessions, API tokens, audit.

Lives on the same database file as VisualStore (single-writer constraint)
but is a separate class so identity persistence can later be swapped (e.g.
Redis/Postgres sessions when scaling past one process) without touching
content storage. Only hashes of session cookies and token secrets are ever
stored — see app/auth.py for the hashing side.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROLES = ("viewer", "author", "admin")
# spec 013: kept in sync by hand with the 4 ids in app/static/js/theme.js's
# THEMES catalog — this backend has no shared-code path to the frontend, and
# a 4-entry list changing rarely is a fine place to accept that duplication.
VALID_THEMES = ("cyberpunk", "daylight", "slate", "contrast")
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{2,32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer','author','admin')),
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_label TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class LastAdminError(Exception):
    """Raised when an update would leave the system with no active admin."""


class AuthStore:
    def __init__(self, db_path: Path, idle_days: int = 7, max_days: int = 30):
        self.db_path = db_path
        self.idle = timedelta(days=idle_days)
        self.absolute = timedelta(days=max_days)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # spec 013: account-level theme preference, guarded so an
            # existing database upgrades in place (same pattern as
            # VisualStore.measure_provenance.user_id / ConversationStore's
            # conversations.llm_model).
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            if "theme" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN theme TEXT")
                conn.execute("ALTER TABLE users ADD COLUMN theme_updated_at TEXT")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── users ────────────────────────────────────────────────

    @staticmethod
    def _user_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "password_hash": row["password_hash"],
            "is_active": bool(row["is_active"]),
            "failed_attempts": row["failed_attempts"],
            "locked_until": row["locked_until"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "theme": row["theme"],
            "theme_updated_at": row["theme_updated_at"],
        }

    @staticmethod
    def validate_username(username: str) -> str:
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise ValueError(
                "username must be 2-32 chars of a-z, 0-9, '_', '.', '-'")
        return username

    def create_user(self, username: str, display_name: str, role: str,
                    password_hash: str) -> dict:
        username = self.validate_username(username)
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        now = _iso(_now())
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, display_name, role, password_hash, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (username, display_name.strip() or username, role, password_hash, now, now),
            )
        return self.get_user(cur.lastrowid)

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._user_to_dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
            ).fetchone()
        return self._user_to_dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [self._user_to_dict(r) for r in rows]

    def count_users(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def update_user(self, user_id: int, *, display_name: Optional[str] = None,
                    role: Optional[str] = None, is_active: Optional[bool] = None,
                    password_hash: Optional[str] = None) -> Optional[dict]:
        """Partial update. Refuses (LastAdminError) any change that would
        leave zero active admins — the check and the write share one
        connection so the invariant holds under the single-writer model."""
        if role is not None and role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                return None
            demoting = role is not None and role != "admin" and row["role"] == "admin"
            deactivating = is_active is False and bool(row["is_active"])
            if (demoting or deactivating) and row["role"] == "admin" and bool(row["is_active"]):
                others = conn.execute(
                    "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1 "
                    "AND id != ?", (user_id,),
                ).fetchone()["n"]
                if others == 0:
                    raise LastAdminError("cannot demote or deactivate the last active admin")
            sets, params = ["updated_at = ?"], [_iso(_now())]
            for col, val in (("display_name", display_name), ("role", role),
                             ("password_hash", password_hash)):
                if val is not None:
                    sets.append(f"{col} = ?")
                    params.append(val)
            if is_active is not None:
                sets.append("is_active = ?")
                params.append(1 if is_active else 0)
            params.append(user_id)
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        return self.get_user(user_id)

    def set_lockout(self, user_id: int, failed_attempts: int,
                    locked_until: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                (failed_attempts, locked_until, user_id),
            )

    # ── theme preference (spec 013) ─────────────────────────────
    # A user's own account-level pick among the 4 fixed themes; validated
    # against the known ids by the API layer (app/api/users.py), not here —
    # same split as `role`, which this file also stores as a bare TEXT
    # column and leaves enum validation to the caller.

    def get_theme(self, user_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT theme, theme_updated_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if not row:
            return None
        return {"theme": row["theme"], "updated_at": row["theme_updated_at"]}

    def set_theme(self, user_id: int, theme: str) -> dict:
        if theme not in VALID_THEMES:
            raise ValueError(f"theme must be one of {VALID_THEMES}")
        now = _iso(_now())
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET theme = ?, theme_updated_at = ? WHERE id = ?",
                (theme, now, user_id),
            )
        return {"theme": theme, "updated_at": now}

    # ── sessions ─────────────────────────────────────────────
    # The narrow seam for a future shared session store: everything below
    # deals in token *hashes* and returns plain dicts.

    def create_session(self, user_id: int, token_hash: str) -> dict:
        now = _iso(_now())
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, last_seen) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, user_id, now, now),
            )
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def get_session_user(self, token_hash: str) -> Optional[dict]:
        """Live session joined to its (active) user, or None. Enforces
        revocation, idle timeout, absolute lifetime, and user activeness."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT s.id AS session_id, s.created_at AS s_created, s.last_seen, "
                "s.revoked_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ?", (token_hash,),
            ).fetchone()
        if not row or row["revoked_at"] or not row["is_active"]:
            return None
        now = _now()
        created = datetime.fromisoformat(row["s_created"])
        last_seen = datetime.fromisoformat(row["last_seen"])
        if now - created > self.absolute or now - last_seen > self.idle:
            return None
        out = self._user_to_dict(row)
        out["session_id"] = row["session_id"]
        out["last_seen"] = row["last_seen"]
        return out

    def touch_session(self, session_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?",
                         (_iso(_now()), session_id))

    def revoke_session(self, token_hash: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (_iso(_now()), token_hash),
            )
        return cur.rowcount > 0

    def revoke_sessions_for_user(self, user_id: int,
                                 except_session_id: Optional[int] = None) -> int:
        sql = "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL"
        params: list = [_iso(_now()), user_id]
        if except_session_id is not None:
            sql += " AND id != ?"
            params.append(except_session_id)
        with self._conn() as conn:
            cur = conn.execute(sql, params)
        return cur.rowcount

    # ── personal access tokens ───────────────────────────────

    @staticmethod
    def _token_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "revoked_at": row["revoked_at"],
        }

    def create_token(self, user_id: int, name: str, token_hash: str) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO api_tokens (token_hash, user_id, name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, user_id, name.strip() or "token", _iso(_now())),
            )
            row = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._token_to_dict(row)

    def list_tokens(self, user_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [self._token_to_dict(r) for r in rows]

    def get_token_user(self, token_hash: str) -> Optional[dict]:
        """Live token joined to its (active) user, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT t.id AS token_id, t.last_used_at, t.revoked_at AS t_revoked, u.* "
                "FROM api_tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash = ?", (token_hash,),
            ).fetchone()
        if not row or row["t_revoked"] or not row["is_active"]:
            return None
        out = self._user_to_dict(row)
        out["token_id"] = row["token_id"]
        out["token_last_used_at"] = row["last_used_at"]
        return out

    def touch_token(self, token_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                         (_iso(_now()), token_id))

    def revoke_token(self, token_id: int, user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? "
                "AND revoked_at IS NULL",
                (_iso(_now()), token_id, user_id),
            )
        return cur.rowcount > 0

    # ── audit (append-only; no browsing API in this feature) ─

    def record_audit(self, action: str, actor_label: str,
                     actor_user_id: Optional[int] = None,
                     target: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit_events (actor_user_id, actor_label, action, target, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (actor_user_id, actor_label, action, target, _iso(_now())),
            )

    def audit_events(self) -> list[dict]:
        """Read-back used by tests; there is deliberately no HTTP surface."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        return [dict(r) for r in rows]
