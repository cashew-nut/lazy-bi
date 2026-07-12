"""Out-of-process execution for untrusted measure code.

Measure `expr`/`frame` are arbitrary python that the engine must `eval`/`exec`
to build (and, for frames, run) a polars computation. Clearing `__builtins__`
is *not* a security boundary — `().__class__.__bases__[0].__subclasses__()`
walks straight back to `os.system` — so any hostile expression reaching the API
process is remote code execution against its secrets and its host.

This module contains that evaluation in a throwaway subprocess per request:

  * fresh process every call — no state leaks between queries;
  * hardened before user code runs — RLIMIT_CPU/AS/FSIZE/NPROC/NOFILE,
    PR_SET_NO_NEW_PRIVS, cores off (see sandbox_worker._harden);
  * a minimal environment (config.sandbox_child_env) — only the scoped S3
    read credentials the scan needs, nothing else of the host's;
  * a wall-clock timeout the parent enforces with SIGKILL;
  * optionally wrapped in bubblewrap / nsjail when either is installed, adding
    namespace + filesystem isolation on top.

The trust direction is asymmetric and deliberate: the parent hands the child a
*pickled* Model (the parent authored it — safe), but the child may only answer
with **JSON**. A polars plan pickled by compromised child code could smuggle an
embedded Python UDF that would execute on unpickle in the parent; JSON and the
Arrow-free result dict carry data, never code. So the child always `collect()`s
and returns finished rows — no lazy plan ever crosses back.
"""
from __future__ import annotations

import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile

from . import config
from .semantic import Model, ModelError

WORKER_MODULE = "app.sandbox_worker"


class SandboxError(Exception):
    """The sandbox itself failed to produce a result — a resource limit was
    hit, the worker was killed, or it crashed. Distinct from a ModelError /
    QueryError the user's expression raised (those are relayed faithfully)."""


def _limits() -> dict:
    return {
        "cpu_seconds": config.SANDBOX_CPU_SECONDS,
        "mem_mb": config.SANDBOX_MEM_MB,
        "fsize_mb": config.SANDBOX_FSIZE_MB,
        "nproc": config.SANDBOX_NPROC,
        "nofile": config.SANDBOX_NOFILE,
    }


def _resolve_backend() -> str:
    """Pick the isolation backend. `auto` prefers the strongest tool present
    (namespace isolation) and always falls back to a bare hardened subprocess,
    which — with the rlimits + dropped privileges the worker applies itself —
    is a real boundary on its own."""
    backend = config.SANDBOX_BACKEND
    if backend in ("bwrap", "nsjail", "subprocess"):
        return backend
    if backend != "auto":
        raise SandboxError(f"unknown CI_SANDBOX_BACKEND '{backend}'")
    if shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("nsjail"):
        return "nsjail"
    return "subprocess"


def _wrap_argv(backend: str, inner: list[str]) -> list[str]:
    """Wrap the worker command in a namespace jail when one is available. Both
    jails keep network up (the scan must reach S3) but drop everything else:
    read-only root, private /tmp, no new privileges, die with the parent."""
    if backend == "bwrap":
        # HOME is set to the temp dir in the child env, so a private /tmp
        # tmpfs already covers polars' scratch space
        return [
            "bwrap", "--unshare-all", "--share-net", "--die-with-parent",
            "--new-session", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp",
            "--", *inner,
        ]
    if backend == "nsjail":
        return [
            "nsjail", "--quiet", "--mode", "o", "--disable_clone_newnet",
            "--rlimit_as", "max", "--chroot", "/", "--", *inner,
        ]
    return inner


def _spawn(payload: dict) -> dict:
    """Run one worker job (execute | validate) and return its parsed result
    dict, or raise. Payload — Model included — is pickled to a private temp
    file the child reads by path; only JSON comes back out."""
    backend = _resolve_backend()
    # -s: skip user site-packages, -B: no bytecode writes. (Not -I/-E: the
    # worker needs PYTHONPATH from the minimal env to import `app`.)
    inner = [sys.executable, "-s", "-B", "-m", WORKER_MODULE]
    fd, payload_path = tempfile.mkstemp(prefix="ci_sandbox_", suffix=".pkl")
    try:
        with os.fdopen(fd, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.chmod(payload_path, 0o600)
        argv = _wrap_argv(backend, [*inner, "--payload", payload_path])
        try:
            proc = subprocess.run(
                argv,
                env=config.sandbox_child_env(),
                cwd=str(config.PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=config.SANDBOX_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise SandboxError(
                f"measure evaluation exceeded the {config.SANDBOX_TIMEOUT_SECONDS:g}s time limit"
            )
        except FileNotFoundError as exc:
            raise SandboxError(f"sandbox backend '{backend}' not runnable: {exc}")
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass

    if proc.returncode != 0 and not proc.stdout:
        # killed by a resource limit (SIGKILL/SIGXCPU) or crashed before it
        # could report — surface the tail of stderr for diagnosis
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-4:]
        detail = " | ".join(tail) if tail else f"worker exited with code {proc.returncode}"
        raise SandboxError(f"measure evaluation was terminated (likely a resource limit): {detail}")
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SandboxError(f"sandbox produced no readable result: {exc}")


def _relay(reply: dict):
    """Turn a worker reply into a return value or the exception it stands for.
    ModelError / QueryError raised by the user's own code are re-raised as
    themselves; anything else the worker hit becomes a SandboxError."""
    status = reply.get("status")
    if status == "ok":
        return reply["result"]
    message = reply.get("message", "unknown sandbox error")
    kind = reply.get("error_type")
    if kind == "ModelError":
        raise ModelError(message)
    if kind == "QueryError":
        # imported lazily: engine imports this module, so importing it up top
        # would be a cycle
        from .engine import QueryError
        raise QueryError(message)
    raise SandboxError(message)


def execute(model: Model, query: dict) -> dict:
    """Run a semantic query with its measure code evaluated in the sandbox and
    return the same result dict engine.run_query would (columns/rows/…)."""
    return _relay(_spawn({"job": "execute", "model": model, "query": query, "limits": _limits()}))


def validate_model(model: Model) -> None:
    """Compile every measure expr/frame of `model` in the sandbox; raise
    ModelError (as the in-process check would) if any is invalid."""
    _relay(_spawn({"job": "validate", "model": model, "limits": _limits()}))
