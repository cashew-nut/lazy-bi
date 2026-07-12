"""Saved visuals CRUD (SQLite-backed)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import measure_dsl
from ..registry import registry

router = APIRouter(tags=["visuals"])


class VisualIn(BaseModel):
    name: str
    model: str
    spec: dict


def _validate_visual_spec(spec: dict) -> None:
    """Structural-only checks on a visual's declared parameters and any
    inline measure referencing them — mirrors app.engine.resolve_parameter_
    values' declaration checks (no query-time selection to validate here,
    just the shape being saved) plus FR-006 (undeclared parameter refs)."""
    query = spec.get("query") or {}
    parameters = query.get("parameters") or []
    seen: set = set()
    for p in parameters:
        name = p.get("name")
        if not name:
            raise HTTPException(status_code=400, detail="parameter needs a name")
        if name in seen:
            raise HTTPException(status_code=400, detail=f"duplicate parameter '{name}'")
        seen.add(name)
        values = p.get("values") or []
        if not values:
            raise HTTPException(status_code=400, detail=f"parameter '{name}' needs a non-empty list of values")
        if p.get("default") not in values:
            raise HTTPException(
                status_code=400, detail=f"parameter '{name}' default is not one of its declared values"
            )
    for m in query.get("inline_measures") or []:
        expr = m.get("expr") or ""
        unknown = measure_dsl.referenced_parameter_names(expr) - seen
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"measure '{m.get('name')}': references undeclared parameter(s) {sorted(unknown)}",
            )


@router.get("/visuals")
def list_visuals():
    return registry.store.list()


@router.post("/visuals", status_code=201)
def create_visual(v: VisualIn):
    _validate_visual_spec(v.spec)
    return registry.store.create(v.name, v.model, v.spec)


@router.put("/visuals/{visual_id}")
def update_visual(visual_id: int, v: VisualIn):
    _validate_visual_spec(v.spec)
    updated = registry.store.update(visual_id, v.name, v.model, v.spec)
    if not updated:
        raise HTTPException(status_code=404, detail="visual not found")
    return updated


@router.delete("/visuals/{visual_id}", status_code=204)
def delete_visual(visual_id: int):
    if not registry.store.delete(visual_id):
        raise HTTPException(status_code=404, detail="visual not found")
