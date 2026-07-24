"""SQLite persistence for saved sandbox notebooks (ad hoc polars/python
scratch scripts — see app/sandbox.py). Same DB as everything else
(config.DB_PATH). A notebook is just {name, cells}; no execution state is
persisted — a run's output is ephemeral, recomputed on demand, never saved
(Constitution V)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_notebooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cells TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SandboxStore:
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
            "id": row["id"], "name": row["name"], "cells": json.loads(row["cells"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def list(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at, updated_at FROM sandbox_notebooks ORDER BY updated_at DESC"
            ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def get(self, nb_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sandbox_notebooks WHERE id = ?", (nb_id,)).fetchone()
        return self._to_dict(row) if row else None

    def create(self, name: str, cells: list[dict]) -> dict:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO sandbox_notebooks (name, cells, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, json.dumps(cells), now, now),
            )
        return self.get(cur.lastrowid)

    def update(self, nb_id: int, name: str, cells: list[dict]) -> Optional[dict]:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE sandbox_notebooks SET name = ?, cells = ?, updated_at = ? WHERE id = ?",
                (name, json.dumps(cells), now, nb_id),
            )
        return self.get(nb_id) if cur.rowcount else None

    def delete(self, nb_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM sandbox_notebooks WHERE id = ?", (nb_id,))
        return cur.rowcount > 0
