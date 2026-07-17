"""Pipeline endpoints: hosted polars transformation scripts (specs/014-
polars-pipeline-module/). A pipeline script is real Python at
application-code trust (Principle VI, re-opened for this feature) — every
mutation and every run trigger requires the admin role, exactly like a
model's `frame:` carve-out; reads (list, yaml, runs) are open to any
authenticated role, same as the rest of the semantic layer.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import config, pipeline_jobs, pipelines as pipelines_mod
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
    pipeline.origin.unlink()
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
