"""SQLite persistence for chat-learned semantic-model memories.

A memory is a durable, user-independent fact the chat assistant learned
about one semantic *model* — an undeclared synonym for a declared
dimension/measure, or a free-text note about the model's vocabulary/shape.
Memories are keyed by model name only and shared by every user: the store
deliberately has no per-user retrieval axis, so nothing user-specific
(personal preferences, favorite charts, who asked what) can ever be
"remembered" through it — `created_by` exists purely as an audit
attribution, it never scopes a read. Admins curate the pool via
app/api/memories.py (edit/delete anything the assistant recorded).

Same shape as app/store.py's VisualStore (schema-on-init SCHEMA string,
sqlite3.Row factory, one class per feature's tables).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# The two memory kinds the assistant may record. Deliberately closed:
# a "preference"/"user"-shaped kind can't be smuggled in by the LLM —
# anything outside this vocabulary is dropped at validation.
MEMORY_KINDS = ("synonym", "note")

# Hard ceilings so a chatty (or misbehaving) model can't grow the store —
# and therefore every future prompt — without bound.
MAX_PER_MODEL = 200
MAX_SUBJECT_LEN = 100
MAX_CONTENT_LEN = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'chat',
    created_by TEXT NOT NULL DEFAULT '',
    conversation_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_memories_model ON model_memories (model, id);
"""


def validate_memory(model, kind: str, subject: str, content: str) -> Optional[str]:
    """The one shared rulebook for what a well-formed memory is — used both
    to silently drop a bad LLM-proposed memory (app/nlq.py) and to 400 a bad
    admin submission (app/api/memories.py). `model` is a semantic.Model.
    Returns an error string, or None when valid."""
    if kind not in MEMORY_KINDS:
        return f"unknown memory kind '{kind}' (use one of {', '.join(MEMORY_KINDS)})"
    if not content or not content.strip():
        return "memory content must not be empty"
    if len(content) > MAX_CONTENT_LEN:
        return f"memory content exceeds {MAX_CONTENT_LEN} characters"
    if len(subject) > MAX_SUBJECT_LEN:
        return f"memory subject exceeds {MAX_SUBJECT_LEN} characters"
    if kind == "synonym":
        if not subject:
            return "a synonym memory needs a subject (the declared dimension/measure it maps to)"
        target = model.dimensions.get(subject) or model.measures.get(subject)
        if target is None:
            return f"'{subject}' is not a declared dimension or measure of model '{model.name}'"
        term = content.strip().lower()
        declared = {n.lower() for n in model.dimensions} | {n.lower() for n in model.measures}
        if term in declared:
            return f"'{content.strip()}' is already a declared name on model '{model.name}'"
        known = {target.name.lower(), (target.label or "").lower()}
        known.update(s.lower() for s in target.synonyms)
        if term in known:
            return f"'{content.strip()}' is already a known name/synonym for '{subject}'"
    return None


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "model": row["model"],
            "kind": row["kind"],
            "subject": row["subject"],
            "content": row["content"],
            "source": row["source"],
            "created_by": row["created_by"],
            "conversation_id": row["conversation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_for_model(self, model: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM model_memories WHERE model = ? ORDER BY id", (model,)
            ).fetchall()
            return [self._to_dict(r) for r in rows]

    def all_by_model(self) -> dict[str, list[dict]]:
        """Every stored memory, grouped by model name — what feeds the chat
        catalog on each ask (app/api/chat.py)."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM model_memories ORDER BY model, id").fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["model"], []).append(self._to_dict(r))
        return out

    def get(self, memory_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM model_memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return self._to_dict(row) if row else None

    def add(self, model: str, kind: str, subject: str, content: str, *,
            source: str = "chat", created_by: str = "",
            conversation_id: Optional[int] = None) -> Optional[dict]:
        """Insert one memory. Returns None (a silent no-op, never an error)
        for an exact duplicate (case-insensitive on kind+subject+content) or
        when the model is at MAX_PER_MODEL — re-learning the same fact twice
        must not bloat the store or fail the ask that carried it."""
        subject, content = subject.strip(), content.strip()
        now = self._now()
        with self._conn() as conn:
            dup = conn.execute(
                "SELECT id FROM model_memories WHERE model = ? AND kind = ? "
                "AND lower(subject) = ? AND lower(content) = ?",
                (model, kind, subject.lower(), content.lower()),
            ).fetchone()
            if dup:
                return None
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM model_memories WHERE model = ?", (model,)
            ).fetchone()["n"]
            if count >= MAX_PER_MODEL:
                return None
            cur = conn.execute(
                "INSERT INTO model_memories "
                "(model, kind, subject, content, source, created_by, conversation_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (model, kind, subject, content, source, created_by, conversation_id, now, now),
            )
            row = conn.execute("SELECT * FROM model_memories WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._to_dict(row)

    def update(self, memory_id: int, *, subject: Optional[str] = None,
               content: Optional[str] = None) -> Optional[dict]:
        current = self.get(memory_id)
        if not current:
            return None
        fields, params = [], []
        if subject is not None:
            fields.append("subject = ?")
            params.append(subject.strip())
        if content is not None:
            fields.append("content = ?")
            params.append(content.strip())
        if fields:
            fields.append("updated_at = ?")
            params.append(self._now())
            params.append(memory_id)
            with self._conn() as conn:
                conn.execute(
                    f"UPDATE model_memories SET {', '.join(fields)} WHERE id = ?", params
                )
        return self.get(memory_id)

    def delete(self, memory_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM model_memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0
