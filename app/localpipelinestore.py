"""SQLite persistence for locally-authored pipelines (app/api/pipelines.py) —
the same DB as everything else (config.DB_PATH, gitignored). A pipeline
created or edited through the running app never touches the filesystem
(config.PIPELINES_DIR only serves a hypothetical built-in catalog, empty
today); it lives here instead, keyed by name — mirrors app/localmodelstore.py,
minus renaming support (a pipeline's name is immutable, see
app/api/pipelines.py's put_pipeline_yaml).

Also holds the single deployment-wide layers.yaml document (app/api/pipelines
.py's /lineage/layers): one row, not one per layer, since a PUT there always
replaces the whole ordered list."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_pipelines (
    name TEXT PRIMARY KEY,
    yaml TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_layers (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    yaml TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class LocalPipelineStore:
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
            rows = conn.execute("SELECT * FROM local_pipelines ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get(self, name: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM local_pipelines WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def create(self, name: str, yaml_text: str) -> dict:
        """Insert a new row — or reclaim a stale one at the same key (a
        deleted pipeline's row can be left behind as a ghost); the caller
        (create_pipeline) has already confirmed nothing live owns `name`."""
        now = self._now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO local_pipelines (name, yaml, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET yaml = excluded.yaml, updated_at = excluded.updated_at",
                (name, yaml_text, now, now),
            )
        return self.get(name)

    def update(self, name: str, yaml_text: str) -> Optional[dict]:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE local_pipelines SET yaml = ?, updated_at = ? WHERE name = ?",
                (yaml_text, self._now(), name),
            )
        return self.get(name) if cur.rowcount else None

    def delete(self, name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM local_pipelines WHERE name = ?", (name,))
        return cur.rowcount > 0

    def get_layers_yaml(self) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT yaml FROM pipeline_layers WHERE id = 1").fetchone()
        return row["yaml"] if row else None

    def set_layers_yaml(self, yaml_text: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO pipeline_layers (id, yaml, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET yaml = excluded.yaml, updated_at = excluded.updated_at",
                (yaml_text, self._now()),
            )
