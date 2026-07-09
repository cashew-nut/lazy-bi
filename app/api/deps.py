"""Small helpers shared by routers."""
from fastapi import HTTPException

from ..registry import registry
from ..semantic import Model


def get_model(name: str) -> Model:
    model = registry.models.get(name)
    if not model:
        raise HTTPException(status_code=404, detail=f"unknown model '{name}'")
    return model
