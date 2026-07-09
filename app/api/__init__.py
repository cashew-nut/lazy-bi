"""API routers, aggregated under /api by the app factory."""
from fastapi import APIRouter

from . import dashboards, explorer, models, query, visuals

api_router = APIRouter()
api_router.include_router(models.router)
api_router.include_router(query.router)
api_router.include_router(visuals.router)
api_router.include_router(dashboards.router)
api_router.include_router(explorer.router)
