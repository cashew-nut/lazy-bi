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

from . import config, emulator, seed
from .api import api_router
from .registry import registry

STATIC_DIR = Path(__file__).parent / "static"


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
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
