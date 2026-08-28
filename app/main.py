"""CASH INTELLIGENCE — lightweight BI over S3 files, powered by DuckDB.

Run:  uvicorn app.main:app --port 8080

App factory + lifecycle only; endpoints live in app/api/*, runtime state in
app/registry.py. The demo bucket is served by an embedded moto S3 server,
seeded on start if it is empty — including alongside a real object store, so
the built-in catalog and a real bucket both answer. Set CI_BUCKET (and,
outside AWS, CI_S3_ENDPOINT) to point everything else at the real one, or
CI_DEMO=0 to drop the demo entirely.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from . import auth, cluster, config, emulator, llmclient, mcpserver, pipeline_jobs, seed
# Registers ask_question/list_models into app/skills.py's registry as a
# module-import side effect — must happen before mcpserver.create_asgi_app()
# below reads that registry (specs/017-agent-skills-mcp-server/research.md
# R6) and before Registry.init() validates agents/*.yaml against it.
from . import skills_analytics  # noqa: F401
from .api import api_router
from .registry import registry

STATIC_DIR = Path(__file__).parent / "static"

# The only /api requests answerable without an identity: signing in, and a
# liveness probe. Everything else is default-deny — a route cannot opt out
# by forgetting a dependency (specs/011-session-auth-rbac/research.md R3).
# /mcp has no equivalent public entry at all (specs/017-agent-skills-mcp-
# server/): even the MCP `initialize` handshake requires valid credentials.
PUBLIC_API = {("POST", "/api/auth/login"), ("GET", "/api/health")}

# How long a replica waits for the boot lock before starting anyway. Long
# enough to cover seeding the demo bucket on a cold bucket (the slowest thing
# under that lock), short enough that one wedged replica cannot hold a whole
# deployment's rollout hostage — the loser's own first-run checks are still
# correct on their own, it just may briefly serve an emptier catalog.
_BOOT_LOCK_WAIT = 120.0

# Built once at import time (not inside create_app()/lifespan) so its
# `.lifespan` can be composed into the app's own lifespan() below — see
# specs/017-agent-skills-mcp-server/research.md R2 for why a mounted
# Streamable HTTP MCP app's lifespan must be entered explicitly.
mcp_app = mcpserver.create_asgi_app()


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticates every /api and /mcp request (401 otherwise) and
    stashes the principal on request.state.user; routes layer authorization
    on top via auth.require_role (/api) or a skill's min_role
    (/mcp, app/skills.py's invoke_skill()). Static assets and the SPA shell
    stay public — they are code, not data; the SPA renders its login view
    when /api/auth/me says 401.

    Credential precedence: an Authorization: Bearer header is used
    exclusively when present (no cookie fallback — one identity, never a
    merge). Cookie-authenticated mutations must carry the CSRF header
    X-Requested-With: fetch; bearer requests are exempt because cross-site
    pages cannot set an Authorization header.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        guarded = path.startswith("/api") or path.startswith("/mcp")
        if not guarded or (request.method, path) in PUBLIC_API:
            return await call_next(request)
        store = registry.auth_store
        if store is None:
            return JSONResponse({"detail": "authentication not ready"}, status_code=503)
        user, via_cookie = None, False
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            user = auth.resolve_token(store, authz[7:].strip())
        else:
            cookie = request.cookies.get(auth.COOKIE_NAME)
            if cookie:
                user = auth.resolve_session(store, cookie)
                via_cookie = user is not None
        if user is None:
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        if (via_cookie and request.method not in ("GET", "HEAD")
                and request.headers.get(auth.CSRF_HEADER) != "fetch"):
            return JSONResponse(
                {"detail": "missing X-Requested-With: fetch header"}, status_code=403)
        request.state.user = user
        return await call_next(request)


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles, but forces revalidation on every request.

    Assets have no build step and no cache-busted filenames, so without an
    explicit Cache-Control header browsers fall back to heuristic caching
    and can keep serving an old module well after a file has changed.
    `no-cache` still allows a 304 round-trip (cheap), it just forbids
    serving a cached copy without checking first.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers.setdefault("cache-control", "no-cache")
        return response


def _s3_banner() -> str:
    """Which S3 credential path actually resolved, printed on every start —
    same guarantee as _llm_banner() below. Reports the profile name or a
    fingerprint (see llmclient.key_fingerprint) rather than calling
    config.resolve_credentials(): actually resolving a profile can hit the
    network (an SSO token refresh) and fail outright (e.g. an expired SSO
    session), and this line specifically must not be one more thing that can
    crash before it prints — see the ENV_FILE_OVERRODE comment below for why
    that already burned once.
    """
    auth = (f"profile={config.AWS_PROFILE}" if config.AWS_PROFILE
            else f"key={llmclient.key_fingerprint(config.AWS_ACCESS_KEY_ID)}"
            if config.AWS_ACCESS_KEY_ID else "key=(boto3 credential chain)")
    store = config.primary_store()
    line = f"S3: endpoint={store.label} bucket={config.BUCKET} {auth}"
    if not config.DEMO_ENABLED:
        line += " | demo catalog off (CI_DEMO=0)"
        if config.BUCKET == config.DEMO_BUCKET:
            # No demo and no bucket of your own leaves the app pointed at an
            # emulator that is not running. Say so here rather than leaving it
            # to be inferred from a connection error naming port 9600.
            line += " — and CI_BUCKET is unset, so nothing is readable"
    elif config.stores_split():
        # Two stores is the configuration most worth stating out loud: it is
        # the one where a path's bucket, not the global endpoint, decides
        # where it is read from.
        line += f" | demo bucket={config.DEMO_BUCKET} on {config.DEMO_S3_ENDPOINT}"
    return line


def _llm_banner() -> str:
    """What the LLM settings actually resolved to, printed on every start.

    Every one of these is a value a deployer *believes* they set and cannot
    otherwise confirm: which wire format their URL was detected as, and
    whether the key that reached the process is the key they configured (as a
    fingerprint — see llmclient.key_fingerprint). Both stay invisible until a
    request fails, and neither is guessable from the 401 or 404 that follows.
    """
    if not config.LLM_ENABLED:
        return "LLM features off (set CI_LLM_API_KEY to enable)"
    parts = [
        f"provider={llmclient.configured_provider()}",
        f"model={config.LLM_MODEL}",
        f"url={config.LLM_BASE_URL or 'https://api.anthropic.com (default)'}",
    ]
    if config.LLM_API_VERSION:
        parts.append(f"api-version={config.LLM_API_VERSION}")
    parts.append(f"key={llmclient.key_fingerprint(config.LLM_API_KEY)}")
    return "LLM: " + " ".join(parts)


def _cluster_banner() -> str:
    """This process's place in the deployment, printed on every start.

    Which role a container is running is the first question when a scaled-out
    deployment misbehaves ("why is nothing draining the queue?"), and it is
    invisible from the outside — a `web` replica and a `worker` share an image,
    a port and a health endpoint."""
    line = f"cluster: role={config.ROLE} node={config.NODE_ID}"
    if not config.CLUSTERED:
        return line + " (single process — CI_CLUSTERED unset)"
    return (line + f" clustered poll={config.CLUSTER_POLL_SECONDS}s "
                   f"lease={config.CLUSTER_LEASE_SECONDS}s")


def _run_preflight() -> None:
    """Refuse to start on a configuration that cannot survive a second
    replica, and warn about one that can only just.

    Fatal rather than a warning because every problem it catches is silent at
    runtime: a per-replica in-memory demo bucket answers queries, it just
    answers them differently on each replica. `CI_CLUSTER_PREFLIGHT=0` is the
    escape hatch for someone who knows exactly what they are doing (a single
    clustered process, say, sharing a database with nothing yet)."""
    problems = cluster.preflight()
    if problems and config.CLUSTER_PREFLIGHT:
        raise SystemExit(
            "[cash-intel] refusing to start clustered with this configuration:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n  (set CI_CLUSTER_PREFLIGHT=0 to start anyway, or CI_CLUSTERED=0 "
              "if this really is a single process)")
    for problem in problems:
        print(f"[cash-intel] cluster WARNING: {problem}")
    for note in cluster.warnings():
        print(f"[cash-intel] cluster note: {note}")


def _initialize_shared_state() -> None:
    """The first-run steps that write shared state, run by one replica only.

    Every one of these is "do it if it hasn't been done" — seed the demo
    bucket if empty, create the bootstrap admin if there are no accounts,
    seed the demo notebook if there are none. That is a check-then-act, and
    N replicas booting from the same image at the same second all pass the
    check: two bootstrap admins, two printed passwords, or a half-seeded
    bucket read while it is still being written. Under one lock they happen
    once; the replicas that lose simply carry on, since by then the work is
    done and their own checks correctly find nothing to do.

    Waits rather than skipping, because the loser still needs the *result* —
    a replica that starts serving before the demo bucket exists answers a
    query with an empty catalog.
    """
    with cluster.lock(cluster.BOOT_LOCK, wait=_BOOT_LOCK_WAIT) as lease:
        if lease is None:
            print("[cash-intel] another node is still initializing shared state "
                  "— continuing without waiting further")
        if seed.seed_bucket():
            print(f"[cash-intel] seeded demo data into s3://{config.DEMO_BUCKET}")
        restored = seed.restore_local_uploads()
        if restored:
            print(f"[cash-intel] restored {restored} uploaded file(s) into s3://{config.BUCKET}")
        registry.init()
        seed.seed_bootstrap_admin()
        if seed.seed_notebook_demo():
            print("[cash-intel] seeded demo notebook: Sales Overview")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # mcp_app's own lifespan (its Streamable HTTP session manager's task
    # group) must be entered explicitly — mounting alone doesn't run it
    # (research.md R2). Wraps the whole body so it's live for every request
    # this app ever serves, torn down after this app's own shutdown steps.
    async with mcp_app.lifespan(mcp_app):
        # Reported first and unconditionally reachable: emulator.start_if_embedded()
        # and seed.seed_bucket() below are the first things that touch S3, and an
        # AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY overridden by `.env` is a common
        # reason they fail outright (e.g. InvalidAccessKeyId) — this line is the
        # only thing that explains why in that case, so it must print before
        # either can crash the process and take the explanation down with it.
        if config.ENV_FILE_OVERRODE:
            print(f"[cash-intel] .env overrode {', '.join(config.ENV_FILE_OVERRODE)} "
                  f"— already set in the environment, but .env wins")
        print(f"[cash-intel] {_cluster_banner()}")
        if config.CLUSTERED:
            _run_preflight()
        print(f"[cash-intel] {_s3_banner()}")
        if emulator.start_if_embedded():
            print(f"[cash-intel] embedded S3 emulator on {config.DEMO_S3_ENDPOINT}")
        _initialize_shared_state()
        print(f"[cash-intel] loaded models: {', '.join(registry.models) or '(none)'}")
        print(f"[cash-intel] loaded agents: {', '.join(registry.agents) or '(none)'}")
        print(f"[cash-intel] {_llm_banner()}")
        # Scoped to this node when clustered: a restarting replica reaps the
        # runs *it* left mid-flight and nothing else. The unscoped sweep — the
        # original "nothing can be running if I am starting" — is still right
        # for a single process, and only for a single process, which is
        # exactly the distinction config.CLUSTERED draws.
        interrupted = registry.pipeline_store.sweep_interrupted(
            config.NODE_ID if config.CLUSTERED else None)
        if interrupted:
            print(f"[cash-intel] marked {interrupted} pipeline run(s) interrupted (restart mid-run)")
        if pipeline_jobs.start_worker(registry):
            print("[cash-intel] pipeline worker started")
        else:
            print(f"[cash-intel] no pipeline worker on this node (CI_ROLE={config.ROLE})")
        if cluster.start_watcher():
            print("[cash-intel] cluster watcher started — following peers' data/config changes")
        yield
        cluster.stop_watcher()
        pipeline_jobs.stop_worker()
        emulator.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Cash Intelligence", lifespan=lifespan)
    app.add_middleware(AuthMiddleware)
    app.include_router(api_router, prefix="/api")
    app.mount("/mcp", mcp_app)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception):
        # Without this, an uncaught exception falls through to Starlette's
        # default handler, which returns a plain-text "Internal Server
        # Error" body — api()/apiUpload() in lib.js always call res.json()
        # on the response, so that plain-text body surfaces to the user as
        # an opaque "Unexpected token 'I'... is not valid JSON" instead of
        # the actual error. Still re-raised after the response is built
        # (Starlette's ServerErrorMiddleware does this itself), so it's
        # logged same as before.
        return JSONResponse({"detail": f"internal error: {exc}"}, status_code=500)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")

    # SPA fallback: the frontend router (app/static/js/router.js) owns real
    # paths like /modelling/model/foo now, so a hard refresh or a pasted link
    # must still come back to the same shell. Registered last — the routes
    # above (this function's "/", api_router, and the /static mount) already
    # matched everything they own by the time Starlette reaches this one.
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("static/"):
            raise HTTPException(status_code=404)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
