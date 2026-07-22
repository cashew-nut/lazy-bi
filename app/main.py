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

from . import auth, config, emulator, pipeline_jobs, seed
from .api import api_router
from .registry import registry

STATIC_DIR = Path(__file__).parent / "static"

# The only /api requests answerable without an identity: signing in, and a
# liveness probe. Everything else is default-deny — a route cannot opt out
# by forgetting a dependency (specs/011-session-auth-rbac/research.md R3).
PUBLIC_API = {("POST", "/api/auth/login"), ("GET", "/api/health")}


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticates every /api request (401 otherwise) and stashes the
    principal on request.state.user; routes layer authorization on top via
    auth.require_role. Static assets and the SPA shell stay public — they
    are code, not data; the SPA renders its login view when /api/auth/me
    says 401.

    Credential precedence: an Authorization: Bearer header is used
    exclusively when present (no cookie fallback — one identity, never a
    merge). Cookie-authenticated mutations must carry the CSRF header
    X-Requested-With: fetch; bearer requests are exempt because cross-site
    pages cannot set an Authorization header.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith("/api") or (request.method, path) in PUBLIC_API:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    if emulator.start_if_embedded():
        print(f"[cash-intel] embedded S3 emulator on {config.S3_ENDPOINT}")
    if seed.seed_bucket():
        print(f"[cash-intel] seeded demo data into s3://{config.BUCKET}")
    if seed.seed_raw_data_bucket():
        print(f"[cash-intel] seeded raw data into s3://{config.RAW_BUCKET}")
    registry.init()
    print(f"[cash-intel] loaded models: {', '.join(registry.models) or '(none)'}")
    seed.seed_bootstrap_admin()
    if seed.seed_notebook_demo():
        print("[cash-intel] seeded demo notebook: Recruitment Overview")
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
