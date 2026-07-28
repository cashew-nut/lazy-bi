"""SQLite persistence for locally-authored models (app/api/models.py) — the
same DB as everything else (config.DB_PATH, gitignored). A model created or
edited through the running app never touches the git-tracked models/
directory; it lives here instead, keyed by the name it was created under
(which may drift from the name declared inside its yaml after a rename —
the same quirk a file's basename has relative to its own `name:`, see
app/api/models.py's put_model_yaml)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_models (
    name TEXT PRIMARY KEY,
    yaml TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class LocalModelStore:
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

    def list(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM local_models ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get(self, name: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM local_models WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def create(self, name: str, yaml_text: str) -> dict:
        """Insert a new row — or reclaim a stale one at the same key. A row
        can already occupy `name` despite no live model being registered
        under it: a rename (see update() below) that predates this method
        existing left its old key behind as a ghost. The caller (create_model)
        has already confirmed nothing live owns `name`, so overwriting here
        is reclaiming an orphan, not clobbering real data."""
        now = self._now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO local_models (name, yaml, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET yaml = excluded.yaml, updated_at = excluded.updated_at",
                (name, yaml_text, now, now),
            )
        return self.get(name)

    def update(self, name: str, yaml_text: str, new_name: Optional[str] = None) -> Optional[dict]:
        """Rewrite a row's yaml in place. Pass `new_name` when the model's own
        `name:` just changed so the row's key (its lookup identity for every
        later get/update/delete) moves with it — otherwise the row is
        stranded under its old key forever, invisible to future operations
        issued against the model's new name."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE local_models SET name = ?, yaml = ?, updated_at = ? WHERE name = ?",
                (new_name or name, yaml_text, self._now(), name),
            )
        return self.get(new_name or name) if cur.rowcount else None

    def delete(self, name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM local_models WHERE name = ?", (name,))
        return cur.rowcount > 0
