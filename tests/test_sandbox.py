"""The measure-code sandbox: the real out-of-process path.

The broad suites run measure code in-process (conftest sets CI_SANDBOX=off) for
speed; here we force the sandbox on and exercise it end to end — result parity
with in-process, resource-limit enforcement, faithful error relay, and — the
point of the whole thing — that a hostile expression which escapes an empty
`__builtins__` still cannot touch the host from inside the jail.
"""
import json
from datetime import date

import polars as pl
import pytest

from app import config, engine, sandbox, semantic

pytestmark = pytest.mark.usefixtures("seeded")


@pytest.fixture
def sandbox_on(monkeypatch):
    """Force the sandbox on and use the always-available bare-subprocess backend
    so the suite is deterministic regardless of whether bwrap/nsjail exist. The
    pool is pinned off here so these tests exercise the direct per-call spawn;
    the pool has its own fixture below."""
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_BACKEND", "subprocess")
    monkeypatch.setattr(config, "SANDBOX_POOL", False)


@pytest.fixture
def pool_on(monkeypatch):
    """Sandbox on via the pre-warmed pool. Small pool, and torn down after each
    test so warm worker processes never leak across the suite."""
    from app import sandbox_pool
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_BACKEND", "subprocess")
    monkeypatch.setattr(config, "SANDBOX_POOL", True)
    monkeypatch.setattr(config, "SANDBOX_POOL_SIZE", 2)
    sandbox_pool.shutdown()
    yield
    sandbox_pool.shutdown()


SALES = "sales"


def _q(**query):
    return query


# ── result parity ─────────────────────────────────────────────────

def test_sandbox_matches_in_process(models, sandbox_on):
    query = _q(dimensions=["region"], measures=["revenue", "orders"])
    sandboxed = engine.run_query(models[SALES], query)
    inprocess = engine.run_query_local(models[SALES], query)
    assert sandboxed["rows"] == inprocess["rows"]
    assert [c["name"] for c in sandboxed["columns"]] == [c["name"] for c in inprocess["columns"]]


def test_sandbox_framed_measure_parity(models, sandbox_on):
    # a framed measure exercises exec() of a multi-statement snippet + collect,
    # all of which must happen inside the worker
    query = _q(dimensions=["therapeutic_area"],
               measures=["randomised_actual", "median_months_to_75pct_randomised"])
    sandboxed = engine.run_query(models["clinical_ops_recruitment"], query)
    inprocess = engine.run_query_local(models["clinical_ops_recruitment"], query)
    assert sandboxed["rows"] == inprocess["rows"]


def test_dispatch_uses_sandbox(models, sandbox_on, monkeypatch):
    called = {}
    real = sandbox.execute
    def spy(model, query):
        called["yes"] = True
        return real(model, query)
    monkeypatch.setattr(sandbox, "execute", spy)
    engine.run_query(models[SALES], _q(dimensions=[], measures=["orders"]))
    assert called.get("yes") is True


# ── containment: the reason this exists ───────────────────────────

ESCAPE = (
    "().__class__.__bases__[0].__subclasses__()"  # climb from object to every class…
)


def _run_hostile(models, expr):
    return engine.run_query(models[SALES], _q(
        dimensions=[], measures=["x"],
        inline_measures=[{"name": "x", "expr": expr}],
    ))


def _attempt(models, payload):
    """Run a hostile expression and swallow whichever way it fails (it may
    raise, or — like os.system — return a value); the invariant under test is
    the *effect on the host*, asserted separately by the caller."""
    try:
        _run_hostile(models, payload)
    except engine.QueryError:
        pass


def test_escape_via_subclasses_is_contained(models, sandbox_on, tmp_path):
    # This expression defeats the empty-__builtins__ "guard": it reaches the
    # subclass graph and tries to spawn a shell that writes a marker file.
    # In-process this WOULD execute; in the sandbox the execve is denied, so
    # the host filesystem is never touched.
    marker = tmp_path / "pwned"
    _attempt(models, (
        "[c for c in ().__class__.__bases__[0].__subclasses__() "
        "if c.__name__ == 'Popen'][0]"
        f"(['/bin/sh', '-c', 'touch {marker}']) and pl.len()"
    ))
    assert not marker.exists()


def test_import_via_builtins_is_contained(models, sandbox_on, tmp_path):
    # another classic: rebuild __import__ from a loaded module's globals and
    # os.system a shell command. os.system itself won't raise (it returns the
    # child's exit status), but the execve behind it is denied — no marker.
    marker = tmp_path / "imported"
    _attempt(models, (
        "[v for v in ().__class__.__bases__[0].__subclasses__() "
        "if v.__name__ == 'catch_warnings'][0]()._module."
        f"__builtins__['__import__']('os').system('touch {marker}') and pl.len()"
    ))
    assert not marker.exists()


def test_seccomp_denies_execve_directly():
    # the guarantee the containment tests lean on, isolated: after the filter
    # is installed the process is alive but can no longer exec another program.
    # Run it in a forked child so the test process keeps its own exec rights.
    import os
    import sys

    if not sys.platform.startswith("linux"):
        pytest.skip("seccomp is linux-only")
    from app import seccomp

    pid = os.fork()
    if pid == 0:  # child
        code = 3
        if seccomp.install_syscall_filter():
            try:
                os.execv("/bin/true", ["/bin/true"])
                code = 0  # execve succeeded — filter did nothing
            except PermissionError:
                code = 42  # denied, as intended
            except Exception:
                code = 1
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 42


def test_worker_env_has_no_unrelated_secrets(monkeypatch):
    # a hostile expression can read os.environ inside the jail — so the jail's
    # environment must not carry anything but the scoped S3 access
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "sk-live-xxxxx")
    env = config.sandbox_child_env()
    assert "SUPER_SECRET_TOKEN" not in env
    assert env["CI_SANDBOX"] == "off"  # the child never re-sandboxes
    assert "AWS_ACCESS_KEY_ID" in env  # but the scan credential is present


# ── resource limits ───────────────────────────────────────────────

def test_cpu_timeout_is_enforced(models, sandbox_on, monkeypatch):
    # a frame that spins forever must be killed and reported, not hang the API
    monkeypatch.setattr(config, "SANDBOX_TIMEOUT_SECONDS", 4.0)
    monkeypatch.setattr(config, "SANDBOX_CPU_SECONDS", 2)
    spin = "while True:\n    pass\nframe = lf"
    with pytest.raises(engine.QueryError, match="limit|terminated"):
        engine.run_query(models[SALES], _q(
            dimensions=[], measures=["x"],
            inline_measures=[{"name": "x", "frame": spin, "expr": "pl.len()"}],
        ))


# ── error relay ───────────────────────────────────────────────────

def test_user_query_error_relayed(models, sandbox_on):
    # a genuine user error (bad column) should surface as a normal QueryError,
    # not a generic sandbox failure
    with pytest.raises(engine.QueryError):
        engine.run_query(models[SALES], _q(
            dimensions=[], measures=["x"],
            inline_measures=[{"name": "x", "expr": 'pl.col("does_not_exist").sum()'}],
        ))


def test_validation_runs_in_sandbox(sandbox_on):
    # parse_model_text with a syntactically-valid but semantically-bad expr:
    # the eval that catches it happens in the worker, and the ModelError is
    # relayed back verbatim
    bad = (
        "name: t\nsource: {format: parquet, path: s3://b/x/*.parquet}\n"
        "dimensions: [{name: r}]\nmeasures: [{name: rows, expr: pl.nope()}]\n"
    )
    with pytest.raises(semantic.ModelError):
        semantic.parse_model_text(bad)


def test_validation_accepts_good_model_in_sandbox(sandbox_on):
    good = (
        "name: t\nsource: {format: parquet, path: s3://b/x/*.parquet}\n"
        "dimensions: [{name: r}]\nmeasures: [{name: rows, expr: pl.len()}]\n"
    )
    model = semantic.parse_model_text(good)  # validates via the worker
    assert model.name == "t"


# ── backend construction ──────────────────────────────────────────

def test_bwrap_wrapping_keeps_network_and_isolates_fs():
    argv = sandbox._wrap_argv("bwrap", ["python", "-m", "app.sandbox_worker"])
    assert argv[0] == "bwrap"
    assert "--share-net" in argv          # the scan must reach S3
    assert "--unshare-all" in argv        # …but nothing else
    assert argv[-3:] == ["python", "-m", "app.sandbox_worker"]


def test_unknown_backend_rejected(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_BACKEND", "nope")
    with pytest.raises(sandbox.SandboxError, match="unknown"):
        sandbox._resolve_backend()


# ── pre-warmed one-shot worker pool ───────────────────────────────

def test_pool_matches_in_process(models, pool_on):
    query = _q(dimensions=["region"], measures=["revenue", "orders"])
    pooled = engine.run_query(models[SALES], query)
    inprocess = engine.run_query_local(models[SALES], query)
    assert pooled["rows"] == inprocess["rows"]


def test_pool_handles_large_result(models, pool_on):
    # a many-row result frames a reply larger than the pipe buffer — exercises
    # the write-all loop on both ends (a single os.write would truncate it)
    q = _q(dimensions=[{"name": "order_date", "grain": "1d"}], measures=["revenue", "orders"], limit=1000)
    pooled = engine.run_query(models[SALES], q)
    inprocess = engine.run_query_local(models[SALES], q)
    assert pooled["row_count"] == inprocess["row_count"] > 100
    assert pooled["rows"] == inprocess["rows"]


def test_pool_serves_multiple_queries(models, pool_on):
    # several jobs in a row exercise take-a-warm-worker + background refill
    for _ in range(4):
        r = engine.run_query(models[SALES], _q(dimensions=[], measures=["orders"]))
        assert r["rows"][0]["orders"] > 0


def test_pool_worker_is_fresh_per_job_and_contains_escape(models, pool_on, tmp_path):
    # the hardening runs identically in a pooled worker: a shell-out is still
    # denied, and because each worker takes exactly one job there is no reuse
    marker = tmp_path / "pooled_pwned"
    _attempt(models, (
        "[c for c in ().__class__.__bases__[0].__subclasses__() "
        "if c.__name__ == 'Popen'][0]"
        f"(['/bin/sh','-c','touch {marker}']) and pl.len()"
    ))
    assert not marker.exists()
    # and the pool still works for a normal query afterward (no poisoned state)
    r = engine.run_query(models[SALES], _q(dimensions=[], measures=["orders"]))
    assert r["rows"][0]["orders"] > 0


def test_pool_timeout_enforced(models, pool_on, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_TIMEOUT_SECONDS", 4.0)
    monkeypatch.setattr(config, "SANDBOX_CPU_SECONDS", 2)
    spin = "while True:\n    pass\nframe = lf"
    with pytest.raises(engine.QueryError, match="limit|terminated|died"):
        engine.run_query(models[SALES], _q(
            dimensions=[], measures=["x"],
            inline_measures=[{"name": "x", "frame": spin, "expr": "pl.len()"}],
        ))


def test_pool_retries_a_dead_warm_worker(models, pool_on):
    # a warm worker can die while idle in the pool; submit() must transparently
    # spawn a fresh one rather than surfacing the stale worker's death
    from app import sandbox_pool
    pool = sandbox_pool.get_pool()
    dead = pool._spawn_warm()
    dead.proc.kill()
    dead.proc.wait()
    pool._ready.put(dead)  # poison the pool with a corpse at the front
    r = engine.run_query(models[SALES], _q(dimensions=[], measures=["orders"]))
    assert r["rows"][0]["orders"] > 0
