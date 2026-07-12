"""A pool of pre-warmed, one-shot sandbox workers.

The per-call sandbox spawns a fresh `python -m app.sandbox_worker` and pays the
polars import (~250 ms) on the request path. This pool moves that cost off the
request path without weakening isolation: it keeps a few workers that have
already imported polars and are blocked waiting for a single job. A query grabs
a warm worker, hands it one job, reads the reply, and the worker exits — one job
per process, so there is still no reuse and no cross-query state. A background
thread refills the pool.

Why not a real fork server (which would share the warm import via copy-on-
write)? object_store's tokio runtime does not survive `fork()` — an S3 scan in a
forked child deadlocks. Each worker here is therefore a *spawned* process with a
healthy runtime; only the import latency is amortized, by pre-warming, not
forking.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import select
import struct
import subprocess
import threading
import time

from . import config

_READY = b"READY\n"


class _WorkerDied(Exception):
    """A pooled worker was already dead (or died mid-job) before replying —
    distinct from a job that produced an error reply. Triggers one retry."""


class _Worker:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def _read_exact(self, n: int, deadline: float) -> bytes:
        fd = self.proc.stdout.fileno()
        data = b""
        while len(data) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError
            chunk = os.read(fd, n - len(data))
            if not chunk:
                raise _WorkerDied("worker closed its result channel")
            data += chunk
        return data

    def run(self, payload: bytes, timeout: float) -> dict:
        """Send one framed job and read the framed JSON reply. Raises
        TimeoutError (caller kills the worker) or _WorkerDied."""
        framed = struct.pack("!I", len(payload)) + payload
        try:
            view = memoryview(framed)
            while view:  # os.write may accept fewer bytes than offered on a pipe
                view = view[os.write(self.proc.stdin.fileno(), view):]
        except (BrokenPipeError, OSError):
            raise _WorkerDied("worker gone before the job was sent")
        deadline = time.monotonic() + timeout
        (length,) = struct.unpack("!I", self._read_exact(4, deadline))
        return json.loads(self._read_exact(length, deadline))

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class WorkerPool:
    """Maintains ~`size` warm workers; hands out one per job."""

    def __init__(self) -> None:
        self._ready: "queue.Queue[_Worker]" = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._refiller = threading.Thread(target=self._refill_loop, name="sandbox-pool", daemon=True)
        self._refiller.start()

    # -- worker lifecycle -------------------------------------------------
    def _spawn_warm(self) -> _Worker:
        """Spawn a worker and block until it reports it has imported polars.
        Raises SandboxError if it dies or never warms up in time."""
        from .sandbox import SandboxError, worker_argv

        proc = subprocess.Popen(
            worker_argv("--warm"),
            env=config.sandbox_child_env(),
            cwd=str(config.PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # avoid a full stderr pipe blocking a warm worker
            bufsize=0,
        )
        worker = _Worker(proc)
        deadline = time.monotonic() + config.SANDBOX_POOL_WARM_TIMEOUT
        try:
            got = worker._read_exact(len(_READY), deadline)
        except (TimeoutError, _WorkerDied) as exc:
            worker.close()
            raise SandboxError(f"sandbox worker failed to warm up: {exc}")
        if got != _READY:
            worker.close()
            raise SandboxError("sandbox worker sent an unexpected warm-up signal")
        return worker

    def _refill_loop(self) -> None:
        while not self._closed:
            if self._ready.qsize() < config.SANDBOX_POOL_SIZE:
                try:
                    worker = self._spawn_warm()
                except Exception:
                    time.sleep(0.5)  # spawning failed; back off, don't hot-loop
                    continue
                if self._closed:       # shutdown raced us — don't leak the worker
                    worker.close()
                    return
                self._ready.put(worker)
                continue
            time.sleep(0.05)

    def _take(self) -> _Worker:
        """A warm worker if one is ready, else spawn one on demand (this call
        pays the import cost, unlike the steady-state fast path)."""
        try:
            return self._ready.get_nowait()
        except queue.Empty:
            return self._spawn_warm()

    # -- job submission ---------------------------------------------------
    def submit(self, payload: bytes) -> dict:
        """Run one job on a fresh warm worker; retry once if the worker we
        picked turned out to be dead (an idle worker can die in the pool)."""
        from .sandbox import SandboxError

        last: Exception | None = None
        for attempt in range(2):
            worker = self._take()
            try:
                return worker.run(payload, config.SANDBOX_TIMEOUT_SECONDS)
            except TimeoutError:
                worker.close()
                raise SandboxError(
                    f"measure evaluation exceeded the {config.SANDBOX_TIMEOUT_SECONDS:g}s time limit"
                )
            except _WorkerDied as exc:
                worker.close()
                last = exc  # a stale pooled worker — try once more with a fresh one
            finally:
                if worker.proc.poll() is None:
                    worker.close()
        raise SandboxError(f"sandbox worker died before replying: {last}")

    def shutdown(self) -> None:
        self._closed = True
        while True:
            try:
                self._ready.get_nowait().close()
            except queue.Empty:
                break


_pool: WorkerPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> WorkerPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = WorkerPool()
        return _pool


def shutdown() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown()
            _pool = None


atexit.register(shutdown)  # reap warm workers even without the app lifespan
