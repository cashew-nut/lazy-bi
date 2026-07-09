"""Saved visuals CRUD (SQLite-backed)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..registry import registry

router = APIRouter(tags=["visuals"])


class VisualIn(BaseModel):
    name: str
    model: str
    spec: dict


@router.get("/visuals")
def list_visuals():
    return registry.store.list()


@router.post("/visuals", status_code=201)
def create_visual(v: VisualIn):
    return registry.store.create(v.name, v.model, v.spec)


@router.put("/visuals/{visual_id}")
def update_visual(visual_id: int, v: VisualIn):
    updated = registry.store.update(visual_id, v.name, v.model, v.spec)
    if not updated:
        raise HTTPException(status_code=404, detail="visual not found")
    return updated


@router.delete("/visuals/{visual_id}", status_code=204)
def delete_visual(visual_id: int):
    if not registry.store.delete(visual_id):
        raise HTTPException(status_code=404, detail="visual not found")
