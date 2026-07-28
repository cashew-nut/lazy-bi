"""Semantic model endpoints: listing, dimension values, and the yaml editor."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

_MEASURE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

from .. import engine, measure_dsl, semantic
from ..auth import User, require_role
from ..registry import registry
from .deps import get_model

router = APIRouter(tags=["models"])

# Route roles (specs/011-session-auth-rbac/contracts/auth-api.md): raw
# model YAML (which can carry frame: blocks — application-code trust,
# Principle VI) is admin; scalar measure authoring is author, with framed
# measures escalating to admin; reads and dry-run validation are open to
# any authenticated user (the middleware guarantees at least that).


class YamlIn(BaseModel):
    yaml: str


class SpineSpec(BaseModel):
    start: str
    end: str
    match: str = "overlap"   # see semantic.MATCH_MODES


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
    grain: str | None = None
    synonyms: list[str] = []


class MeasureSpec(BaseModel):
    name: str
    expr: str
    label: str = ""
    format: str = "number"
    description: str = ""
    # framed measures (multi-step derived-frame logic) round-trip through the
    # guided form like any other spec field — the measure-lab save path gates
    # who may *save* one (admin, via _require_frame_privilege), not whether
    # the form can see/edit one that already exists on the model. The
    # whole-model yaml save routes that could smuggle a frame in are
    # admin-gated for the same reason (spec 011, Principle VI).
    frame: Optional[str] = None
    frame_emits: list[str] = []
    synonyms: list[str] = []


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
    match: str = "overlap"    # `how: between` only — see semantic.MATCH_MODES


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
    # the measure lab (measurelab.js) never surfaces this field or
    # `description` — it only ever sends name/label/format/expr, and
    # update_measure() replaces the measure's whole yaml block — so, like
    # description, re-saving an existing measure through the lab (not the
    # guided model form or the raw yaml editor) drops synonyms hand-authored
    # outside it. Pre-existing, accepted narrowness of that editor.
    synonyms: list[str] = []


def _reload_or_400() -> None:
    try:
        registry.reload_all()
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _resolve(model: semantic.Model) -> semantic.Model:
    """Merge a freshly-parsed model's imported dimensions, then — for a
    multi-fact model — its facts' shared catalog, both against what's
    currently loaded. Raises semantic.ModelError."""
    semantic.resolve_imports(model, registry.dimension_bundles)
    semantic.resolve_facts(model, registry.models)
    return model


def _parse_or_400(text: str) -> semantic.Model:
    try:
        return _resolve(semantic.parse_model_text(text))
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _forbid_if_locked(model: semantic.Model) -> None:
    if model.locked:
        raise HTTPException(
            status_code=403,
            detail=f"model '{model.name}' is part of the built-in demo catalog and can't be edited",
        )


def _read_model_text(model: semantic.Model) -> str:
    return registry.read_model_text(model)


def _model_out(parsed: semantic.Model) -> dict:
    """Summary + schema introspection, shared by /validate and /generate —
    each renders/parses differently but reports the same shape."""
    out = {
        "ok": True, "error": None,
        "model": {"name": parsed.name, "label": parsed.label,
                  "kind": "composite" if parsed.is_composite else "fact",
                  "dimensions": len(parsed.dimensions), "measures": len(parsed.measures)},
    }
    if parsed.facts:
        # which facts resolved, and what they conform on — the editor's feedback
        # on a `facts:` list, whether or not the model also has a source
        out["facts"] = [
            {"alias": b.alias, "model": b.model.name,
             "measures": len(b.model.measures)}
            for b in parsed.fact_bindings if not b.host
        ]
        out["shared_dimensions"] = list(semantic.shared_dimensions(parsed.fact_bindings))
    if parsed.is_composite:
        out["columns"] = None   # no source of its own to introspect
        return out
    try:
        schema = engine.scan(parsed).collect_schema()
        out["columns"] = [{"name": n, "dtype": str(t)} for n, t in schema.items()]
    except Exception as exc:
        out["columns"] = None
        out["schema_error"] = f"source not reachable: {exc}"
    return out


@router.get("/models")
def list_models():
    return [m.to_public() for m in registry.models.values()]


@router.post("/models/reload", dependencies=[Depends(require_role("admin"))])
def reload_models():
    _reload_or_400()
    return {"loaded": list(registry.models)}


@router.post("/models/validate")
def validate_model(body: YamlIn):
    """Parse-check editor YAML; if it parses, also introspect the source schema
    so the editor can show the columns available to dimensions and measures."""
    try:
        parsed = _resolve(semantic.parse_model_text(body.yaml))
    except semantic.ModelError as exc:
        return {"ok": False, "error": str(exc)}
    return _model_out(parsed)


@router.post("/models/generate", dependencies=[Depends(require_role("author"))])
def generate_model_yaml(spec: ModelSpec):
    """Render the guided form's structured spec to canonical YAML, then run the
    same parse + schema introspection as /models/validate so the form gets the
    document and its verdict (with post-join columns) in one call."""
    text = semantic.spec_to_yaml(spec.model_dump())
    try:
        parsed = _resolve(semantic.parse_model_text(text))
    except semantic.ModelError as exc:
        return {"ok": False, "error": str(exc), "yaml": text, "columns": None}
    out = _model_out(parsed)
    out["yaml"] = text
    return out


@router.get("/models/{name}/spec")
def get_model_spec(name: str):
    """The model's yaml re-parsed (unresolved — native dimensions only) into
    the structured spec the guided form edits."""
    model = get_model(name)
    try:
        parsed = semantic.parse_model_text(_read_model_text(model))
        spec = semantic.model_to_spec(parsed)
    except semantic.ModelError as exc:  # bad stored state, or a multi-fact model
        raise HTTPException(status_code=400, detail=str(exc))
    return {"name": name, "file": model.origin.name if model.locked else None,
            "locked": model.locked, "spec": spec}


@router.post("/models", status_code=201, dependencies=[Depends(require_role("admin"))])
def create_model(body: YamlIn):
    """Always creates a local model (app/localmodelstore.py) — the app never
    writes into the committed models/ directory; that catalog only changes
    via a code change. See _forbid_if_locked for why the built-in catalog
    can't be edited through this API either."""
    parsed = _parse_or_400(body.yaml)
    if parsed.name in registry.models:
        raise HTTPException(status_code=409, detail=f"model '{parsed.name}' already exists")
    registry.local_model_store.create(parsed.name, body.yaml)
    _reload_or_400()
    return registry.models[parsed.name].to_public()


@router.get("/models/{name}/yaml")
def get_model_yaml(name: str):
    model = get_model(name)
    return {"name": name, "file": model.origin.name if model.locked else None,
            "yaml": _read_model_text(model)}


@router.put("/models/{name}/yaml", dependencies=[Depends(require_role("admin"))])
def put_model_yaml(name: str, body: YamlIn):
    model = get_model(name)
    _forbid_if_locked(model)
    parsed = _parse_or_400(body.yaml)
    other = registry.models.get(parsed.name)
    if other and other.name != model.name:
        raise HTTPException(status_code=409, detail=f"model '{parsed.name}' already exists")
    registry.local_model_store.update(name, body.yaml)
    _reload_or_400()
    return registry.models[parsed.name].to_public()


@router.delete("/models/{name}", status_code=204,
               dependencies=[Depends(require_role("admin"))])
def delete_model(name: str):
    model = get_model(name)
    _forbid_if_locked(model)
    registry.local_model_store.delete(name)
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
    parameters: list[dict] = []    # the visual's currently-declared parameters, if any


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
        # there is no "current selection" while still drafting — check against
        # each declared parameter's default, same as a query with no override
        parameter_values = engine.resolve_parameter_values(body.parameters, {})
        measure_dsl.compile_measure(body.expr, schema, alias="_check", parameter_values=parameter_values)
    except (measure_dsl.MeasureCompileError, engine.QueryError) as exc:
        return {"ok": False, "error": str(exc), "window": False}
    return {"ok": True, "error": None, "window": is_window}


def _single_fact_or_400(name: str) -> semantic.Model:
    """A measure belongs to one fact table. A multi-fact model only borrows its
    facts' measures, so authoring one here would write a `measures:` block the
    parser then rejects — say which model to edit instead."""
    model = get_model(name)
    if model.is_composite:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' is a multi-fact model — its measures belong to the facts it lists ("
                   f"{', '.join(b.model.name for b in model.fact_bindings)}); add or edit the "
                   f"measure on one of those",
        )
    return model


def _validate_measure_body(model: semantic.Model, m: MeasureIn) -> None:
    if m.format not in ("number", "currency", "percent"):
        raise HTTPException(status_code=400, detail=f"unknown format '{m.format}'")
    if m.expr and measure_dsl.referenced_parameter_names(m.expr):
        # parameters are visual-scoped context a model measure never has —
        # this construct can only ever be saved as an inline (visual) measure
        raise HTTPException(
            status_code=400,
            detail=f"measure '{m.name}': references a parameter — parameterized measures can only "
                   "be saved to a visual, not to the shared model",
        )
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
    if m.synonyms:
        entry["synonyms"] = list(m.synonyms)
    entry["expr"] = m.expr
    return entry


def _require_frame_privilege(user: User, m: MeasureIn) -> None:
    """The frame: escape hatch is eval-based, application-code trust —
    saving one requires the admin role, not just author (Principle VI)."""
    if (m.frame or m.frame_emits) and not user.has_role("admin"):
        raise HTTPException(status_code=403,
                            detail="framed measures require the admin role")


@router.post("/models/{name}/measures", status_code=201)
def add_measure(name: str, m: MeasureIn, user: User = Depends(require_role("author"))):
    """Append a measure to the model's yaml file (comment-preserving) and
    hot-reload — the 'save to model' path of the measure lab."""
    _require_frame_privilege(user, m)
    model = _single_fact_or_400(name)
    if not _MEASURE_NAME.match(m.name):
        raise HTTPException(status_code=400, detail="measure name must be snake_case (a-z, 0-9, _)")
    if m.name in model.measures or m.name in model.dimensions:
        raise HTTPException(status_code=409, detail=f"'{m.name}' already exists on model '{name}'")
    _validate_measure_body(model, m)

    entry = _measure_entry(m)
    new_text = semantic.append_measure_yaml(_read_model_text(model), entry)
    parsed = _parse_or_400(new_text)  # belt and braces before persisting
    if m.name not in parsed.measures:
        raise HTTPException(status_code=500, detail="failed to place the measure in the yaml")
    registry.write_model_text(model, new_text)
    _reload_or_400()
    registry.store.record_measure_provenance(
        name, m.name, "create", user.display_name, expr=m.expr,
        frame=m.frame, frame_emits=m.frame_emits or None, user_id=user.id,
    )
    return registry.models[name].to_public()


@router.put("/models/{name}/measures/{measure_name}")
def update_measure(name: str, measure_name: str, m: MeasureIn,
                   user: User = Depends(require_role("author"))):
    """Rewrite an existing measure's yaml block in place and hot-reload."""
    _require_frame_privilege(user, m)
    model = _single_fact_or_400(name)
    if measure_name not in model.measures:
        raise HTTPException(status_code=404, detail=f"unknown measure '{measure_name}' on model '{name}'")
    if m.name != measure_name:
        raise HTTPException(status_code=400, detail="body 'name' must match the measure being updated")
    _validate_measure_body(model, m)

    entry = _measure_entry(m)
    new_text = semantic.replace_measure_yaml(_read_model_text(model), measure_name, entry)
    parsed = _parse_or_400(new_text)  # belt and braces before persisting
    if measure_name not in parsed.measures:
        raise HTTPException(status_code=500, detail="failed to place the measure in the yaml")
    registry.write_model_text(model, new_text)
    _reload_or_400()
    registry.store.record_measure_provenance(
        name, m.name, "update", user.display_name, expr=m.expr,
        frame=m.frame, frame_emits=m.frame_emits or None, user_id=user.id,
    )
    return registry.models[name].to_public()


@router.delete("/models/{name}/measures/{measure_name}", status_code=204)
def delete_measure(name: str, measure_name: str,
                   user: User = Depends(require_role("author"))):
    model = _single_fact_or_400(name)
    if measure_name not in model.measures:
        raise HTTPException(status_code=404, detail=f"unknown measure '{measure_name}' on model '{name}'")
    new_text = semantic.remove_measure_yaml(_read_model_text(model), measure_name)
    _parse_or_400(new_text)  # belt and braces before persisting
    registry.write_model_text(model, new_text)
    _reload_or_400()
    registry.store.record_measure_provenance(
        name, measure_name, "delete", user.display_name, user_id=user.id)


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
