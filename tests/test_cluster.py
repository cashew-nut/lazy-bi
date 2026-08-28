"""The coordination layer (app/cluster.py, app/clusterstore.py) and the two
things that depend on it: the pipeline run claim, and cross-replica
invalidation.

Every test here exercises the *multi-process* case with two store objects on
one database file — which is exactly what two replicas sharing a volume are,
minus the process boundary. What it cannot exercise (and what docs/scaling.md
is explicit about) is a shared filesystem's locking, which is why multi-host
wants Postgres behind these same classes.
"""
import threading
import uuid

import pytest

# Imported at module scope on purpose: app/cache.py registers its
# DATA_GENERATION reaction as an import side effect, and a test that imported
# it lazily would be asserting on wiring its own import had just installed.
from app import cache, cluster, config
from app.clusterstore import ClusterStore
from app.pipelinestore import PipelineStore


@pytest.fixture
def db(tmp_path):
    return tmp_path / "cluster.db"


@pytest.fixture
def two_nodes(db):
    """Two ClusterStores on one database — replica A and replica B."""
    return ClusterStore(db), ClusterStore(db)


@pytest.fixture
def clustered(monkeypatch, db):
    """Put app/cluster.py itself into clustered mode against `db`, and undo it
    afterwards so no other test inherits a shared store.

    Reactions are snapshotted and restored rather than cleared: the ones
    registered at import (app/cache.py's, and app/duck.py's when duckdb is
    loaded) are the wiring under test, and a test that adds its own must not
    leave it behind for the next one."""
    monkeypatch.setattr(config, "CLUSTERED", True)
    monkeypatch.setattr(config, "DB_PATH", db)
    before = {name: list(fns) for name, fns in cluster._reactions.items()}
    cluster.reset()
    yield
    cluster.reset()
    cluster._reactions.clear()
    cluster._reactions.update(before)
    monkeypatch.setattr(config, "CLUSTERED", False)


# ── leases ───────────────────────────────────────────────────────────────

def test_only_one_node_holds_a_lock(two_nodes):
    a, b = two_nodes
    assert a.acquire("target", "tok-a", "A", 60)
    assert not b.acquire("target", "tok-b", "B", 60)


def test_a_lease_that_expires_is_takeable(two_nodes):
    a, b = two_nodes
    assert a.acquire("target", "tok-a", "A", -1)     # already expired
    assert b.acquire("target", "tok-b", "B", 60)


def test_renewing_keeps_a_lock_and_a_lost_one_reports_it(two_nodes):
    a, b = two_nodes
    a.acquire("target", "tok-a", "A", 60)
    assert a.renew("target", "tok-a", 60)
    assert not b.renew("target", "tok-b", 60)        # never held it


def test_release_is_scoped_to_the_holder(two_nodes):
    a, b = two_nodes
    a.acquire("target", "tok-a", "A", 60)
    b.release("target", "tok-b")                     # B was never the holder
    assert not b.acquire("target", "tok-b", "B", 60)
    a.release("target", "tok-a")
    assert b.acquire("target", "tok-b", "B", 60)


def test_fence_never_goes_backwards_across_a_release(two_nodes):
    """A fencing token is only useful monotonic — which is why release expires
    the row rather than deleting it."""
    a, b = two_nodes
    a.acquire("t", "tok-a", "A", 60)
    first = a.locks()[0]["fence"]
    a.release("t", "tok-a")
    b.acquire("t", "tok-b", "B", 60)
    assert b.locks()[0]["fence"] > first


def test_concurrent_acquire_has_exactly_one_winner(db):
    """The property the whole design rests on: however many workers ask at
    once, the store hands the lock to one."""
    winners = []
    barrier = threading.Barrier(8)

    def contend():
        store = ClusterStore(db)
        barrier.wait()
        if store.acquire("hot", uuid.uuid4().hex, "node", 60):
            winners.append(1)

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_cluster_lock_yields_none_when_held_elsewhere(clustered):
    with cluster.lock("busy") as first:
        assert first is not None
        # a "second node" holding the same store — the context manager's own
        # token differs, so this is the real contention path
        with cluster.lock("busy") as second:
            assert second is None


def test_cluster_lock_is_mutual_exclusion_unclustered():
    """Unclustered, lock() is a threading.Lock — the scope is narrower but the
    guarantee a caller relies on is the same."""
    with cluster.lock("x") as first:
        assert first is not None
        with cluster.lock("x") as second:
            assert second is None


# ── change generations ───────────────────────────────────────────────────

def test_a_peers_bump_is_seen_and_reacted_to(clustered):
    fired = []
    cluster.on_change(cluster.DATA_GENERATION, lambda: fired.append(1))
    cluster.start_watcher()          # adopts the current generations, fires nothing
    assert fired == []

    # a peer bumps, directly against the store — this process never called bump()
    ClusterStore(config.DB_PATH).bump(cluster.DATA_GENERATION, "peer")
    assert cluster.poll_once() == [cluster.DATA_GENERATION]
    assert fired == [1]

    # and nothing re-fires while the generation sits still
    assert cluster.poll_once() == []
    assert fired == [1]


def test_a_nodes_own_bump_does_not_fire_its_own_reaction(clustered):
    """The bumping process has already applied the change; reacting to itself
    would drop a cache it just correctly rebuilt."""
    fired = []
    cluster.on_change(cluster.DATA_GENERATION, lambda: fired.append(1))
    cluster.bump(cluster.DATA_GENERATION)
    assert cluster.poll_once() == []
    assert fired == []


def test_a_failing_reaction_does_not_stop_the_next_one(clustered):
    fired = []

    def boom():
        raise RuntimeError("reload failed")

    cluster.on_change(cluster.DATA_GENERATION, boom)
    cluster.on_change(cluster.DATA_GENERATION, lambda: fired.append(1))
    ClusterStore(config.DB_PATH).bump(cluster.DATA_GENERATION, "peer")
    cluster.poll_once()
    assert fired == [1]


def test_generations_are_local_counters_when_unclustered():
    before = cluster.observed(cluster.DATA_GENERATION)
    assert cluster.bump(cluster.DATA_GENERATION) == before + 1
    assert cluster.poll_once() == []          # no store, nothing to watch


def test_cache_clears_on_a_peers_data_bump(clustered):
    """The wiring, not just the primitive: app/cache.py registers clear() as a
    DATA_GENERATION reaction at import, so one replica's bucket write empties
    every other replica's derived answers."""
    cache.clear()
    assert cache.get_or_set("k", 60.0, lambda: "first") == "first"
    ClusterStore(config.DB_PATH).bump(cluster.DATA_GENERATION, "peer")
    cluster.poll_once()
    assert cache.get_or_set("k", 60.0, lambda: "second") == "second"


# ── node roster ──────────────────────────────────────────────────────────

def test_nodes_report_within_the_liveness_window(two_nodes):
    a, b = two_nodes
    a.heartbeat("web-1", "web")
    b.heartbeat("worker-1", "worker")
    assert {n["node_id"] for n in a.nodes(60)} == {"web-1", "worker-1"}
    assert {n["role"] for n in a.nodes(60)} == {"web", "worker"}
    assert a.nodes(-1) == []                  # nothing is that fresh


def test_a_stopped_node_is_forgotten(two_nodes):
    a, b = two_nodes
    a.heartbeat("web-1", "web")
    a.forget("web-1")
    assert b.nodes(60) == []


# ── the pipeline run claim ───────────────────────────────────────────────

def test_a_queued_run_is_claimed_by_exactly_one_worker(db):
    store = PipelineStore(db)
    store.create_run("sales_daily", None, "test")
    assert store.claim_next_run("A", 60)["claimed_by"] == "A"
    assert store.claim_next_run("B", 60) is None


def test_concurrent_workers_claim_disjoint_runs(db):
    """Two workers, three runs, no run executed twice — the invariant the old
    single-thread FIFO gave for free and a cluster has to earn."""
    store = PipelineStore(db)
    for _ in range(3):
        store.create_run("sales_daily", None, "test")

    claimed = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(node):
        own = PipelineStore(db)
        barrier.wait()
        while True:
            run = own.claim_next_run(node, 60)
            if run is None:
                return
            with lock:
                claimed.append(run["id"])

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == [1, 2, 3]
    assert len(set(claimed)) == 3


def test_requeue_returns_a_run_to_the_queue_unstarted(db):
    store = PipelineStore(db)
    store.create_run("sales_daily", None, "test")
    run = store.claim_next_run("A", 60)
    assert store.requeue_run(run["id"], "A")
    back = store.get_run(run["id"])
    assert (back["status"], back["claimed_by"], back["started_at"]) == ("queued", None, None)
    assert store.claim_next_run("B", 60)["id"] == run["id"]


def test_requeue_is_scoped_to_the_claiming_worker(db):
    store = PipelineStore(db)
    store.create_run("sales_daily", None, "test")
    run = store.claim_next_run("A", 60)
    assert not store.requeue_run(run["id"], "B")
    assert store.get_run(run["id"])["status"] == "running"


def test_sweep_expired_reaps_a_dead_workers_run_and_spares_a_live_one(db):
    store = PipelineStore(db)
    store.create_run("dead", None, "test")
    store.create_run("live", None, "test")
    dead = store.claim_next_run("A", -1)          # lease already lapsed
    live = store.claim_next_run("B", 60)

    assert store.sweep_expired() == 1
    assert store.get_run(dead["id"])["status"] == "interrupted"
    assert store.get_run(live["id"])["status"] == "running"


def test_sweep_expired_leaves_queued_runs_alone(db):
    """An unclaimed run is not a lost one — it is one nobody has picked up."""
    store = PipelineStore(db)
    run = store.create_run("sales_daily", None, "test")
    assert store.sweep_expired() == 0
    assert store.get_run(run["id"])["status"] == "queued"


def test_scoped_startup_sweep_spares_a_peers_live_run(db):
    """The bug the old blanket sweep would have shipped: replica C restarting
    must not declare replica B's in-flight run interrupted, nor drain the
    queue on its way past."""
    store = PipelineStore(db)
    store.create_run("mine", None, "test")
    store.create_run("theirs", None, "test")
    queued = store.create_run("nobody's", None, "test")
    mine = store.claim_next_run("C", 60)
    theirs = store.claim_next_run("B", 60)

    assert store.sweep_interrupted("C") == 1
    assert store.get_run(mine["id"])["status"] == "interrupted"
    assert store.get_run(theirs["id"])["status"] == "running"
    assert store.get_run(queued["id"])["status"] == "queued"


def test_unscoped_startup_sweep_keeps_its_single_process_behaviour(db):
    """Unclustered, "if I am starting, nothing is running" is still true, and
    still what recovers a queue left behind by a crash."""
    store = PipelineStore(db)
    running = store.create_run("a", None, "test")
    queued = store.create_run("b", None, "test")
    store.claim_next_run("solo", 0)

    assert store.sweep_interrupted() == 2
    assert store.get_run(running["id"])["status"] == "interrupted"
    assert store.get_run(queued["id"])["status"] == "interrupted"


def test_an_unclustered_claim_stores_no_lease(db):
    """Zero seconds writes NULL: nothing is watching a single process's claim
    mid-flight, and sweep_expired must not invent a deadline for it... except
    on the very next start, which is when a crashed run is meant to be reaped."""
    store = PipelineStore(db)
    store.create_run("solo", None, "test")
    run = store.claim_next_run("solo-node", 0)
    assert run["lease_expires_at"] is None
    assert store.sweep_expired() == 1          # NULL lease == nothing renewing it


# ── preflight ────────────────────────────────────────────────────────────

def test_preflight_rejects_a_per_replica_demo_emulator(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDED_EMULATOR", True)
    monkeypatch.setattr(config, "DUCKDB_PATH", ":memory:")
    problems = cluster.preflight()
    assert len(problems) == 1
    assert "emulator" in problems[0]


def test_preflight_rejects_a_shared_duckdb_file(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDED_EMULATOR", False)
    monkeypatch.setattr(config, "DUCKDB_PATH", "/data/duck.db")
    problems = cluster.preflight()
    assert len(problems) == 1
    assert "CI_DUCKDB_PATH" in problems[0]


def test_preflight_passes_a_sound_configuration(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDED_EMULATOR", False)
    monkeypatch.setattr(config, "DUCKDB_PATH", ":memory:")
    assert cluster.preflight() == []


def test_status_reports_the_role_and_peers(clustered):
    cluster.start_watcher()
    status = cluster.status()
    assert status["clustered"] is True
    assert status["node_id"] == config.NODE_ID
    assert status["role"] == config.ROLE
    assert [n["node_id"] for n in status["nodes"]] == [config.NODE_ID]
