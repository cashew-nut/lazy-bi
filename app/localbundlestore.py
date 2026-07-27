"""SQLite persistence for locally-authored dimension bundles (app/api/dimensions.py)
— the same DB as everything else (config.DB_PATH, gitignored). A bundle created or
edited through the running app never touches the git-tracked dimensions/
directory (and so never becomes locked); it lives here instead, keyed by the
name it was created under (mirrors app/localmodelstore.py exactly — see there
for why the key can drift from the name declared inside its own yaml)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_bundles (
    name TEXT PRIMARY KEY,
    yaml TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class LocalBundleStore:
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
            rows = conn.execute("SELECT * FROM local_bundles ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get(self, name: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM local_bundles WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def create(self, name: str, yaml_text: str) -> dict:
        now = self._now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO local_bundles (name, yaml, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, yaml_text, now, now),
            )
        return self.get(name)

    def update(self, name: str, yaml_text: str) -> Optional[dict]:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE local_bundles SET yaml = ?, updated_at = ? WHERE name = ?",
                (yaml_text, self._now(), name),
            )
        return self.get(name) if cur.rowcount else None

    def delete(self, name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM local_bundles WHERE name = ?", (name,))
        return cur.rowcount > 0
