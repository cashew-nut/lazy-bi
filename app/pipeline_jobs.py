"""FIFO pipeline run worker (specs/014-polars-pipeline-module/): a single
daemon thread drains queued runs one at a time, spawning each in its own
subprocess (app/pipeline_runner.py) so a hard timeout can actually be
enforced — a plain thread cannot be killed — and a script crash or infinite
loop never takes the app down. The worker is the only writer of
pipeline_runs rows, extending this app's existing single-writer posture
(embedded S3 emulator, sqlite store) to pipeline execution: at most one run
executes platform-wide at any moment, enforced simply by there being one
consumer thread pulling from one queue.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from typing import Optional

from . import config
from .pipelines import Pipeline
from .pipelinestore import PipelineStore

_queue: "queue.Queue[int]" = queue.Queue()
_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _pipeline_job_spec(pipeline: Pipeline) -> dict:
    """The subset of a Pipeline the runner subprocess needs — see
    contracts/pipelines-api.md's runner protocol. Built directly from the
    dataclasses (not Pipeline.to_public(), which omits `script` — that
    summary is for the list API, not execution)."""
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
        "script": pipeline.script,
    }


def _execute(run_id: int, pipeline: Pipeline, store: PipelineStore) -> None:
    store.mark_running(run_id)
    job = {
        "pipeline": _pipeline_job_spec(pipeline),
        # two storage_options shapes: polars-style lowercase for scanning
        # declared sources (any format — matches app/engine.py), deltalake's
        # own uppercase env-var-style for the target read/write/merge
        # (matches app/seed.py's existing delta-write precedent).
        "storage": {"read": config.storage_options(), "write": config.delta_write_options()},
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

    try:
        stdout, stderr = proc.communicate(input=json.dumps(job), timeout=pipeline.timeout_seconds)
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
        store.finish_run(
            run_id, "succeeded",
            rows_written=result.get("rows_written"), rows_deleted=result.get("rows_deleted"),
            rows_flagged=result.get("rows_flagged"), output_schema=result.get("output_schema"),
        )
    else:
        store.finish_run(
            run_id, "failed", error=result.get("error", "unknown runner failure"),
            output_schema=result.get("output_schema"),
        )


def _drain(registry) -> None:
    while not _stop_event.is_set():
        try:
            run_id = _queue.get(timeout=1)
        except queue.Empty:
            continue
        run = registry.pipeline_store.get_run(run_id)
        if run is None or run["status"] != "queued":
            continue  # defensive only — enqueue() only ever posts freshly-queued ids
        pipeline = registry.pipelines.get(run["pipeline"])
        if pipeline is None:
            registry.pipeline_store.finish_run(
                run_id, "failed", error=f"pipeline '{run['pipeline']}' no longer exists"
            )
            continue
        _execute(run_id, pipeline, registry.pipeline_store)


def enqueue(run_id: int) -> None:
    """Post a freshly-created queued run to the worker. Safe to call before
    start_worker() — the queue simply holds it until the thread is running."""
    _queue.put(run_id)


def start_worker(registry) -> None:
    global _worker_thread
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_drain, args=(registry,), daemon=True, name="pipeline-jobs")
    _worker_thread.start()


def stop_worker() -> None:
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5)
