"""Semantic model endpoints: listing, dimension values, and the yaml editor."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

_MEASURE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

from .. import config, engine, measure_dsl, semantic
from ..auth import require_measure_author
from ..registry import registry
from .deps import get_model

router = APIRouter(tags=["models"])


class YamlIn(BaseModel):
    yaml: str


class SpineSpec(BaseModel):
    start: str
    end: str


class GeoSpec(BaseModel):
    lat: str
    lon: str


class SourceSpec(BaseModel):
    path: str
    format: str = "parquet"


class DimensionSpec(BaseModel):
    name: str
    column: str | None = None
    label: str = ""
    type: str = "categorical"
    description: str = ""
    spine: SpineSpec | None = None
    geo: GeoSpec | None = None


class MeasureSpec(BaseModel):
    name: str
    expr: str
    label: str = ""
    format: str = "number"
    description: str = ""
    # framed measures (multi-step derived-frame logic) round-trip through the
    # guided form like any other spec field — MeasureIn's auth gate governs
    # who may *save* one via the measure-lab path, not whether the form can
    # see/edit one that already exists on the model (see spec 008 §edge
    # cases: "hand-edits outside the API" are out of scope to reconcile, and
    # the whole-model yaml save routes below have never required auth).
    frame: Optional[str] = None
    frame_emits: list[str] = []


class JoinSpec(BaseModel):
    name: str
    path: str
    format: str = "parquet"
    left_on: list[str] = []
    right_on: list[str] = []
    how: str = "left"


class ImportSpec(BaseModel):
    bundle: str
    anchor_dataset: str
    left_on: list[str] = []
    right_on: list[str] = []
    how: str = "left"
    datasets: list[str] | None = None


class ModelSpec(BaseModel):
    """Structured form of a model — what the guided modelling form edits.
    POST /models/generate renders it to YAML; GET /models/{name}/spec is the
    inverse for opening an existing file in the form."""
    name: str
    label: str = ""
    description: str = ""
    source: SourceSpec
    joins: list[JoinSpec] = []
    dimension_imports: list[ImportSpec] = []
    dimensions: list[DimensionSpec] = []
    measures: list[MeasureSpec] = []


class MeasureIn(BaseModel):
    name: str
    expr: str
    label: str = ""
    format: str = "number"
    description: str = ""
    # framed measures (multi-step derived-frame logic) are an authenticated-
    # model-measure-only construct — never available to inline/query-time
    # measures. See specs/008-safe-measure-compilation.
    frame: Optional[str] = None
    frame_emits: list[str] = []


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


@router.post("/models/generate")
def generate_model_yaml(spec: ModelSpec):
    """Render the guided form's structured spec to canonical YAML, then run the
    same parse + schema introspection as /models/validate so the form gets the
    document and its verdict (with post-join columns) in one call."""
    text = semantic.spec_to_yaml(spec.model_dump())
    try:
        parsed = semantic.parse_model_text(text)
        semantic.resolve_imports(parsed, registry.dimension_bundles)
    except semantic.ModelError as exc:
        return {"ok": False, "error": str(exc), "yaml": text, "columns": None}
    out = {
        "ok": True, "error": None, "yaml": text,
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


@router.get("/models/{name}/spec")
def get_model_spec(name: str):
    """The model's yaml re-parsed (unresolved — native dimensions only) into
    the structured spec the guided form edits."""
    model = get_model(name)
    try:
        parsed = semantic.parse_model_text(model.origin.read_text())
    except semantic.ModelError as exc:  # file edited into a bad state on disk
        raise HTTPException(status_code=400, detail=str(exc))
    return {"name": name, "file": model.origin.name, "spec": semantic.model_to_spec(parsed)}


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


class MeasureCheckIn(BaseModel):
    """A single measure's would-be definition, checked without needing a
    saved model to validate against — the guided form's source of live,
    per-row ✓/✗ feedback while a measure is still being typed. Mirrors the
    checks `_parse_model`/`_validate_measure_body` run at load/save time, but
    takes candidate names straight from the caller instead of a live scan."""
    expr: str = ""
    frame: Optional[str] = None
    frame_emits: list[str] = []
    columns: list[str] = []        # source column names, for a plain/window-free expr
    measure_names: list[str] = []  # sibling measure names, for a window expr (running_total/lag)


@router.post("/measures/check")
def check_measure(body: MeasureCheckIn):
    if body.frame:
        # a framed measure still needs its aggregation expr — an empty one
        # compiles fine as a no-op `exec` (validate_frame wouldn't catch it)
        # but load_model's compile_expr(m.expr) always runs and fails on it
        if not body.expr.strip():
            return {"ok": False, "error": "measure needs an expression", "window": False}
        try:
            semantic.validate_frame(body.frame, "measure")
            semantic.compile_expr(body.expr, "measure")
        except semantic.ModelError as exc:
            return {"ok": False, "error": str(exc), "window": False}
        return {"ok": True, "error": None, "window": False}
    if body.frame_emits:
        return {"ok": False, "error": "'frame_emits' needs a 'frame'", "window": False}
    if not body.expr.strip():
        return {"ok": False, "error": "measure needs an expression", "window": False}
    try:
        is_window = measure_dsl.is_window_expr(body.expr)
        schema = set(body.measure_names) if is_window else set(body.columns)
        measure_dsl.compile_measure(body.expr, schema, alias="_check")
    except measure_dsl.MeasureCompileError as exc:
        return {"ok": False, "error": str(exc), "window": False}
    return {"ok": True, "error": None, "window": is_window}


def _validate_measure_body(model: semantic.Model, m: MeasureIn) -> None:
    if m.format not in ("number", "currency", "percent"):
        raise HTTPException(status_code=400, detail=f"unknown format '{m.format}'")
    if m.frame:
        # the framed-measure construct is authenticated-model-measure-only:
        # a load-time syntax check now, the real compile_frame run happens
        # against a live scan at query time (see app/semantic.py).
        try:
            semantic.validate_frame(m.frame, f"measure '{m.name}'")
        except semantic.ModelError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    elif m.frame_emits:
        raise HTTPException(status_code=400, detail=f"measure '{m.name}': 'frame_emits' needs a 'frame'")
    else:
        try:
            is_window = measure_dsl.is_window_expr(m.expr)
        except measure_dsl.MeasureCompileError as exc:
            raise HTTPException(status_code=400, detail=f"measure '{m.name}': {exc}")
        if is_window:
            # window measures (running_total()/lag()) read sibling *measures*,
            # not raw source columns — no need to touch the live source at all
            schema = set(model.measures)
        else:
            try:
                schema = engine.scan(model).collect_schema()
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"source not reachable: {exc}")
        try:
            measure_dsl.compile_measure(m.expr, schema, alias=m.name)
        except measure_dsl.MeasureCompileError as exc:
            raise HTTPException(status_code=400, detail=f"measure '{m.name}': {exc}")


def _measure_entry(m: MeasureIn) -> dict:
    entry = {"name": m.name}
    if m.label:
        entry["label"] = m.label
    if m.format != "number":
        entry["format"] = m.format
    if m.description:
        entry["description"] = m.description
    if m.frame:
        entry["frame"] = m.frame
    if m.frame_emits:
        entry["frame_emits"] = list(m.frame_emits)
    entry["expr"] = m.expr
    return entry


@router.post("/models/{name}/measures", status_code=201)
def add_measure(name: str, m: MeasureIn, author: str = Depends(require_measure_author)):
    """Append a measure to the model's yaml file (comment-preserving) and
    hot-reload — the 'save to model' path of the measure lab."""
    model = get_model(name)
    if not _MEASURE_NAME.match(m.name):
        raise HTTPException(status_code=400, detail="measure name must be snake_case (a-z, 0-9, _)")
    if m.name in model.measures or m.name in model.dimensions:
        raise HTTPException(status_code=409, detail=f"'{m.name}' already exists on model '{name}'")
    _validate_measure_body(model, m)

    entry = _measure_entry(m)
    new_text = semantic.append_measure_yaml(model.origin.read_text(), entry)
    parsed = _parse_or_400(new_text)  # belt and braces before touching disk
    if m.name not in parsed.measures:
        raise HTTPException(status_code=500, detail="failed to place the measure in the yaml")
    model.origin.write_text(new_text)
    _reload_or_400()
    registry.store.record_measure_provenance(
        name, m.name, "create", author, expr=m.expr, frame=m.frame, frame_emits=m.frame_emits or None,
    )
    return registry.models[name].to_public()


@router.put("/models/{name}/measures/{measure_name}")
def update_measure(name: str, measure_name: str, m: MeasureIn, author: str = Depends(require_measure_author)):
    """Rewrite an existing measure's yaml block in place and hot-reload."""
    model = get_model(name)
    if measure_name not in model.measures:
        raise HTTPException(status_code=404, detail=f"unknown measure '{measure_name}' on model '{name}'")
    if m.name != measure_name:
        raise HTTPException(status_code=400, detail="body 'name' must match the measure being updated")
    _validate_measure_body(model, m)

    entry = _measure_entry(m)
    new_text = semantic.replace_measure_yaml(model.origin.read_text(), measure_name, entry)
    parsed = _parse_or_400(new_text)  # belt and braces before touching disk
    if measure_name not in parsed.measures:
        raise HTTPException(status_code=500, detail="failed to place the measure in the yaml")
    model.origin.write_text(new_text)
    _reload_or_400()
    registry.store.record_measure_provenance(
        name, m.name, "update", author, expr=m.expr, frame=m.frame, frame_emits=m.frame_emits or None,
    )
    return registry.models[name].to_public()


@router.delete("/models/{name}/measures/{measure_name}", status_code=204)
def delete_measure(name: str, measure_name: str, author: str = Depends(require_measure_author)):
    model = get_model(name)
    if measure_name not in model.measures:
        raise HTTPException(status_code=404, detail=f"unknown measure '{measure_name}' on model '{name}'")
    new_text = semantic.remove_measure_yaml(model.origin.read_text(), measure_name)
    _parse_or_400(new_text)  # belt and braces before touching disk
    model.origin.write_text(new_text)
    _reload_or_400()
    registry.store.record_measure_provenance(name, measure_name, "delete", author)


@router.get("/models/{name}/measures/{measure_name}/history")
def measure_history(name: str, measure_name: str):
    get_model(name)  # 404 for unknown model
    return registry.store.measure_history(name, measure_name)


@router.get("/models/{name}/dimensions/{dimension}/values")
def get_dimension_values(name: str, dimension: str):
    try:
        return engine.dimension_values(get_model(name), dimension)
    except (semantic.ModelError, engine.QueryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
