"""SQLite persistence for pipeline runs (specs/014-polars-pipeline-module/).

Append-only history: rows are created queued, transition through the
lifecycle, and are never deleted (a deleted pipeline's run history is
retained — see data-model.md).

The table is also the *queue*, not just its history. That distinction only
started to matter with more than one process: the in-memory FIFO queue in
app/pipeline_jobs.py was the real queue, and this table recorded what it
did — an arrangement in which "at most one run at a time" was guaranteed by
there being one worker thread, and which therefore guarantees nothing once a
second replica exists. `claim_next_run` moves the queue itself here, where
one atomic UPDATE decides who runs what: a run is claimed by exactly one
worker, whatever number of workers ask, because the store says so.

Claims carry a lease (`claimed_by`, `lease_expires_at`). A worker that dies
mid-run cannot mark its own run interrupted, so the lease is how the rest of
the cluster learns it is gone — see `sweep_expired`, and note what it
replaced: a blanket "mark everything pending as interrupted" at startup,
which with a second replica would have reaped a *live* peer's run every time
anything restarted."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import sqlitedb

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
    error TEXT,
    claimed_by TEXT,
    lease_expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pipeline ON pipeline_runs (pipeline, id DESC);
-- the claim query's whole predicate: oldest queued run first
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_queued ON pipeline_runs (status, id);
"""

# The store's own clock, for lease comparisons — the same reasoning (and the
# same expressions) as app/clusterstore.py: two workers on two hosts must
# agree on whether a lease has lapsed, and only one clock can decide that.
# Every other timestamp in this table stays a Python-side `_now()`, because
# it is a record of when something happened rather than a value anything
# compares against.
_NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def _expiry_sql(seconds: float) -> str:
    """SQL for "now + `seconds`", or NULL when `seconds` is zero — the
    unclustered case, where nothing renews and nothing sweeps."""
    if not seconds:
        return "NULL"
    return f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '{float(seconds):+f} seconds')"


# Terminal states a run never leaves once reached.
TERMINAL_STATUSES = {"succeeded", "failed", "timed_out", "interrupted"}
PENDING_STATUSES = {"queued", "running"}


class PipelineStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Guarded so an existing database upgrades in place, same shape as
            # app/store.py's own column migrations. A pre-lease row simply has
            # both NULL, which reads as "queued and unclaimed" / "running with
            # no lease" — and the latter is exactly what sweep_expired reaps.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_runs)")}
            if "claimed_by" not in cols:
                conn.execute("ALTER TABLE pipeline_runs ADD COLUMN claimed_by TEXT")
            if "lease_expires_at" not in cols:
                conn.execute("ALTER TABLE pipeline_runs ADD COLUMN lease_expires_at TEXT")

    def _conn(self) -> sqlite3.Connection:
        return sqlitedb.connect(self.db_path)

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
            "claimed_by": row["claimed_by"],
            "lease_expires_at": row["lease_expires_at"],
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

    def mark_running(self, run_id: int, claimed_by: str = "", lease_seconds: float = 0) -> bool:
        """Move a specific queued run to running under `claimed_by`, if it is
        still queued. False means someone else already took it.

        The `WHERE status = 'queued'` is the whole point: it makes the check
        and the transition one statement, so two workers reading the same
        queued row both try and exactly one succeeds. A worker that gets
        False re-polls rather than treating it as an error — losing a race is
        the normal outcome of two workers being healthy at once.
        """
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE pipeline_runs SET status = 'running', started_at = ?, "
                f"claimed_by = ?, lease_expires_at = {_expiry_sql(lease_seconds)} "
                f"WHERE id = ? AND status = 'queued'",
                (self._now(), claimed_by or None, run_id),
            )
            return cur.rowcount > 0

    def claim_next_run(self, claimed_by: str, lease_seconds: float) -> Optional[dict]:
        """Take the oldest queued run for this worker, or None if there is
        none to take.

        Read-then-claim rather than a single `UPDATE … ORDER BY … LIMIT 1`,
        which SQLite only supports with a compile-time option that is off in
        most builds. The claim itself is still atomic (see `mark_running`), so
        the race a second worker can win is "read the same row", not "run the
        same run": the loser's UPDATE matches nothing and it tries the next
        one. The loop bounds itself on the queue it read, so a burst of
        contention costs a few extra statements, never a spin.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM pipeline_runs WHERE status = 'queued' ORDER BY id LIMIT 20"
            ).fetchall()
        for row in rows:
            if self.mark_running(row["id"], claimed_by, lease_seconds):
                return self.get_run(row["id"])
        return None

    def requeue_run(self, run_id: int, claimed_by: str) -> bool:
        """Hand a claimed run back to the queue, unstarted.

        The claim-then-lock order in app/pipeline_jobs.py means a worker can
        claim a run it turns out it must not execute yet — another worker
        holds the target's lock, or this node's registry hasn't caught up to
        a pipeline created on a peer. Neither is a failure of the run, so it
        goes back to `queued` with its claim and `started_at` cleared, exactly
        as it was created, and the next poll picks it up.

        Scoped to `claimed_by` so a worker can only requeue its own claim.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE pipeline_runs SET status = 'queued', started_at = NULL, "
                "claimed_by = NULL, lease_expires_at = NULL "
                "WHERE id = ? AND status = 'running' AND claimed_by = ?",
                (run_id, claimed_by),
            )
            return cur.rowcount > 0

    def renew_lease(self, run_id: int, claimed_by: str, lease_seconds: float) -> bool:
        """Push a running run's lease out. False means the run is no longer
        this worker's — swept as expired, or already finished."""
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE pipeline_runs SET lease_expires_at = {_expiry_sql(lease_seconds)} "
                f"WHERE id = ? AND status = 'running' AND claimed_by = ?",
                (run_id, claimed_by),
            )
            return cur.rowcount > 0

    def sweep_expired(self) -> int:
        """Mark as interrupted every run whose worker has stopped renewing.

        This is what `sweep_interrupted` became. The old version marked
        *every* queued-or-running row interrupted at startup, on the reasoning
        that if this process is starting then nothing can be running — true of
        exactly one process, and actively destructive with two: a restarting
        replica would declare a peer's in-flight run dead and, worse, leave
        every genuinely queued run unrunnable.

        The lease answers the same question without assuming anything about
        who else exists. A row is reaped only when its lease has lapsed
        against the store's own clock, which a live worker prevents by
        renewing. Queued rows are untouched — an unclaimed run is not a lost
        one, it is one nobody has picked up yet, and the next worker to poll
        will.

        A pre-lease `running` row (NULL lease, written before this column
        existed) is reaped too: nothing is renewing it, and it is the exact
        case — a crash mid-run — the sweep is for.
        """
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE pipeline_runs SET status = 'interrupted', finished_at = ?, "
                f"error = COALESCE(error, 'interrupted: the worker running this stopped "
                f"responding and its lease expired') "
                f"WHERE status = 'running' "
                f"AND (lease_expires_at IS NULL OR lease_expires_at <= {_NOW_SQL})",
                (self._now(),),
            )
        return cur.rowcount

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

    def sweep_interrupted(self, claimed_by: Optional[str] = None) -> int:
        """Terminally mark runs this worker had in flight as interrupted —
        the startup half of FR-015: a run must never be left looking
        perpetually in-flight after a crash.

        Scoped to `claimed_by` when given, which is what makes it safe to
        call at the start of every replica: a restarting worker reaps the
        runs *it* left behind (it holds the same node id, and nothing else
        can be running under that id — it is only just starting), and leaves
        peers' runs to their own leases and `sweep_expired`. With no
        `claimed_by` it keeps the original whole-table behaviour, which is
        still correct for the single-process default and is what an
        unclustered start uses.

        Queued rows are left alone when scoped: an unclaimed run belongs to
        the queue, not to whoever restarted.
        """
        with self._conn() as conn:
            if claimed_by is None:
                cur = conn.execute(
                    "UPDATE pipeline_runs SET status = 'interrupted', finished_at = ?, "
                    "error = COALESCE(error, 'interrupted: app restarted while this run was pending') "
                    "WHERE status IN ('queued', 'running')",
                    (self._now(),),
                )
            else:
                cur = conn.execute(
                    "UPDATE pipeline_runs SET status = 'interrupted', finished_at = ?, "
                    "error = COALESCE(error, 'interrupted: the worker running this restarted') "
                    "WHERE status = 'running' AND claimed_by = ?",
                    (self._now(), claimed_by),
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
