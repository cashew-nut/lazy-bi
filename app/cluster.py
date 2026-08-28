"""What this process is, what else is running, and how the two stay in step.

Read the architecture doc's "Process model" first: this app was built as one
process on purpose, and most of that purpose survives scaling out. DuckDB is
an *embedded* engine — the process **is** the query engine — so adding
replicas adds real query capacity with no shared executor to contend on and
no coordinator to hand work to. That is the good news, and it is why the
scale-out story here is a coordination story rather than an engine one.

What does not survive is the set of shortcuts one process is entitled to
take. Four of them, and this module is the seam that removes each:

1. **"At most one pipeline runs, platform-wide."** Enforced by there being
   one FIFO worker thread — an argument that evaporates at two replicas,
   silently, while two runs materialize into the same bucket path. Now
   enforced by a lease on a lock named for the *target*
   (`pipeline_target:<path>`), so the guarantee holds however many workers
   exist, and independent targets stop blocking each other into the bargain.
2. **"A write I make is a write everyone sees."** `duck.invalidate()` drops
   the pins and cached bytes of the process that called it. Other replicas
   kept serving pre-write rows. Now every write bumps `DATA_GENERATION` and
   every replica's watcher invalidates on the next poll.
3. **"A model I save is a model everyone has."** `registry.reload_all()`
   rebuilds one process's in-memory registry from SQLite. A save on replica A
   was invisible on replica B, so a hard refresh that landed elsewhere
   reported the model missing. Now a save bumps `CONFIG_GENERATION` and every
   replica reloads.
4. **"If it needs doing once at boot, I do it."** Seeding the demo bucket and
   creating the bootstrap admin are first-run-only by a check-then-act that
   two replicas booting together both pass. Now they run under a boot lock.

Everything here is inert unless `CI_CLUSTERED` is set: unclustered, `lock()`
is a threading.Lock, the generations are process-local integers, and no
watcher thread starts. That is not a performance dodge — it is the promise
that the default deployment behaves exactly as it did before this module
existed, and that a bug in the coordination path cannot reach someone running
one container.
"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from . import config
from .clusterstore import ClusterStore, open_store

# The two things replicas watch each other for. Named constants because they
# are a cross-process contract: a bump written by one version of this app must
# mean the same thing to another, mid-rolling-deploy.
DATA_GENERATION = "data"        # the bucket's contents changed (pipeline run, upload, delete)
CONFIG_GENERATION = "config"    # models/bundles/pipelines/agents changed

# Held for the duration of first-run initialization so exactly one replica
# seeds the demo bucket, the bootstrap admin and the demo notebook.
BOOT_LOCK = "boot"

# How stale a heartbeat may be before `/api/cluster` stops counting a node as
# live, and how long a dead node's roster row survives. Multiples of the poll
# interval rather than fixed seconds, so tightening the poll tightens both.
_LIVE_MULTIPLE = 4
_PRUNE_MULTIPLE = 720           # ~1 hour at the default 5s poll

_lock = threading.Lock()
_store: Optional[ClusterStore] = None
# process-local generation values, used unclustered and as the "last seen"
# baseline when clustered — see observe()
_local: dict[str, int] = {}
_watcher: Optional[threading.Thread] = None
_stop = threading.Event()
_reactions: dict[str, list[Callable[[], None]]] = {}
# per-name locks for the unclustered path, so lock() means the same thing
# (mutual exclusion within the process) with or without a shared store
_thread_locks: dict[str, threading.Lock] = {}


def store() -> Optional[ClusterStore]:
    """The shared coordination store, or None when unclustered.

    Opened on first use rather than at import: `config.DB_PATH` is read at
    import time by the stores themselves, but a test that points it somewhere
    else does so before touching this."""
    global _store
    if not config.CLUSTERED:
        return None
    with _lock:
        if _store is None:
            _store = open_store()
        return _store


def reset() -> None:
    """Forget the store and every observed generation. Tests only — the
    request path never re-opens coordination state.

    Deliberately leaves `_reactions` alone. Those are registered at import by
    the modules that own the affected state (app/cache.py, app/duck.py,
    app/registry.py), so they are wiring, not state: clearing them here would
    silently unhook every replica-awareness this module exists to provide,
    for the rest of the process, and the only thing that would notice is a
    test that happened to run afterwards."""
    global _store
    stop_watcher()
    with _lock:
        _store = None
        _local.clear()
        _thread_locks.clear()


# ── change generations ───────────────────────────────────────────────────

def bump(name: str) -> int:
    """Record that `name`'s underlying state changed, and return the new
    generation.

    The caller is expected to have already applied the change to its *own*
    process — this marks it for everyone else. Unclustered the counter is
    still maintained (it costs one increment) so callers can key caches on it
    without branching, and so a single-process deployment exercises the same
    code path the clustered one does.
    """
    shared = store()
    if shared is None:
        with _lock:
            _local[name] = _local.get(name, 0) + 1
            return _local[name]
    value = shared.bump(name, config.NODE_ID)
    # Remember our own bump as already-applied: this process did the work
    # before calling, and a watcher that re-reacted to it would drop a cache
    # it just correctly rebuilt.
    with _lock:
        _local[name] = value
    return value


def generation(name: str) -> int:
    """`name`'s current cluster-wide generation."""
    shared = store()
    if shared is None:
        with _lock:
            return _local.get(name, 0)
    return shared.generation(name)


def observed(name: str) -> int:
    """The generation this process has already reacted to. Cheap — no store
    round trip — which is what makes it usable as a cache-key component on
    every lookup (see app/cache.py)."""
    with _lock:
        return _local.get(name, 0)


def on_change(name: str, reaction: Callable[[], None]) -> None:
    """Run `reaction` when some *other* process bumps `name`.

    Registered by the modules that own the affected state (app/duck.py's
    caches, app/registry.py's model registry) rather than being called from
    the watcher directly, so this module never has to know what a generation
    means — only that something changed.
    """
    _reactions.setdefault(name, []).append(reaction)


def observe(name: str, value: int) -> bool:
    """Mark `value` as applied for `name`. True if it moved — i.e. if this
    process was behind and the caller should react."""
    with _lock:
        if value <= _local.get(name, 0):
            return False
        _local[name] = value
        return True


def poll_once() -> list[str]:
    """One pass of the watcher: read every generation, fire the reactions for
    the ones that moved, return their names.

    Separated from the thread so a test can drive it directly and so a
    reaction that raises is contained to its own name — a model that fails to
    reload must not stop a cache invalidation registered after it."""
    shared = store()
    if shared is None:
        return []
    changed = []
    for name, value in shared.generations().items():
        if not observe(name, value):
            continue
        changed.append(name)
        for reaction in _reactions.get(name, []):
            try:
                reaction()
            except Exception as exc:                    # noqa: BLE001
                print(f"[cash-intel] cluster: reaction to '{name}' failed: {exc}")
    return changed


# ── leases ───────────────────────────────────────────────────────────────

class LockLost(RuntimeError):
    """A held lease expired and was taken by another node. Raised by
    `renew()`; work guarded by the lock must stop rather than continue
    unprotected."""


class Lease:
    """A held lock, and the two things a holder does with one: keep it, and
    check it is still held before doing something irreversible."""

    def __init__(self, name: str, token: Optional[str], ttl: float):
        self.name = name
        self.token = token
        self.ttl = ttl

    def renew(self) -> None:
        """Extend the lease. Raises LockLost if it is already gone.

        Callers renew from a side thread at roughly a third of the TTL — see
        `renewing()` — so one slow store round trip cannot cost the lock."""
        shared = store()
        if shared is None or self.token is None:
            return
        if not shared.renew(self.name, self.token, self.ttl):
            raise LockLost(f"lease on '{self.name}' expired and was taken by another node")

    def held(self) -> bool:
        try:
            self.renew()
            return True
        except LockLost:
            return False


@contextmanager
def lock(name: str, ttl: Optional[float] = None, wait: float = 0.0) -> Iterator[Optional[Lease]]:
    """Hold `name` exclusively across the cluster, or yield None if someone
    else has it.

    `wait` seconds of retrying before giving up; 0 means try once. Yielding
    None rather than raising is deliberate — every caller here has a sensible
    "someone else is doing it" branch (skip the seeding, leave the run
    queued for whoever holds the target), and an exception would turn that
    into error handling.

    Unclustered this is a plain per-name `threading.Lock`, so a caller's
    mutual-exclusion assumption holds identically whether or not a shared
    store is configured — the scope of "exclusive" is just narrower.
    """
    ttl = config.CLUSTER_LEASE_SECONDS if ttl is None else ttl
    shared = store()
    if shared is None:
        with _lock:
            local = _thread_locks.setdefault(name, threading.Lock())
        if not local.acquire(blocking=wait > 0, timeout=wait if wait > 0 else -1):
            yield None
            return
        try:
            yield Lease(name, None, ttl)
        finally:
            local.release()
        return

    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait
    while True:
        if shared.acquire(name, token, config.NODE_ID, ttl):
            break
        if time.monotonic() >= deadline:
            yield None
            return
        time.sleep(min(0.25, max(0.01, wait / 10)))
    lease = Lease(name, token, ttl)
    try:
        yield lease
    finally:
        shared.release(name, token)


@contextmanager
def renewing(lease: Optional[Lease],
             alongside: Optional[Callable[[], bool]] = None) -> Iterator[threading.Event]:
    """Keep `lease` alive on a side thread for the duration of the block, and
    yield an Event that is set if it is ever lost.

    The work a lease guards (a pipeline run) is a blocking subprocess call
    that cannot renew from the inside, and a lease that lapses mid-run means
    another node may already have started the same run — so the loser needs
    to find out, not just eventually fail. Callers check the event where they
    can still act on it: before writing a result, and on the way out.

    `alongside` renews whatever else shares this lease's lifetime, on the same
    tick, returning False if *it* was lost. A pipeline run has two such
    things — the lock on its target and the lease on its own run row — with
    identical holders and lifetimes; renewing them from one thread is what
    keeps them from drifting apart and disagreeing about who is running.
    """
    lost = threading.Event()
    if lease is None or lease.token is None:
        if alongside is None:
            yield lost
            return
    stop = threading.Event()

    def _keep() -> None:
        interval = max(1.0, (lease.ttl if lease else config.CLUSTER_LEASE_SECONDS) / 3)
        while not stop.wait(interval):
            try:
                if lease is not None:
                    lease.renew()
                if alongside is not None and not alongside():
                    lost.set()
                    return
            except LockLost:
                lost.set()
                return
            except Exception:                           # noqa: BLE001
                # a transient store error is not a lost lease; the next tick
                # retries, and the TTL is what ultimately decides
                pass

    thread = threading.Thread(target=_keep, daemon=True,
                              name=f"lease-{lease.name if lease else 'run'}")
    thread.start()
    try:
        yield lost
    finally:
        stop.set()
        thread.join(timeout=2)


# ── the watcher ──────────────────────────────────────────────────────────

def _watch() -> None:
    shared = store()
    if shared is None:
        return
    heartbeats = 0
    while not _stop.wait(config.CLUSTER_POLL_SECONDS):
        try:
            poll_once()
            shared.heartbeat(config.NODE_ID, config.ROLE)
            heartbeats += 1
            if heartbeats % _PRUNE_MULTIPLE == 0:
                shared.prune_nodes(config.CLUSTER_POLL_SECONDS * _PRUNE_MULTIPLE)
        except Exception as exc:                        # noqa: BLE001
            # The watcher must outlive a store hiccup: dying here would leave
            # this replica silently serving stale data with no error anywhere.
            print(f"[cash-intel] cluster: watcher pass failed: {exc}")


def start_watcher() -> bool:
    """Start following other nodes' changes. No-op (returns False)
    unclustered, where there are no other nodes to follow."""
    global _watcher
    shared = store()
    if shared is None or _watcher is not None:
        return False
    # Adopt the current generations without reacting: this process has just
    # loaded everything from scratch, so it is already up to date, and firing
    # every reaction on the first pass would re-read the whole registry and
    # drop a cold cache for nothing.
    for name, value in shared.generations().items():
        observe(name, value)
    shared.heartbeat(config.NODE_ID, config.ROLE)
    _stop.clear()
    _watcher = threading.Thread(target=_watch, daemon=True, name="cluster-watch")
    _watcher.start()
    return True


def stop_watcher() -> None:
    global _watcher
    _stop.set()
    if _watcher is not None:
        _watcher.join(timeout=2)
        _watcher = None
    shared = _store
    if shared is not None:
        try:
            shared.forget(config.NODE_ID)
        except Exception:                               # noqa: BLE001
            pass


def status() -> dict:
    """What `/api/cluster` reports: this node, its peers, the generations
    everyone is following, and what is currently locked."""
    shared = store()
    info = {
        "clustered": shared is not None,
        "node_id": config.NODE_ID,
        "role": config.ROLE,
        "serves_http": config.SERVES_HTTP,
        "runs_pipelines": config.RUNS_PIPELINES,
        "poll_seconds": config.CLUSTER_POLL_SECONDS,
        "lease_seconds": config.CLUSTER_LEASE_SECONDS,
        "observed": dict(_local),
    }
    if shared is None:
        info.update({"nodes": [], "generations": {}, "locks": []})
        return info
    info.update({
        "nodes": shared.nodes(config.CLUSTER_POLL_SECONDS * _LIVE_MULTIPLE),
        "generations": shared.generations(),
        "locks": shared.locks(),
    })
    return info


# ── preflight ────────────────────────────────────────────────────────────

def preflight() -> list[str]:
    """Configuration that cannot survive a second replica, as a list of
    problems (empty when the config is sound).

    Every one of these fails *silently* otherwise, which is the reason this
    exists rather than a documentation note. A per-replica embedded S3
    emulator does not error — it answers, with different data per replica, so
    the demo catalog appears to flicker. A DuckDB file path shared over a
    volume does not error either; two processes open it and the second one
    gets a lock error at some unpredictable later moment.
    """
    problems = []
    if config.EMBEDDED_EMULATOR:
        problems.append(
            "the demo bucket is served by this process's own in-memory S3 emulator "
            "(CI_DEMO_S3_ENDPOINT is the built-in loopback address), so every replica "
            "would serve a different demo bucket — point CI_DEMO_S3_ENDPOINT at a shared "
            "endpoint (MinIO, LocalStack, a real bucket) or set CI_DEMO=0")
    if config.DUCKDB_PATH != ":memory:":
        problems.append(
            f"CI_DUCKDB_PATH is a file ({config.DUCKDB_PATH}); DuckDB takes an exclusive "
            "lock on its database file, so a second replica sharing that path fails to "
            "open it — leave it unset (:memory:) when running more than one replica")
    return problems


def warnings() -> list[str]:
    """Configuration that works but will not keep working — reported at
    start, never fatal."""
    notes = []
    if config.CLUSTERED and config.LOCAL_DATA_DIR and config.SERVES_HTTP:
        notes.append(
            f"uploaded datasets are cached on local disk ({config.LOCAL_DATA_DIR}); "
            "replicas need that path on shared storage, or an upload restored after a "
            "restart will exist on one replica only")
    return notes
