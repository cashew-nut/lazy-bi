"""Sandbox worker: the *inside* of the jail (see app/sandbox.py).

Runs as a throwaway `python -I -m app.sandbox_worker --payload <file>` process.
It reads one pickled job from the trusted parent, clamps itself down (resource
limits + no-new-privileges), evaluates the untrusted measure code, and writes a
single JSON line back on a stdout channel it guards so nothing else can pollute
it. Never trusts its own output channel with anything but JSON.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import pickle
import resource
import struct
import sys
import traceback

READY = b"READY\n"

# PR_SET_* constants (linux/prctl.h)
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38


def _setrlimit(which: int, value: int) -> None:
    """Set a soft+hard rlimit, never *raising* the ceiling above the inherited
    hard cap and quietly skipping limits this platform lacks."""
    try:
        _soft, hard = resource.getrlimit(which)
    except (ValueError, OSError):
        return
    cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
    try:
        resource.setrlimit(which, (cap, cap))
    except (ValueError, OSError):
        pass


def _harden(limits: dict) -> None:
    """Clamp the process before any untrusted code runs. Applied *after* the
    heavy imports (polars reserves large virtual mappings at import) so an
    address-space cap doesn't fault the interpreter itself."""
    if limits.get("cpu_seconds"):
        _setrlimit(resource.RLIMIT_CPU, int(limits["cpu_seconds"]))
    if limits.get("fsize_mb"):
        _setrlimit(resource.RLIMIT_FSIZE, int(limits["fsize_mb"]) * 1024 * 1024)
    if limits.get("nproc") and hasattr(resource, "RLIMIT_NPROC"):
        _setrlimit(resource.RLIMIT_NPROC, int(limits["nproc"]))
    if limits.get("mem_mb"):  # opt-in: RLIMIT_AS can fault polars' allocator
        _setrlimit(resource.RLIMIT_AS, int(limits["mem_mb"]) * 1024 * 1024)
    if limits.get("nofile"):
        _setrlimit(resource.RLIMIT_NOFILE, int(limits["nofile"]))
    _setrlimit(resource.RLIMIT_CORE, 0)

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)  # no setuid escalation
        libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)      # no ptrace / core dumps
    except (OSError, AttributeError):
        pass

    # last: block execve/execveat/ptrace so a hostile expression cannot shell
    # out even though the bare-subprocess tier shares the host filesystem
    from . import seccomp
    seccomp.install_syscall_filter()


def _run(job: dict) -> dict:
    """Dispatch one job to the in-process primitives. _harden clamps the
    process (once — one job per worker) right before the untrusted code runs."""
    from . import engine, semantic

    _harden(job["limits"])
    model = job["model"]
    if job["job"] == "execute":
        return {"status": "ok", "result": engine.run_query_local(model, job["query"])}
    if job["job"] == "validate":
        semantic.validate_model_exprs(model)  # raises ModelError if invalid
        return {"status": "ok", "result": {"ok": True}}
    raise ValueError(f"unknown sandbox job '{job['job']}'")


def _reply_for(job: dict) -> dict:
    try:
        return _run(job)
    except Exception as exc:  # noqa: BLE001 - every failure becomes a JSON reply
        kind = type(exc).__name__
        error_type = kind if kind in ("ModelError", "QueryError") else "Exception"
        if error_type == "Exception":
            traceback.print_exc()  # to stderr, for the parent's diagnostics tail
        return {"status": "error", "error_type": error_type, "message": str(exc)}


def _read_exact(fd: int, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = os.read(fd, n - len(data))
        if not chunk:
            raise EOFError("parent closed the job channel")
        data += chunk
    return data


def _write_all(fd: int, data: bytes) -> None:
    """os.write can write fewer bytes than asked for a large buffer on a pipe;
    loop so a big result/reply is never truncated."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _serve_warm(real_stdout: int) -> None:
    """Pool mode: import the heavy deps now (paying that cost while idle in the
    parent's pool), announce readiness, then handle exactly one framed job from
    stdin and exit. One job per process — no reuse, no cross-query state."""
    from . import engine, semantic  # noqa: F401 - warm the import before READY

    _write_all(real_stdout, READY)
    header = _read_exact(0, 4)
    job = pickle.loads(_read_exact(0, struct.unpack("!I", header)[0]))  # trusted parent
    body = json.dumps(_reply_for(job)).encode("utf-8")
    _write_all(real_stdout, struct.pack("!I", len(body)) + body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", help="one-shot: read a pickled job from this file")
    parser.add_argument("--warm", action="store_true", help="pool: serve one framed job from stdin")
    args = parser.parse_args()

    # guard the result channel: keep the real stdout, then point fd 1 at stderr
    # so any stray library write can't corrupt what the parent parses
    real_stdout = os.dup(1)
    os.dup2(2, 1)

    if args.warm:
        _serve_warm(real_stdout)
        return
    with open(args.payload, "rb") as fh:
        job = pickle.load(fh)  # trusted: the parent authored this pickle
    _write_all(real_stdout, json.dumps(_reply_for(job)).encode("utf-8"))


if __name__ == "__main__":
    main()
