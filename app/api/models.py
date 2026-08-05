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


class RelationSpec(BaseModel):
    """A relation from one of the model's datasets to a sibling dataset."""
    to: str
    left_on: list[str] = []
    right_on: list[str] = []
    how: str = "left"


class DatasetSpec(BaseModel):
    """One table the model reads: its source, what it declares, and how it
    relates to the model's other datasets. Datasets that relate to nothing
    else are separate fact tables — see semantic.ModelPart."""
    name: str
    source: SourceSpec
    joins: list[RelationSpec] = []
    dimensions: list[DimensionSpec] = []
    measures: list[MeasureSpec] = []


class ImportSpec(BaseModel):
    bundle: str
    from_dataset: str = ""    # which of this model's datasets relates to the bundle
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
    datasets: list[DatasetSpec] = []
    dimension_imports: list[ImportSpec] = []


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
    """Split a freshly-parsed model into its fact tables and merge in each
    one's imported dimensions, against the bundles currently loaded. Raises
    semantic.ModelError."""
    return semantic.resolve_model(model, registry.dimension_bundles)


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
        "parts": [
            {"name": p.name, "datasets": list(p.datasets),
             "dimensions": list(p.model.dimensions), "measures": list(p.model.measures)}
            for p in parsed.parts
        ],
        # what every fact table can be grouped by at once — the same list as
        # `dimensions` for a single-part model, and the interesting one when
        # there are several: which dimensions survived the conform
        "shared_dimensions": list(parsed.dimensions),
    }
    # post-join columns per fact table, keyed by part — the form resolves a
    # dimension's dtype and a measure's completion pool against the part that
    # owns it, so a model holding several tables introspects all of them
    columns: dict[str, list[dict]] = {}
    errors = []
    for part in parsed.parts:
        try:
            schema = engine.scan(part.model).collect_schema()
        except Exception as exc:
            errors.append(f"{part.name}: {exc}")
            continue
        columns[part.name] = [{"name": n, "dtype": str(t)} for n, t in schema.items()]
    out["part_columns"] = columns
    # `columns` stays the flat union every existing caller reads
    seen: set[str] = set()
    flat = [c for cols in columns.values() for c in cols
            if not (c["name"] in seen or seen.add(c["name"]))]
    out["columns"] = flat if columns else None
    if errors:
        out["schema_error"] = "source not reachable: " + "; ".join(errors)
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
    except semantic.ModelError as exc:  # bad stored state
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
    registry.local_model_store.update(
        name, body.yaml, new_name=parsed.name if parsed.name != name else None)
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
        schema = engine.scan_schema(model)
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
    """A measure belongs to one fact table, and the measure lab names a model
    rather than a table — so it only authors against a model that has just
    one. With several, which one the expression is scoped to is the whole
    question, and the guided form (which edits measures per dataset) is where
    to answer it."""
    model = get_model(name)
    if model.is_composite:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' holds {len(model.parts)} unrelated fact tables ("
                   f"{', '.join(p.name for p in model.parts)}) — a measure belongs to one of them, "
                   f"so add or edit it on that dataset in the model form",
        )
    return model


def _apply_measure(model: semantic.Model, text: str, name: str,
                   entry: Optional[dict]) -> str:
    """`text` with measure `name` added, replaced (`entry`) or removed (None).

    A file written the terse `source:` way carries its measures in one
    top-level block, and is edited in place so its comments survive — that is
    the shape every hand-written model in models/ uses. A `datasets:`-shape
    file nests each measure block inside its dataset entry; those are
    re-rendered from the spec instead, which is what the guided form that
    generates them does on every save anyway."""
    if any(line.rstrip() == "measures:" for line in text.split("\n")):
        if entry is None:
            return semantic.remove_measure_yaml(text, name)
        return (semantic.replace_measure_yaml(text, name, entry) if name in model.measures
                else semantic.append_measure_yaml(text, entry))
    spec = semantic.model_to_spec(semantic.parse_model_text(text))
    # an existing measure is rewritten where it already lives (a fact table can
    # span several related datasets); a new one lands on the root
    target = next((d for d in spec["datasets"]
                   if any(x["name"] == name for x in d["measures"])), None)
    if target is None:
        owner = model.parts[0].name
        target = next((d for d in spec["datasets"] if d["name"] == owner), None)
    if target is None:
        raise HTTPException(status_code=500, detail=f"model '{model.name}' has no dataset to hold '{name}'")
    measures = list(target["measures"])
    at = next((i for i, m in enumerate(measures) if m["name"] == name), None)
    if entry is None:
        if at is not None:
            measures.pop(at)
    elif at is None:
        measures.append(entry)
    else:
        measures[at] = entry
    target["measures"] = measures
    return semantic.spec_to_yaml(spec)


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
                schema = engine.scan_schema(model)
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
    new_text = _apply_measure(model, _read_model_text(model), m.name, entry)
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
    new_text = _apply_measure(model, _read_model_text(model), measure_name, entry)
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
    new_text = _apply_measure(model, _read_model_text(model), measure_name, None)
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
    except Exception as exc:  # polars errors from a stale/misrouted source surface as 400s
        raise HTTPException(status_code=400, detail=f"dimension values failed: {exc}")
