"""Pipeline endpoints: hosted polars transformation scripts (specs/014-
polars-pipeline-module/). A pipeline script is real Python at
application-code trust (Principle VI, re-opened for this feature) — every
mutation and every run trigger requires the admin role, exactly like a
model's `frame:` carve-out; reads (list, yaml, runs) are open to any
authenticated role, same as the rest of the semantic layer.
"""
from __future__ import annotations

import fnmatch
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import polars as pl

from .. import config, iceberg_util, pipeline_jobs, semantic
from .. import pipelines as pipelines_mod
from ..auth import User, require_role
from ..registry import registry
from .deps import get_pipeline

router = APIRouter(tags=["pipelines"])


class PipelineYamlIn(BaseModel):
    yaml: str


def _reload_or_400() -> None:
    try:
        registry.reload_all()
    except (pipelines_mod.PipelineError,) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # semantic.ModelError etc. — a pipeline reload also reloads models
        raise HTTPException(status_code=400, detail=str(exc))


def _parse_or_400(text: str) -> pipelines_mod.Pipeline:
    try:
        return pipelines_mod.parse_pipeline_text(text)
    except pipelines_mod.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _list_entry(pipeline: pipelines_mod.Pipeline) -> dict:
    latest = registry.pipeline_store.latest_for(pipeline.name)
    out = pipeline.to_public()
    out["latest_run"] = (
        {"id": latest["id"], "status": latest["status"], "finished_at": latest["finished_at"]}
        if latest else None
    )
    return out


@router.get("/pipelines")
def list_pipelines():
    return [_list_entry(p) for p in registry.pipelines.values()]


@router.post("/pipelines/validate")
def validate_pipeline(body: PipelineYamlIn):
    """Parse-check editor YAML — never executes the script, only syntax-
    checks it (pipelines_mod.validate_script)."""
    try:
        parsed = pipelines_mod.parse_pipeline_text(body.yaml)
    except pipelines_mod.PipelineError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "error": None, "pipeline": parsed.to_public()}


@router.post("/pipelines/reload", dependencies=[Depends(require_role("admin"))])
def reload_pipelines():
    _reload_or_400()
    return {"loaded": list(registry.pipelines)}


@router.post("/pipelines", status_code=201)
def create_pipeline(body: PipelineYamlIn, user: User = Depends(require_role("admin"))):
    parsed = _parse_or_400(body.yaml)
    path = config.PIPELINES_DIR / f"{parsed.name}.yaml"
    if parsed.name in registry.pipelines or path.exists():
        raise HTTPException(status_code=409, detail=f"pipeline '{parsed.name}' already exists")
    for existing in registry.pipelines.values():
        if existing.target.path == parsed.target.path:
            raise HTTPException(
                status_code=409,
                detail=f"target '{parsed.target.path}' is already owned by pipeline '{existing.name}'",
            )
    config.PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(body.yaml)
    _reload_or_400()
    registry.auth_store.record_audit(
        "pipeline.create", user.display_name, actor_user_id=user.id, target=parsed.name
    )
    return _list_entry(registry.pipelines[parsed.name])


@router.get("/pipelines/{name}/yaml")
def get_pipeline_yaml(name: str):
    pipeline = get_pipeline(name)
    return {"name": name, "file": pipeline.origin.name, "yaml": pipeline.origin.read_text()}


@router.put("/pipelines/{name}/yaml")
def put_pipeline_yaml(name: str, body: PipelineYamlIn, user: User = Depends(require_role("admin"))):
    pipeline = get_pipeline(name)
    parsed = _parse_or_400(body.yaml)
    if parsed.name != name:
        raise HTTPException(
            status_code=400,
            detail=f"pipeline name is immutable — cannot rename '{name}' to '{parsed.name}'",
        )
    for existing in registry.pipelines.values():
        if existing.name != name and existing.target.path == parsed.target.path:
            raise HTTPException(
                status_code=409,
                detail=f"target '{parsed.target.path}' is already owned by pipeline '{existing.name}'",
            )
    pipeline.origin.write_text(body.yaml)
    _reload_or_400()
    registry.auth_store.record_audit(
        "pipeline.update", user.display_name, actor_user_id=user.id, target=name
    )
    return _list_entry(registry.pipelines[name])


@router.delete("/pipelines/{name}", status_code=204)
def delete_pipeline(name: str, user: User = Depends(require_role("admin"))):
    pipeline = get_pipeline(name)
    if registry.pipeline_store.pending_for(name):
        raise HTTPException(
            status_code=409, detail=f"pipeline '{name}' has a run queued or running — wait for it to finish"
        )
    # capture the matched model before this pipeline's file (and registry
    # entry) disappear — orphan-marking is a write triggered by this DELETE
    # itself, never deferred to a reload side effect
    model_name = pipelines_mod.match_target_model(pipeline, registry.models)
    pipeline.origin.unlink()
    _reload_or_400()
    if model_name and model_name in registry.models:
        model = registry.models[model_name]
        if model.pipeline_lineage:
            section = {
                "pipeline": model.pipeline_lineage.pipeline,
                "updated": model.pipeline_lineage.updated,
                "orphaned": True,
                "fields": [
                    {"field": f.field, "sources": f.sources, "transform": f.transform,
                     **({"stale": True} if f.stale else {})}
                    for f in model.pipeline_lineage.fields
                ],
            }
            model.origin.write_text(semantic.replace_lineage_yaml(model.origin.read_text(), section))
            _reload_or_400()
    registry.auth_store.record_audit("pipeline.delete", user.display_name, actor_user_id=user.id, target=name)


@router.post("/pipelines/{name}/run", status_code=202)
def run_pipeline(name: str, user: User = Depends(require_role("admin"))):
    pipeline = get_pipeline(name)
    if registry.pipeline_store.pending_for(name):
        raise HTTPException(
            status_code=409, detail=f"pipeline '{name}' already has a run queued or running"
        )
    run = registry.pipeline_store.create_run(name, user.id, user.display_name)
    pipeline_jobs.enqueue(run["id"])
    registry.auth_store.record_audit("pipeline.run", user.display_name, actor_user_id=user.id, target=name)
    return {"run_id": run["id"], "status": run["status"]}


@router.get("/pipelines/{name}/runs")
def list_runs(name: str, limit: int = 50):
    get_pipeline(name)
    return registry.pipeline_store.runs_for(name, limit=limit)


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    run = registry.pipeline_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
    return run


# ── layers (optional, deployment-wide, ordered) ──────────────────────────

class LayersIn(BaseModel):
    layers: list[dict]  # [{name, label?}], order is meaningful


@router.get("/lineage/layers")
def get_layers():
    return {"layers": [{"name": l.name, "label": l.label} for l in registry.layers.values()]}


@router.put("/lineage/layers")
def put_layers(body: LayersIn, user: User = Depends(require_role("admin"))):
    names = [entry.get("name") for entry in body.layers]
    if not all(names):
        raise HTTPException(status_code=400, detail="each layer needs a 'name'")
    removed = set(registry.layers) - set(names)
    if removed:
        referencing = sorted({
            p.name for p in registry.pipelines.values()
            if removed & ({s.layer for s in p.sources.values() if s.layer}
                          | ({p.target.layer} if p.target.layer else set()))
        })
        if referencing:
            raise HTTPException(
                status_code=409,
                detail=f"layer(s) {sorted(removed)} still referenced by pipeline(s) {referencing}",
            )
    layers = {
        entry["name"]: pipelines_mod.Layer(
            name=entry["name"], label=entry.get("label", entry["name"].replace("_", " ").title())
        )
        for entry in body.layers
    }
    config.PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    (config.PIPELINES_DIR / "layers.yaml").write_text(pipelines_mod.layers_to_yaml(layers))
    _reload_or_400()
    registry.auth_store.record_audit("layers.update", user.display_name, actor_user_id=user.id)
    return {"layers": [{"name": l.name, "label": l.label} for l in registry.layers.values()]}


# ── lineage pass-through suggestion (never auto-persisted — FR-017) ──────

def _scan_source_schema(fmt: str, path: str):
    opts = config.storage_options()
    if fmt == "csv":
        return pl.scan_csv(path, storage_options=opts).collect_schema()
    if fmt == "delta":
        return pl.scan_delta(path, storage_options=opts).collect_schema()
    if fmt == "iceberg":
        return iceberg_util.scan(path).collect_schema()
    return pl.scan_parquet(path, storage_options=opts).collect_schema()


@router.get("/pipelines/{name}/lineage/suggest")
def suggest_lineage(name: str):
    pipeline = get_pipeline(name)
    output_schema = None
    try:
        schema = _scan_source_schema(pipeline.target.format, pipeline.target.path)
        output_schema = [{"name": n, "dtype": str(t)} for n, t in schema.items()]
    except Exception:
        output_schema = None
    if output_schema is None:
        output_schema = registry.pipeline_store.latest_successful_schema(name)
    if output_schema is None:
        raise HTTPException(
            status_code=409,
            detail="no output schema available yet — run the pipeline at least once, "
                   "or wait for its target to exist",
        )

    declared_fields = {e.field for e in pipeline.lineage}
    suggestions = []
    for f in output_schema:
        if f["name"] in declared_fields:
            continue
        for src_name, src in pipeline.sources.items():
            try:
                src_schema = _scan_source_schema(src.format, src.path)
            except Exception:
                continue
            if f["name"] in src_schema:
                suggestions.append({
                    "field": f["name"], "from": [f"{src_name}.{f['name']}"], "transform": "pass-through",
                })
                break
    return {"suggestions": suggestions}


# ── lineage graph (US4) — read-only, assembled from loaded pipelines/models/
# layers + latest runs; no live bucket scan, so it stays fast and never hangs
# on a cycle (edges are built per-pipeline, never traversed recursively). ──

def _find_model_for(path: str, fmt: str) -> Optional[str]:
    for name, model in registry.models.items():
        src = model.source
        if fmt == "delta":
            if src.format == "delta" and src.path.rstrip("/") == path.rstrip("/"):
                return name
        elif fmt == "parquet" and src.format == "parquet" and fnmatch.fnmatch(path, src.path):
            return name
        elif fmt == "csv" and src.format == "csv" and src.path == path:
            return name
    return None


@router.get("/lineage/graph")
def lineage_graph():
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    field_lineage: list[dict] = []

    def get_or_create_node(path: str, fmt: str, layer: Optional[str]) -> dict:
        if path in nodes:
            if layer and not nodes[path]["layer"]:
                nodes[path]["layer"] = layer
            return nodes[path]
        node = {
            "id": path, "label": path.rstrip("/").rsplit("/", 1)[-1] or path,
            "layer": layer, "model": _find_model_for(path, fmt), "fields": [],
        }
        nodes[path] = node
        return node

    for p in registry.pipelines.values():
        target_node = get_or_create_node(p.target.path, p.target.format, p.target.layer)
        latest = registry.pipeline_store.latest_for(p.name)
        status = latest["status"] if latest else None
        transform_summary = (
            p.description if p.description
            else f"{len(p.lineage)} field(s) documented" if p.lineage
            else "No transformation documented"
        )
        for s in p.sources.values():
            source_node = get_or_create_node(s.path, s.format, s.layer)
            edges.append({
                "pipeline": p.name, "source_id": source_node["id"], "target_id": target_node["id"],
                "status": status, "transform_summary": transform_summary,
            })
        for entry in p.lineage:
            upstream = []
            for ref in entry.sources:
                src_name, _, col = ref.partition(".")
                src = p.sources.get(src_name)
                if src:
                    upstream.append({"node_id": src.path, "field": col})
            if upstream:
                field_lineage.append({
                    "node_id": target_node["id"], "field": entry.field,
                    "upstream": upstream, "transform": entry.transform,
                })

    # backfill each node's known fields from whatever field lineage touches it
    for entry in field_lineage:
        fields = nodes[entry["node_id"]]["fields"]
        if entry["field"] not in fields:
            fields.append(entry["field"])
        for up in entry["upstream"]:
            up_fields = nodes.get(up["node_id"], {}).get("fields")
            if up_fields is not None and up["field"] not in up_fields:
                up_fields.append(up["field"])

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "field_lineage": field_lineage,
        "layers": [{"name": l.name, "label": l.label} for l in registry.layers.values()],
    }
