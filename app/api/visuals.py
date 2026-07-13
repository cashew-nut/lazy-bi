"""Saved visuals CRUD (SQLite-backed)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import engine, measure_dsl
from ..auth import require_role
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
    just the shape being saved) plus FR-006 (undeclared parameter refs).
    Type-aware since specs/010-parameter-type-generalization: reuses
    engine.PARAM_TYPES/param_type_ok/coerce_param_value so a visual can
    never save a parameter whose values/default don't match its declared
    (or implicit int) type."""
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
        type_name = p.get("type") or "int"
        if type_name not in engine.PARAM_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"parameter '{name}' has unsupported type '{type_name}' "
                       f"(expected one of {sorted(engine.PARAM_TYPES)})",
            )
        values = p.get("values") or []
        if not values:
            raise HTTPException(status_code=400, detail=f"parameter '{name}' needs a non-empty list of values")
        bad = [v for v in values if not engine.param_type_ok(v, type_name)]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"parameter '{name}': value {bad[0]!r} does not match declared type '{type_name}'",
            )
        default = p.get("default")
        coerced_values = {engine.coerce_param_value(v, type_name) for v in values}
        if not engine.param_type_ok(default, type_name) or engine.coerce_param_value(default, type_name) not in coerced_values:
            raise HTTPException(
                status_code=400, detail=f"parameter '{name}' default is not one of its declared values"
            )
    declared_types = {p.get("name"): (p.get("type") or "int") for p in parameters if p.get("name")}
    for m in query.get("inline_measures") or []:
        expr = m.get("expr") or ""
        unknown = measure_dsl.referenced_parameter_names(expr) - seen
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"measure '{m.get('name')}': references undeclared parameter(s) {sorted(unknown)}",
            )
        # lag()'s periods argument requires an int-typed parameter — this one
        # position gets a save-time type check even without a full compile,
        # since it's a purely structural (not schema-dependent) fact about
        # the expression (specs/010-parameter-type-generalization US3)
        for pname in measure_dsl.lag_period_param_names(expr):
            if declared_types.get(pname) != "int":
                raise HTTPException(
                    status_code=400,
                    detail=f"measure '{m.get('name')}': lag()'s periods argument references parameter "
                           f"'{pname}' (type '{declared_types.get(pname)}'), which must be int",
                )


@router.get("/visuals")
def list_visuals():
    return registry.store.list()


@router.post("/visuals", status_code=201, dependencies=[Depends(require_role("author"))])
def create_visual(v: VisualIn):
    _validate_visual_spec(v.spec)
    return registry.store.create(v.name, v.model, v.spec)


@router.put("/visuals/{visual_id}", dependencies=[Depends(require_role("author"))])
def update_visual(visual_id: int, v: VisualIn):
    _validate_visual_spec(v.spec)
    updated = registry.store.update(visual_id, v.name, v.model, v.spec)
    if not updated:
        raise HTTPException(status_code=404, detail="visual not found")
    return updated


@router.delete("/visuals/{visual_id}", status_code=204, dependencies=[Depends(require_role("author"))])
def delete_visual(visual_id: int):
    if not registry.store.delete(visual_id):
        raise HTTPException(status_code=404, detail="visual not found")
