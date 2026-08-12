"""CASH INTELLIGENCE — lightweight BI over S3 files, powered by polars.

Run:  uvicorn app.main:app --port 8080

App factory + lifecycle only; endpoints live in app/api/*, runtime state in
app/registry.py. In demo mode (no CI_S3_ENDPOINT) an embedded moto S3 server
is started and seeded with demo data if the bucket is empty.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from . import auth, config, emulator, llmclient, mcpserver, pipeline_jobs, seed
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # mcp_app's own lifespan (its Streamable HTTP session manager's task
    # group) must be entered explicitly — mounting alone doesn't run it
    # (research.md R2). Wraps the whole body so it's live for every request
    # this app ever serves, torn down after this app's own shutdown steps.
    async with mcp_app.lifespan(mcp_app):
        # Reported first and unconditionally reachable: emulator.start_if_embedded()
        # and seed.seed_bucket() below are the first things that touch S3, and a
        # shadowed AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY is a common reason they
        # fail outright (e.g. InvalidAccessKeyId) — this line is the only thing
        # that explains why in that case, so it must print before either can crash
        # the process and take the explanation down with it.
        if config.ENV_FILE_SHADOWED:
            print(f"[cash-intel] .env ignored for {', '.join(config.ENV_FILE_SHADOWED)} "
                  f"— already set in the environment, which wins")
        if emulator.start_if_embedded():
            print(f"[cash-intel] embedded S3 emulator on {config.S3_ENDPOINT}")
        if seed.seed_bucket():
            print(f"[cash-intel] seeded demo data into s3://{config.BUCKET}")
        registry.init()
        print(f"[cash-intel] loaded models: {', '.join(registry.models) or '(none)'}")
        print(f"[cash-intel] loaded agents: {', '.join(registry.agents) or '(none)'}")
        print(f"[cash-intel] {_llm_banner()}")
        seed.seed_bootstrap_admin()
        if seed.seed_notebook_demo():
            print("[cash-intel] seeded demo notebook: Sales Overview")
        interrupted = registry.pipeline_store.sweep_interrupted()
        if interrupted:
            print(f"[cash-intel] marked {interrupted} pipeline run(s) interrupted (restart mid-run)")
        pipeline_jobs.start_worker(registry)
        yield
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
