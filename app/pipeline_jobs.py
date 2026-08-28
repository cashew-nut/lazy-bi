"""The pipeline run worker (specs/014-polars-pipeline-module/): a daemon
thread drains queued runs, spawning each in its own subprocess
(app/pipeline_runner.py) so a hard timeout can actually be enforced — a plain
thread cannot be killed — and a runaway query or infinite loop never takes
the app down.

**What changed for horizontal scaling.** The original guarantee was "at most
one run executes platform-wide at any moment, enforced simply by there being
one consumer thread pulling from one queue". That enforcement is a property
of the *deployment*, not of the code: run a second replica and there are two
threads, two queues, and no guarantee — two runs materializing into the same
bucket path at the same time, silently, with the loser's rows or the winner's
depending on how the writes interleave. It is the single most dangerous thing
about scaling this app out, precisely because nothing errors.

So the guarantee now lives where the number of processes cannot change it:

  - **The queue is the `pipeline_runs` table**, not the in-process
    `queue.Queue`. A run is claimed by one atomic UPDATE
    (`PipelineStore.claim_next_run`), so exactly one worker gets it however
    many ask. The in-process queue survives as a *doorbell* — it wakes this
    thread immediately when the run was triggered on this same process, which
    is the common case and saves a poll interval of latency. A run triggered
    on another replica is found by the poll.
  - **Mutual exclusion is per target, not global.** A run holds a cluster
    lock named for the path it writes (`pipeline_target:<path>`) for as long
    as it runs. That is the actual invariant — two runs writing the same
    target must not overlap — and it is strictly stronger than the old
    global serialization where it matters (it holds across processes) and
    deliberately weaker where it did not (two pipelines writing different
    targets now run concurrently, on different workers).
  - **A dead worker is detected, not assumed.** The claim carries a lease
    that this thread renews while the subprocess runs. A worker that is
    killed mid-run stops renewing and its run is swept to `interrupted` by
    whoever notices; a worker that is merely slow keeps its claim.

Unclustered — the default — all three collapse back to the original
behaviour: one process, one worker, an uncontended lock, and a claim nobody
races for.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from . import cache, cluster, config, duck, semantic
from . import pipelines as pipelines_mod
from .pipelines import Pipeline
from .pipelinestore import PipelineStore

_queue: "queue.Queue[int]" = queue.Queue()
_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _pipeline_job_spec(pipeline: Pipeline) -> dict:
    """The subset of a Pipeline the runner subprocess needs — see
    contracts/pipelines-api.md's runner protocol. Built directly from the
    dataclasses (not Pipeline.to_public(), which omits `sql` — that summary
    is for the list API, not execution)."""
    return {
        "name": pipeline.name,
        "sources": [
            {"name": s.name, "format": s.format, "path": s.path}
            for s in pipeline.sources.values()
        ],
        "target": {"path": pipeline.target.path, "format": pipeline.target.format},
        "materialization": {
            "mode": pipeline.materialization.mode,
            "keys": list(pipeline.materialization.keys),
            "on_delete": pipeline.materialization.on_delete,
            "soft_delete_column": pipeline.materialization.soft_delete_column,
            "delete_predicate": pipeline.materialization.delete_predicate,
            "allow_empty_sync": pipeline.materialization.allow_empty_sync,
        },
        "sql": pipeline.sql,
    }


def _sync_lineage(registry, pipeline: Pipeline, output_schema: Optional[list]) -> tuple:
    """Validate declared lineage against the run's output schema and, if a
    loaded model scans this pipeline's target, regenerate that model's
    `pipeline_lineage:` section. Returns (lineage_ok, issues) for the run
    record — lineage_ok is None when the pipeline declares no lineage at all
    (nothing to validate, FR-018). Validation itself is always computed;
    the model-yaml write is best-effort — a filesystem hiccup there must
    never be conflated with "the declared lineage is wrong"."""
    if not pipeline.lineage:
        return None, []
    if output_schema is None:  # the run failed before a schema was ever reported
        return False, [{"kind": "declared_missing", "field": e.field} for e in pipeline.lineage]
    issues = pipelines_mod.validate_lineage(pipeline.lineage, output_schema)
    try:
        model_name = pipelines_mod.match_target_model(pipeline, registry.models)
        if model_name:
            model = registry.models[model_name]
            updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
            section = pipelines_mod.build_lineage_section(pipeline, output_schema, issues, updated)
            new_text = semantic.replace_lineage_yaml(registry.read_model_text(model), section)
            registry.write_model_text(model, new_text)
            registry.reload_all()
    except Exception:
        pass
    return (len(issues) == 0), issues


def target_lock_name(pipeline: Pipeline) -> str:
    """The cluster lock a run of `pipeline` holds while it executes.

    Named for the target path rather than the pipeline, because the thing
    that must not overlap is the *write*: two differently-named pipelines
    pointed at one path are exactly the collision this is for, and two
    pipelines with different targets have no reason to wait for each other."""
    return f"pipeline_target:{pipeline.target.path}"


def _execute(run_id: int, pipeline: Pipeline, registry) -> None:
    """Run one already-claimed run to completion and record its outcome.

    The caller has claimed `run_id` (so its row is `running`, attributed to
    this node) and holds this pipeline's target lock. This function only
    executes and records."""
    store: PipelineStore = registry.pipeline_store
    target = duck.split_s3(pipeline.target.path)
    job = {
        "pipeline": _pipeline_job_spec(pipeline),
        # only the write path needs credentials here: the runner reads its
        # sources through DuckDB's own S3 secrets (app/duck.py), and deltalake
        # wants its own uppercase env-var-style keys for the target's
        # read/write/merge (matching app/seed.py's delta-write precedent).
        # Resolved against the target's *own* bucket, so a pipeline writing to
        # a real bucket is credentialed for it even while the demo store is up.
        "storage": {"write": config.delta_write_options(target[0] if target else "")},
    }

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.pipeline_runner"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:
        store.finish_run(run_id, "failed", error=f"could not start runner subprocess: {exc}")
        return

    # The run's own lease is renewed for as long as the subprocess is alive.
    # The run row's own lease is renewed for as long as the subprocess is
    # alive. A pipeline's timeout may be an hour (config.PIPELINE_TIMEOUT_MAX)
    # and a lease is a minute, so without this every long run would look
    # abandoned and be swept out from under itself. The caller's keeper thread
    # renews the *target lock* over the same span; this one is for the row.
    #
    # None unclustered: there is no lease on the row (see _lease_seconds) and
    # nothing sweeping it mid-flight, so a renewal thread would tick for the
    # life of every run to call a function that returns True.
    keep = _RunLease(store, run_id, config.NODE_ID) if config.CLUSTERED else None
    try:
        with cluster.renewing(None, alongside=keep.renew if keep else None):
            stdout, stderr = proc.communicate(
                input=json.dumps(job), timeout=pipeline.timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # reap the process so its pipes don't leak
        store.finish_run(
            run_id, "timed_out",
            error=f"run exceeded its {pipeline.timeout_seconds}s timeout and was terminated",
        )
        return

    if not stdout.strip():
        store.finish_run(
            run_id, "failed",
            error=f"runner exited with code {proc.returncode} without reporting a result "
                  f"(stderr: {stderr[-4000:]})",
        )
        return

    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except Exception as exc:
        store.finish_run(
            run_id, "failed",
            error=f"could not parse runner output: {exc}; "
                  f"stdout={stdout[-2000:]!r} stderr={stderr[-2000:]!r}",
        )
        return

    if result.get("ok"):
        # the run wrote to the bucket, from a subprocess this one can't see
        # into, so anything this process is holding for the written path is now
        # stale. app/duck.py pins source *contents*, not just derived schemas,
        # and DuckDB's file cache holds the bytes of everything else, which is
        # what makes this a correctness step rather than a freshness nicety:
        # without it a model reading the pipeline's target would keep answering
        # from pre-run rows until the TTL lapsed.
        cache.clear()
        duck.invalidate()
        lineage_ok, lineage_issues = _sync_lineage(registry, pipeline, result.get("output_schema"))
        store.finish_run(
            run_id, "succeeded",
            rows_written=result.get("rows_written"), rows_deleted=result.get("rows_deleted"),
            rows_flagged=result.get("rows_flagged"), output_schema=result.get("output_schema"),
            lineage_ok=lineage_ok, lineage_issues=lineage_issues,
        )
    else:
        store.finish_run(
            run_id, "failed", error=result.get("error", "unknown runner failure"),
            output_schema=result.get("output_schema"),
        )


class _RunLease:
    """Renews one claimed run's lease, in the shape `cluster.renewing` wants
    (a callable returning False when the claim is gone). Only constructed in
    a cluster — see _execute."""

    def __init__(self, store: PipelineStore, run_id: int, node_id: str):
        self._store, self._run_id, self._node_id = store, run_id, node_id

    def renew(self) -> bool:
        return self._store.renew_lease(
            self._run_id, self._node_id, config.CLUSTER_LEASE_SECONDS)


def _lease_seconds() -> float:
    """The lease a claim carries. Zero unclustered, which writes NULL and
    means "nothing is watching this claim" — the single-process case, where
    a crash is recovered by `sweep_interrupted` at the next start rather than
    by a peer noticing."""
    return config.CLUSTER_LEASE_SECONDS if config.CLUSTERED else 0.0


def _run_one(registry) -> bool:
    """Claim and run at most one queued run. True if one was executed.

    The order is claim-then-lock, not lock-then-claim, because the claim is
    what tells us which target to lock. A claim that cannot get its target
    lock is handed straight back to the queue: another worker is writing that
    path right now, and this one has other runs it could be doing.
    """
    store: PipelineStore = registry.pipeline_store
    run = store.claim_next_run(config.NODE_ID, _lease_seconds())
    if run is None:
        return False
    run_id = run["id"]
    pipeline = registry.pipelines.get(run["pipeline"])
    if pipeline is None:
        # Reachable in a cluster for a reason it never was in one process: a
        # pipeline created on a peer whose CONFIG_GENERATION bump this node
        # has not polled yet. Requeue rather than fail — the registry will
        # catch up within a poll interval, and failing a run because the
        # worker was momentarily behind would be a lie about the pipeline.
        if config.CLUSTERED and store.requeue_run(run_id, config.NODE_ID):
            return False
        store.finish_run(run_id, "failed",
                         error=f"pipeline '{run['pipeline']}' no longer exists")
        return True
    with cluster.lock(target_lock_name(pipeline), wait=0.0) as lease:
        if lease is None:
            # Someone else is writing this target. Back to the queue.
            store.requeue_run(run_id, config.NODE_ID)
            return False
        with cluster.renewing(lease) as lost:
            _execute(run_id, pipeline, registry)
        if lost.is_set():
            print(f"[cash-intel] pipeline run {run_id}: target lock lapsed mid-run "
                  f"— another worker may have run the same target concurrently")
    return True


def _drain(registry) -> None:
    while not _stop_event.is_set():
        try:
            # The doorbell: a run triggered on this process wakes the loop
            # immediately. The timeout is the poll that finds runs triggered
            # elsewhere — and, unclustered, simply the idle tick it always was.
            _queue.get(timeout=config.CLUSTER_POLL_SECONDS if config.CLUSTERED else 1)
        except queue.Empty:
            pass
        try:
            # Drain rather than run-one-per-wake: several runs may be queued
            # (a burst, or a peer's backlog after this worker restarted), and
            # each _queue.get only accounts for one of them.
            while not _stop_event.is_set() and _run_one(registry):
                pass
            if config.CLUSTERED:
                registry.pipeline_store.sweep_expired()
        except Exception as exc:                                # noqa: BLE001
            # The worker thread must outlive one bad run: dying here would
            # leave every future trigger queued forever with nothing to say
            # why. The run itself is already recorded failed by _execute.
            print(f"[cash-intel] pipeline worker pass failed: {exc}")


def enqueue(run_id: int) -> None:
    """Ring the doorbell for a freshly-created queued run.

    Not the queue itself any more — the run's `queued` row in the store is
    (see the module docstring). This only saves a poll interval when the
    worker happens to live in the process that took the trigger, so it is
    safe to call from a `web` replica with no worker at all: the row is
    already there and some worker will claim it."""
    _queue.put(run_id)


def start_worker(registry) -> bool:
    """Start draining runs, unless this process's role says not to.

    Returns whether a worker started, so the caller can say so at boot — "no
    worker here" is exactly the kind of thing that must be visible in a log
    rather than inferred from runs sitting queued."""
    global _worker_thread
    if not config.RUNS_PIPELINES:
        return False
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_drain, args=(registry,), daemon=True, name="pipeline-jobs")
    _worker_thread.start()
    return True


def stop_worker() -> None:
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5)
