"""Shared-store coordination primitives: leases, change generations, and a
node roster.

This is the persistence half of app/cluster.py — three small tables and the
handful of statements that make them atomic. It exists because scaling this
app out horizontally is not, mostly, a matter of running more copies: it is a
matter of the assumptions one copy is allowed to make. Three of them matter,
and each maps to one table here:

  - **"I am the only writer."** A pipeline run materializes into a shared
    bucket path, and the old design guaranteed one-at-a-time by there being
    exactly one worker thread in exactly one process. Two replicas is two
    threads and no guarantee at all. `cluster_locks` moves that guarantee
    into the store, where the number of processes doesn't change the answer.
  - **"My caches are the only caches."** A write to the bucket invalidated
    DuckDB's pins and the TTL cache *of the process that did the write*. A
    second replica went on serving pre-write rows until its TTL lapsed.
    `cluster_generations` is a counter every replica watches, so one node's
    write is every node's invalidation.
  - **"My registry is the whole truth."** Same shape, different state: a
    model saved through replica A lived in A's memory until A reloaded, so a
    request load-balanced to B reported it missing. The same counter,
    under a different name, drives B's reload.

`cluster_nodes` is the fourth thing scaling costs you — knowing what is
running — and is observability only; nothing depends on it being accurate.

**Time comes from the database, never from a process.** Every expiry
comparison is evaluated inside a statement against the store's own clock, so
two hosts with drifting clocks still agree on whether a lease has expired.
That is the one property that makes leases safe across machines, and it is
why the timestamps here are computed in SQL rather than passed in as
parameters like the rest of this codebase's `_now()` columns.

**Portability is deliberate.** The statements below use the SQL that SQLite
and PostgreSQL spell identically (`INSERT … ON CONFLICT … DO UPDATE … WHERE`);
the only dialect-specific fragments are the two clock expressions, isolated
in `_NOW` / `_expiry()`. Swapping this class for a Postgres one — the move a
multi-*host* deployment needs anyway, since SQLite's locking does not survive
a network filesystem — is a change to those two constants and the connection
helper, not to the logic. See docs/scaling.md.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from . import sqlitedb

SCHEMA = """
CREATE TABLE IF NOT EXISTS cluster_generations (
    name TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    bumped_at TEXT NOT NULL,
    bumped_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cluster_locks (
    name TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    node_id TEXT NOT NULL,
    fence INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cluster_nodes (
    node_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
"""

# The store's own clock, as a sortable UTC string. Millisecond precision and a
# fixed width, so `expires_at <= _NOW` is a plain string comparison that
# orders the same way the instants do — no parsing in the hot path, and an
# index on it would still work.
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def _expiry(seconds: float) -> str:
    """SQL for "now + `seconds`", evaluated by the store.

    The modifier is formatted in rather than bound because SQLite will not
    take a bound parameter as a `strftime` modifier. `seconds` is a float
    this module's callers control (a config value), never request input, and
    it is rendered through `%f` — there is no path for anything but a number
    to reach the statement.
    """
    return f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '{float(seconds):+f} seconds')"


class ClusterStore:
    """Coordination state for one cluster, on one database."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlitedb.connect(self.db_path)

    # ── change generations ───────────────────────────────────────────────

    def generation(self, name: str) -> int:
        """The current value of counter `name`; 0 if nobody has bumped it."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT generation FROM cluster_generations WHERE name = ?", (name,)
            ).fetchone()
        return int(row["generation"]) if row else 0

    def generations(self) -> dict[str, int]:
        """Every counter at once — one round trip for a watcher that follows
        several, and the shape `/api/cluster` reports."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, generation FROM cluster_generations").fetchall()
        return {r["name"]: int(r["generation"]) for r in rows}

    def bump(self, name: str, node_id: str) -> int:
        """Increment counter `name` and return its new value.

        Read-back rather than `RETURNING` so the statement runs on any SQLite
        old enough to be in a base image, and inside the same transaction so
        a concurrent bump can't hand this caller a value it never held.
        Racing bumps are fine either way: this is a change *signal*, and two
        overlapping changes that land on one increment still tell every
        watcher to re-derive.
        """
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO cluster_generations (name, generation, bumped_at, bumped_by) "
                f"VALUES (?, 1, {_NOW}, ?) "
                f"ON CONFLICT(name) DO UPDATE SET "
                f"  generation = cluster_generations.generation + 1, "
                f"  bumped_at = {_NOW}, bumped_by = excluded.bumped_by",
                (name, node_id),
            )
            row = conn.execute(
                "SELECT generation FROM cluster_generations WHERE name = ?", (name,)
            ).fetchone()
        return int(row["generation"])

    # ── leases ───────────────────────────────────────────────────────────

    def acquire(self, name: str, token: str, node_id: str, ttl: float) -> bool:
        """Take lock `name` for `ttl` seconds under `token`, or return False.

        One statement, so "is it free?" and "take it" cannot be separated by
        another process. A lock is free when no row exists or the existing
        row's lease has expired against the *store's* clock — a holder that
        died without releasing therefore blocks others for at most `ttl`,
        and a holder that is alive keeps it by renewing.

        `token` is per-acquisition, not per-node: a node that restarts under
        the same name, or a second thread inside one process, is a different
        holder and must wait, exactly like any other contender.
        """
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO cluster_locks (name, token, node_id, fence, acquired_at, expires_at) "
                f"VALUES (?, ?, ?, 1, {_NOW}, {_expiry(ttl)}) "
                f"ON CONFLICT(name) DO UPDATE SET "
                f"  token = excluded.token, node_id = excluded.node_id, "
                f"  fence = cluster_locks.fence + 1, "
                f"  acquired_at = {_NOW}, expires_at = {_expiry(ttl)} "
                f"WHERE cluster_locks.expires_at <= {_NOW}",
                (name, token, node_id),
            )
            return cur.rowcount > 0

    def renew(self, name: str, token: str, ttl: float) -> bool:
        """Extend a lease this caller still holds. False means it was lost —
        expired and taken by someone else — and the work it was guarding must
        stop rather than carry on unprotected."""
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE cluster_locks SET expires_at = {_expiry(ttl)} "
                f"WHERE name = ? AND token = ? AND expires_at > {_NOW}",
                (name, token),
            )
            return cur.rowcount > 0

    def release(self, name: str, token: str) -> None:
        """Give up a lock, by expiring it rather than deleting the row.

        Scoped to the token so a caller whose lease already lapsed — and whose
        lock another node has since taken — cannot release someone else's hold
        on its way out. Expiring rather than deleting is what keeps `fence`
        monotonic: a deleted row's next `acquire` would re-insert at 1, and a
        fencing token that can go backwards is not one. The row costs nothing
        (there is one per lock *name*, not per acquisition) and `locks()`
        filters expired ones out.
        """
        with self._conn() as conn:
            conn.execute(
                f"UPDATE cluster_locks SET expires_at = {_NOW} "
                f"WHERE name = ? AND token = ?", (name, token))

    def locks(self) -> list[dict]:
        """Every live lock, for `/api/cluster`. Expired rows are filtered
        rather than deleted: reaping is `acquire`'s job, and a lock nobody
        contends for is harmless left behind."""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT name, node_id, fence, acquired_at, expires_at FROM cluster_locks "
                f"WHERE expires_at > {_NOW} ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    # ── node roster ──────────────────────────────────────────────────────

    def heartbeat(self, node_id: str, role: str) -> None:
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO cluster_nodes (node_id, role, started_at, last_seen) "
                f"VALUES (?, ?, {_NOW}, {_NOW}) "
                f"ON CONFLICT(node_id) DO UPDATE SET role = excluded.role, last_seen = {_NOW}",
                (node_id, role),
            )

    def nodes(self, within_seconds: float) -> list[dict]:
        """Nodes that have heartbeat within `within_seconds`. A node that
        stopped is not deleted here — `forget` does that on a clean shutdown,
        and a crashed one simply ages out of this window."""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT node_id, role, started_at, last_seen FROM cluster_nodes "
                f"WHERE last_seen > {_expiry(-within_seconds)} ORDER BY node_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def forget(self, node_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM cluster_nodes WHERE node_id = ?", (node_id,))

    def prune_nodes(self, older_than_seconds: float) -> int:
        """Drop roster rows for nodes long gone, so a deployment that rolls
        daily doesn't accumulate one row per container forever."""
        with self._conn() as conn:
            cur = conn.execute(
                f"DELETE FROM cluster_nodes WHERE last_seen <= {_expiry(-older_than_seconds)}")
        return cur.rowcount


def open_store(db_path: Optional[Path] = None) -> ClusterStore:
    """The cluster store on the platform database, or on `db_path` in tests."""
    from . import config

    return ClusterStore(db_path or config.DB_PATH)
