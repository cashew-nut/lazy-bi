"""API routers, aggregated under /api by the app factory."""
from fastapi import APIRouter

from . import (
    auth, chat, composer, dashboards, datasets, dimensions, explorer, memories,
    models, notebooks, pipelines, query, sandbox, users, visuals,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(models.router)
api_router.include_router(memories.router)
api_router.include_router(dimensions.router)
api_router.include_router(pipelines.router)
api_router.include_router(sandbox.router)
api_router.include_router(query.router)
api_router.include_router(visuals.router)
api_router.include_router(dashboards.router)
api_router.include_router(notebooks.router)
api_router.include_router(explorer.router)
api_router.include_router(datasets.router)
api_router.include_router(chat.router)
api_router.include_router(composer.router)
