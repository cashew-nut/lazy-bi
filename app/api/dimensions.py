"""Dimension bundle endpoints: listing and the yaml read/write path — the
common-dimensional-model equivalent of app/api/models.py, minus anything
measure-specific (bundles have no measures) and minus the in-app-editor-only
pieces out of scope for v1 (create/delete/live-validate — see
specs/006-common-dimensions/spec.md Assumptions)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import config, engine, semantic
from ..auth import require_role
from ..registry import registry
from .deps import get_bundle
from .models import DimensionSpec, SourceSpec

router = APIRouter(tags=["dimensions"])


class YamlIn(BaseModel):
    yaml: str


class BundleJoinSpec(BaseModel):
    to: str
    left_on: list[str] = []
    right_on: list[str] = []
    how: str = "left"


class BundleDatasetSpec(BaseModel):
    name: str
    source: SourceSpec
    dimensions: list[DimensionSpec] = []
    joins: list[BundleJoinSpec] = []


class BundleSpec(BaseModel):
    """Structured form of a common model — what the guided bundle form edits.
    POST /dimensions/generate renders it to YAML; GET /dimensions/{name}/spec
    is the inverse for opening an existing file in the form."""
    name: str
    label: str = ""
    description: str = ""
    datasets: list[BundleDatasetSpec] = []


def _to_public(bundle: semantic.DimensionBundle) -> dict:
    return {
        "name": bundle.name,
        "label": bundle.label,
        "description": bundle.description,
        "file": bundle.origin.name if bundle.origin else None,
        "datasets": [
            {"name": ds.name, "path": ds.source.path, "format": ds.source.format,
             "dimensions": [d.name for d in ds.dimensions.values()], "joins": [j.to for j in ds.joins]}
            for ds in bundle.datasets.values()
        ],
    }


def _importing_models(bundle_name: str) -> list[str]:
    """Names of loaded models whose imports reference this bundle."""
    return [m.name for m in registry.models.values()
            if any(b.bundle.name == bundle_name for b in m.import_bindings)]


def _reload_or_400() -> None:
    try:
        registry.reload_all()
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _bundle_datasets_out(parsed: semantic.DimensionBundle) -> list[dict]:
    """Per-dataset summary with schema introspection, shared by /validate and
    /generate — each renders/parses differently but reports the same shape."""
    datasets = []
    for ds in parsed.datasets.values():
        entry = {"name": ds.name, "dimensions": len(ds.dimensions),
                 "joins": [j.to for j in ds.joins]}
        try:
            schema = engine.scan_source(ds.source).collect_schema()
            entry["columns"] = [{"name": n, "dtype": str(t)} for n, t in schema.items()]
        except Exception as exc:
            entry["columns"] = None
            entry["schema_error"] = f"source not reachable: {exc}"
        datasets.append(entry)
    return datasets


@router.get("/dimensions")
def list_dimension_bundles():
    return [_to_public(b) for b in registry.dimension_bundles.values()]


@router.post("/dimensions/reload", dependencies=[Depends(require_role("admin"))])
def reload_dimension_bundles():
    _reload_or_400()
    return {"loaded": list(registry.dimension_bundles)}


@router.post("/dimensions/validate")
def validate_dimension_bundle(body: YamlIn):
    """Parse-check editor YAML for a bundle; if it parses, introspect each
    dataset's own source columns so the editor can show what's available to
    write dimensions/join keys against (mirrors /api/models/validate)."""
    try:
        parsed = semantic.parse_bundle_text(body.yaml)
    except semantic.ModelError as exc:
        return {"ok": False, "error": str(exc)}
    datasets = _bundle_datasets_out(parsed)
    return {"ok": True, "error": None,
            "bundle": {"name": parsed.name, "label": parsed.label, "datasets": datasets}}


@router.post("/dimensions/generate", dependencies=[Depends(require_role("author"))])
def generate_bundle_yaml(spec: BundleSpec):
    """Render the guided bundle form's structured spec to canonical YAML, then
    run the same parse + per-dataset schema introspection as
    /dimensions/validate so the form gets document and verdict in one call."""
    text = semantic.bundle_spec_to_yaml(spec.model_dump())
    try:
        parsed = semantic.parse_bundle_text(text)
    except semantic.ModelError as exc:
        return {"ok": False, "error": str(exc), "yaml": text}
    datasets = _bundle_datasets_out(parsed)
    return {"ok": True, "error": None, "yaml": text,
            "bundle": {"name": parsed.name, "label": parsed.label, "datasets": datasets}}


@router.get("/dimensions/{name}/spec")
def get_bundle_spec(name: str):
    """The bundle's yaml re-parsed into the structured spec the guided form
    edits (mirrors GET /models/{name}/spec)."""
    bundle = get_bundle(name)
    try:
        parsed = semantic.parse_bundle_text(bundle.origin.read_text())
    except semantic.ModelError as exc:  # file edited into a bad state on disk
        raise HTTPException(status_code=400, detail=str(exc))
    return {"name": name, "file": bundle.origin.name, "spec": semantic.bundle_to_spec(parsed)}


@router.post("/dimensions", status_code=201,
             dependencies=[Depends(require_role("admin"))])
def create_dimension_bundle(body: YamlIn):
    try:
        parsed = semantic.parse_bundle_text(body.yaml)
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    path = config.DIMENSIONS_DIR / f"{parsed.name}.yaml"
    if parsed.name in registry.dimension_bundles or path.exists():
        raise HTTPException(status_code=409, detail=f"dimension bundle '{parsed.name}' already exists")
    config.DIMENSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(body.yaml)
    _reload_or_400()
    return _to_public(registry.dimension_bundles[parsed.name])


@router.delete("/dimensions/{name}", status_code=204,
               dependencies=[Depends(require_role("admin"))])
def delete_dimension_bundle(name: str):
    bundle = get_bundle(name)
    importers = _importing_models(name)
    if importers:
        raise HTTPException(
            status_code=409,
            detail=f"cannot delete '{name}': imported by model(s) {', '.join(importers)}. "
                   f"Remove the import(s) first.",
        )
    bundle.origin.unlink()
    _reload_or_400()


@router.get("/dimensions/{name}/yaml")
def get_dimension_bundle_yaml(name: str):
    bundle = get_bundle(name)
    return {"name": name, "file": bundle.origin.name, "yaml": bundle.origin.read_text()}


@router.put("/dimensions/{name}/yaml", dependencies=[Depends(require_role("admin"))])
def put_dimension_bundle_yaml(name: str, body: YamlIn):
    bundle = get_bundle(name)
    try:
        parsed = semantic.parse_bundle_text(body.yaml)
    except semantic.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    other = registry.dimension_bundles.get(parsed.name)
    if other and other.origin != bundle.origin:
        raise HTTPException(
            status_code=409, detail=f"dimension bundle '{parsed.name}' already exists in {other.origin.name}"
        )
    bundle.origin.write_text(body.yaml)
    _reload_or_400()
    return _to_public(registry.dimension_bundles[parsed.name])
