"""SQLite persistence for pipeline runs (specs/014-polars-pipeline-module/).

Append-only history: rows are created queued, transition through the
lifecycle, and are never deleted (a deleted pipeline's run history is
retained — see data-model.md). Only the parent job-worker process
(app/pipeline_jobs.py) ever writes to this table, preserving the app's
single-writer posture."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    status TEXT NOT NULL,
    triggered_by INTEGER,
    triggered_label TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    rows_written INTEGER,
    rows_deleted INTEGER,
    rows_flagged INTEGER,
    lineage_ok INTEGER,
    lineage_issues TEXT,
    output_schema TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pipeline ON pipeline_runs (pipeline, id DESC);
"""

# Terminal states a run never leaves once reached.
TERMINAL_STATUSES = {"succeeded", "failed", "timed_out", "interrupted"}
PENDING_STATUSES = {"queued", "running"}


class PipelineStore:
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
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "pipeline": row["pipeline"],
            "status": row["status"],
            "triggered_by": row["triggered_by"],
            "triggered_label": row["triggered_label"],
            "queued_at": row["queued_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "rows_written": row["rows_written"],
            "rows_deleted": row["rows_deleted"],
            "rows_flagged": row["rows_flagged"],
            "lineage_ok": bool(row["lineage_ok"]) if row["lineage_ok"] is not None else None,
            "lineage_issues": json.loads(row["lineage_issues"]) if row["lineage_issues"] else [],
            "output_schema": json.loads(row["output_schema"]) if row["output_schema"] else None,
            "error": row["error"],
        }

    def create_run(self, pipeline: str, triggered_by: Optional[int], triggered_label: str) -> dict:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO pipeline_runs (pipeline, status, triggered_by, triggered_label, queued_at) "
                "VALUES (?, 'queued', ?, ?, ?)",
                (pipeline, triggered_by, triggered_label, now),
            )
            run_id = cur.lastrowid
        return self.get_run(run_id)

    def mark_running(self, run_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE pipeline_runs SET status = 'running', started_at = ? WHERE id = ?",
                (self._now(), run_id),
            )

    def finish_run(
        self, run_id: int, status: str, *,
        rows_written: Optional[int] = None, rows_deleted: Optional[int] = None,
        rows_flagged: Optional[int] = None, lineage_ok: Optional[bool] = None,
        lineage_issues: Optional[list] = None, output_schema: Optional[list] = None,
        error: Optional[str] = None,
    ) -> dict:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"finish_run: status must be one of {TERMINAL_STATUSES}, got '{status}'")
        with self._conn() as conn:
            conn.execute(
                "UPDATE pipeline_runs SET status = ?, finished_at = ?, rows_written = ?, "
                "rows_deleted = ?, rows_flagged = ?, lineage_ok = ?, lineage_issues = ?, "
                "output_schema = ?, error = ? WHERE id = ?",
                (status, self._now(), rows_written, rows_deleted, rows_flagged,
                 (1 if lineage_ok else 0) if lineage_ok is not None else None,
                 json.dumps(lineage_issues) if lineage_issues else None,
                 json.dumps(output_schema) if output_schema else None,
                 error, run_id),
            )
        return self.get_run(run_id)

    def sweep_interrupted(self) -> int:
        """Terminally mark every run left queued/running (an app crash or
        restart mid-run) as interrupted. Called once at startup, before the
        job worker begins draining new triggers — no run is ever left
        looking perpetually in-flight (FR-015)."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE pipeline_runs SET status = 'interrupted', finished_at = ?, "
                "error = COALESCE(error, 'interrupted: app restarted while this run was pending') "
                "WHERE status IN ('queued', 'running')",
                (self._now(),),
            )
        return cur.rowcount

    def runs_for(self, pipeline: str, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_runs WHERE pipeline = ? ORDER BY id DESC LIMIT ?",
                (pipeline, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_run(self, run_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def pending_for(self, pipeline: str) -> Optional[dict]:
        """The pipeline's queued/running row, if any — backs the same-
        pipeline 409 on trigger (a different pipeline's trigger still queues
        platform-wide; only a *duplicate* trigger for this one is refused)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_runs WHERE pipeline = ? AND status IN ('queued', 'running') "
                "ORDER BY id DESC LIMIT 1",
                (pipeline,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def latest_for(self, pipeline: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_runs WHERE pipeline = ? ORDER BY id DESC LIMIT 1",
                (pipeline,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def latest_successful_schema(self, pipeline: str) -> Optional[list]:
        """The output_schema of the pipeline's most recent successful run —
        the lineage-suggest endpoint's fallback when the target doesn't
        exist yet (data-model.md `output_schema` / research U1)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT output_schema FROM pipeline_runs WHERE pipeline = ? AND status = 'succeeded' "
                "AND output_schema IS NOT NULL ORDER BY id DESC LIMIT 1",
                (pipeline,),
            ).fetchone()
        return json.loads(row["output_schema"]) if row and row["output_schema"] else None

    def next_queued(self) -> Optional[dict]:
        """The oldest still-queued run across every pipeline — what the FIFO
        worker picks up next (platform-wide serialization, FR-012)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_runs WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
            ).fetchone()
        return self._row_to_dict(row) if row else None
