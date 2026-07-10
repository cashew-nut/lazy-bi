"""CASH INTELLIGENCE — lightweight BI over S3 files, powered by polars.

Run:  uvicorn app.main:app --port 8080

App factory + lifecycle only; endpoints live in app/api/*, runtime state in
app/registry.py. In demo mode (no CI_S3_ENDPOINT) an embedded moto S3 server
is started and seeded with demo data if the bucket is empty.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from . import config, emulator, seed
from .api import api_router
from .registry import registry

STATIC_DIR = Path(__file__).parent / "static"


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
    registry.init()
    print(f"[cash-intel] loaded models: {', '.join(registry.models) or '(none)'}")
    yield
    emulator.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Cash Intelligence", lifespan=lifespan)
    app.include_router(api_router, prefix="/api")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
