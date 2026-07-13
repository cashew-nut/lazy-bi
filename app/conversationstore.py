"""SQLite persistence for conversational-analytics conversations.

Same shape as app/store.py's VisualStore (schema-on-init SCHEMA string,
sqlite3.Row factory, one class per feature's tables) but kept separate: a
per-user chat log has a different access pattern (strictly owner-scoped,
viewer-writable) than visuals/dashboards (author/admin-gated mutations,
shared across all users) — see specs/012-conversational-analytics/
research.md R3.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    model_scope TEXT NOT NULL DEFAULT '[]',
    llm_model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    question_text TEXT,
    resolved_query TEXT,
    result TEXT,
    outcome TEXT,
    answer_text TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_conversation
    ON conversation_messages (conversation_id, id);
"""


class ConversationStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # guarded so a database created before per-conversation model
            # selection upgrades in place, same pattern as store.py's
            # measure_provenance.user_id migration
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
            if "llm_model" not in cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN llm_model TEXT")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _message_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "question_text": row["question_text"],
            "resolved_query": json.loads(row["resolved_query"]) if row["resolved_query"] else None,
            "result": json.loads(row["result"]) if row["result"] else None,
            "outcome": row["outcome"],
            "answer_text": row["answer_text"],
            "created_at": row["created_at"],
        }

    def _messages(self, conn: sqlite3.Connection, conversation_id: int) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [self._message_to_dict(r) for r in rows]

    def _conversation_to_dict(self, conn: sqlite3.Connection, row: sqlite3.Row,
                               with_messages: bool = True) -> dict:
        out = {
            "id": row["id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "model_scope": json.loads(row["model_scope"]),
            "llm_model": row["llm_model"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if with_messages:
            out["messages"] = self._messages(conn, row["id"])
        return out

    # ── conversations ────────────────────────────────────────

    def list_for_user(self, user_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            return [self._conversation_to_dict(conn, r, with_messages=False) for r in rows]

    def get(self, conversation_id: int, user_id: int) -> Optional[dict]:
        """Owner-scoped: returns None both for a missing conversation and for
        one that exists but isn't owned by user_id — existence is never
        leaked across users (FR-013)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
            return self._conversation_to_dict(conn, row) if row else None

    def create(self, user_id: int, model_scope: Optional[list[str]] = None,
               llm_model: Optional[str] = None) -> dict:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO conversations (user_id, title, model_scope, llm_model, created_at, updated_at) "
                "VALUES (?, '', ?, ?, ?, ?)",
                (user_id, json.dumps(model_scope or []), llm_model, now, now),
            )
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._conversation_to_dict(conn, row)

    def update(self, conversation_id: int, user_id: int, *,
               title: Optional[str] = None, model_scope: Optional[list[str]] = None,
               llm_model: Optional[str] = None) -> Optional[dict]:
        if not self.get(conversation_id, user_id):
            return None
        fields, params = [], []
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if model_scope is not None:
            fields.append("model_scope = ?")
            params.append(json.dumps(model_scope))
        if llm_model is not None:
            fields.append("llm_model = ?")
            params.append(llm_model)
        if fields:
            fields.append("updated_at = ?")
            params.append(self._now())
            params.extend([conversation_id, user_id])
            with self._conn() as conn:
                conn.execute(
                    f"UPDATE conversations SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
                    params,
                )
        return self.get(conversation_id, user_id)

    def delete(self, conversation_id: int, user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
            )
            if cur.rowcount:
                conn.execute(
                    "DELETE FROM conversation_messages WHERE conversation_id = ?", (conversation_id,)
                )
            return cur.rowcount > 0

    # ── messages ─────────────────────────────────────────────

    def add_message(self, conversation_id: int, role: str, *,
                     question_text: Optional[str] = None,
                     resolved_query: Optional[dict] = None,
                     result: Optional[dict] = None,
                     outcome: Optional[str] = None,
                     answer_text: Optional[str] = None) -> dict:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id, role, question_text, resolved_query, result, outcome, answer_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, role, question_text,
                 json.dumps(resolved_query) if resolved_query is not None else None,
                 json.dumps(result) if result is not None else None,
                 outcome, answer_text, now),
            )
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
            # first message in a title-less conversation seeds its title
            conv = conn.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if role == "user" and conv and not conv["title"] and question_text:
                title = question_text.strip()[:80]
                conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
            row = conn.execute(
                "SELECT * FROM conversation_messages WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._message_to_dict(row)
