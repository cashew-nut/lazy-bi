"""SQLite persistence for saved visuals."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS visuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    model TEXT NOT NULL,
    spec TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dashboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    items TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    dashboard_id INTEGER PRIMARY KEY,
    folder TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class VisualStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "model": row["model"],
            "spec": json.loads(row["spec"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM visuals ORDER BY updated_at DESC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, visual_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM visuals WHERE id = ?", (visual_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def create(self, name: str, model: str, spec: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO visuals (name, model, spec, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (name, model, json.dumps(spec), now, now),
            )
        return self.get(cur.lastrowid)

    def update(self, visual_id: int, name: str, model: str, spec: dict) -> Optional[dict]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE visuals SET name = ?, model = ?, spec = ?, updated_at = ? WHERE id = ?",
                (name, model, json.dumps(spec), now, visual_id),
            )
        return self.get(visual_id) if cur.rowcount else None

    def delete(self, visual_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM visuals WHERE id = ?", (visual_id,))
        return cur.rowcount > 0

    # ── dashboards ──────────────────────────────────────────
    # The items column stores {"items": [{"visual_id": int, "w": 1|2}],
    # "views": [{"name": str, "filters": [...]}], "active_view": int}.
    # A view is a named filter set pushed down to every tile whose model has
    # the filtered dimension. Legacy rows stored a bare items list.

    @staticmethod
    def _dash_to_dict(row: sqlite3.Row) -> dict:
        payload = json.loads(row["items"])
        if isinstance(payload, list):  # legacy: bare tile list
            payload = {"items": payload}
        items = payload.get("items")
        views = payload.get("views") or []
        if items is None and views and "items" in views[0]:  # transitional shape
            items = views[0]["items"]
        views = [v for v in views if "filters" in v] or [{"name": "default", "filters": []}]
        return {
            "id": row["id"],
            "name": row["name"],
            "items": items or [],
            "views": views,
            "active_view": min(payload.get("active_view", 0), len(views) - 1),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_dashboards(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM dashboards ORDER BY updated_at DESC").fetchall()
        return [self._dash_to_dict(r) for r in rows]

    def get_dashboard(self, dash_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM dashboards WHERE id = ?", (dash_id,)).fetchone()
        return self._dash_to_dict(row) if row else None

    @staticmethod
    def _dash_payload(items: list, views: list, active_view: int) -> str:
        if not views:
            views = [{"name": "default", "filters": []}]
        return json.dumps({"items": items, "views": views, "active_view": active_view})

    def create_dashboard(self, name: str, items: list, views: list, active_view: int = 0) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO dashboards (name, items, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, self._dash_payload(items, views, active_view), now, now),
            )
        return self.get_dashboard(cur.lastrowid)

    def update_dashboard(self, dash_id: int, name: str, items: list, views: list,
                         active_view: int = 0) -> Optional[dict]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE dashboards SET name = ?, items = ?, updated_at = ? WHERE id = ?",
                (name, self._dash_payload(items, views, active_view), now, dash_id),
            )
        return self.get_dashboard(dash_id) if cur.rowcount else None

    def delete_dashboard(self, dash_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM dashboards WHERE id = ?", (dash_id,))
            conn.execute("DELETE FROM publications WHERE dashboard_id = ?", (dash_id,))
        return cur.rowcount > 0

    # ── publications: dashboards exposed in the portal, in nested folders ──
    # folder is a slash path ("", "finance", "finance/quarterly", ...)

    def publish(self, dashboard_id: int, folder: str) -> Optional[dict]:
        if not self.get_dashboard(dashboard_id):
            return None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO publications (dashboard_id, folder, published_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(dashboard_id) DO UPDATE SET folder = excluded.folder, updated_at = excluded.updated_at",
                (dashboard_id, folder, now, now),
            )
        return {"dashboard_id": dashboard_id, "folder": folder}

    def unpublish(self, dashboard_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM publications WHERE dashboard_id = ?", (dashboard_id,))
        return cur.rowcount > 0

    def list_publications(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT p.dashboard_id, p.folder, p.published_at, p.updated_at, d.name, d.items "
                "FROM publications p JOIN dashboards d ON d.id = p.dashboard_id "
                "ORDER BY p.folder, d.name"
            ).fetchall()
        out = []
        for r in rows:
            payload = json.loads(r["items"])
            items = payload if isinstance(payload, list) else payload.get("items", [])
            out.append({
                "dashboard_id": r["dashboard_id"],
                "name": r["name"],
                "folder": r["folder"],
                "tiles": len(items or []),
                "published_at": r["published_at"],
                "updated_at": r["updated_at"],
            })
        return out
