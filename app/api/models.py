"""Semantic model endpoints: listing, dimension values, and the yaml editor."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_MEASURE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

from .. import config, engine, semantic
from ..registry import registry
from .deps import get_model

router = APIRouter(tags=["models"])


class YamlIn(BaseModel):
    yaml: str


class MeasureIn(BaseModel):
    name: str
    expr: str
    label: str = ""
    format: str = "number"
    description: str = ""


def _reload_or_400() -> None:
    try:
        registry.reload_models()
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _parse_or_400(text: str) -> semantic.Model:
    try:
        model = semantic.parse_model_text(text)
        semantic.resolve_imports(model, registry.dimension_bundles)
        return model
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/models")
def list_models():
    return [m.to_public() for m in registry.models.values()]


@router.post("/models/reload")
def reload_models():
    _reload_or_400()
    return {"loaded": list(registry.models)}


@router.post("/models/validate")
def validate_model(body: YamlIn):
    """Parse-check editor YAML; if it parses, also introspect the source schema
    so the editor can show the columns available to dimensions and measures."""
    try:
        parsed = semantic.parse_model_text(body.yaml)
        semantic.resolve_imports(parsed, registry.dimension_bundles)
    except semantic.ModelError as exc:
        return {"ok": False, "error": str(exc)}
    out = {
        "ok": True, "error": None,
        "model": {"name": parsed.name, "label": parsed.label,
                  "dimensions": len(parsed.dimensions), "measures": len(parsed.measures)},
    }
    try:
        schema = engine.scan(parsed).collect_schema()
        out["columns"] = [{"name": n, "dtype": str(t)} for n, t in schema.items()]
    except Exception as exc:
        out["columns"] = None
        out["schema_error"] = f"source not reachable: {exc}"
    return out


@router.post("/models", status_code=201)
def create_model(body: YamlIn):
    parsed = _parse_or_400(body.yaml)
    path = config.MODELS_DIR / f"{parsed.name}.yaml"
    if parsed.name in registry.models or path.exists():
        raise HTTPException(status_code=409, detail=f"model '{parsed.name}' already exists")
    path.write_text(body.yaml)
    _reload_or_400()
    return registry.models[parsed.name].to_public()


@router.get("/models/{name}/yaml")
def get_model_yaml(name: str):
    model = get_model(name)
    return {"name": name, "file": model.origin.name, "yaml": model.origin.read_text()}


@router.put("/models/{name}/yaml")
def put_model_yaml(name: str, body: YamlIn):
    model = get_model(name)
    parsed = _parse_or_400(body.yaml)
    other = registry.models.get(parsed.name)
    if other and other.origin != model.origin:
        raise HTTPException(status_code=409, detail=f"model '{parsed.name}' already exists in {other.origin.name}")
    model.origin.write_text(body.yaml)
    _reload_or_400()
    return registry.models[parsed.name].to_public()


@router.delete("/models/{name}", status_code=204)
def delete_model(name: str):
    model = get_model(name)
    model.origin.unlink()
    _reload_or_400()


@router.get("/models/{name}/schema")
def model_schema(name: str):
    """Source columns (post-join) with dtypes — feeds the measure editor's
    completion list."""
    model = get_model(name)
    try:
        schema = engine.scan(model).collect_schema()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"source not reachable: {exc}")
    return {"columns": [{"name": n, "dtype": str(t)} for n, t in schema.items()]}


@router.post("/models/{name}/measures", status_code=201)
def add_measure(name: str, m: MeasureIn):
    """Append a measure to the model's yaml file (comment-preserving) and
    hot-reload — the 'save to model' path of the measure lab."""
    model = get_model(name)
    if not _MEASURE_NAME.match(m.name):
        raise HTTPException(status_code=400, detail="measure name must be snake_case (a-z, 0-9, _)")
    if m.name in model.measures or m.name in model.dimensions:
        raise HTTPException(status_code=409, detail=f"'{m.name}' already exists on model '{name}'")
    if m.format not in ("number", "currency", "percent"):
        raise HTTPException(status_code=400, detail=f"unknown format '{m.format}'")
    try:
        semantic.compile_expr(m.expr, f"measure '{m.name}'")
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    entry = {"name": m.name}
    if m.label:
        entry["label"] = m.label
    if m.format != "number":
        entry["format"] = m.format
    if m.description:
        entry["description"] = m.description
    entry["expr"] = m.expr
    new_text = semantic.append_measure_yaml(model.origin.read_text(), entry)
    parsed = _parse_or_400(new_text)  # belt and braces before touching disk
    if m.name not in parsed.measures:
        raise HTTPException(status_code=500, detail="failed to place the measure in the yaml")
    model.origin.write_text(new_text)
    _reload_or_400()
    return registry.models[name].to_public()


@router.get("/models/{name}/dimensions/{dimension}/values")
def get_dimension_values(name: str, dimension: str):
    try:
        return engine.dimension_values(get_model(name), dimension)
    except (semantic.ModelError, engine.QueryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
