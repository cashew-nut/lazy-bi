"""Semantic model endpoints: listing, dimension values, and the yaml editor."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, engine, semantic
from ..registry import registry
from .deps import get_model

router = APIRouter(tags=["models"])


class YamlIn(BaseModel):
    yaml: str


def _reload_or_400() -> None:
    try:
        registry.reload_models()
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _parse_or_400(text: str) -> semantic.Model:
    try:
        return semantic.parse_model_text(text)
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


@router.get("/models/{name}/dimensions/{dimension}/values")
def get_dimension_values(name: str, dimension: str):
    try:
        return engine.dimension_values(get_model(name), dimension)
    except (semantic.ModelError, engine.QueryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
