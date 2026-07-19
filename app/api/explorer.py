"""Data explorer (bucket objects mapped to models) and health."""
from __future__ import annotations

from fastapi import APIRouter

from .. import config, s3, semantic
from ..registry import registry

router = APIRouter(tags=["explorer"])


@router.get("/explorer")
def explorer():
    """Every object in the bucket, matched against each model's source and
    join globs so the UI can show which files feed which models."""
    client = s3.client()
    matchers = semantic.model_source_matchers(registry.models.values(), config.BUCKET)

    objects = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.BUCKET):
        objects.extend(page.get("Contents", []))

    per_model = semantic.per_model_stats(
        [{"key": o["Key"], "size": o["Size"]} for o in objects], matchers, registry.models,
    )
    files = [{
        "key": o["Key"],
        "size": o["Size"],
        "modified": o["LastModified"].isoformat(timespec="seconds"),
        "models": [{"model": name, "role": role} for name, role, match in matchers if match(o["Key"])],
    } for o in objects]
    return {
        "bucket": config.BUCKET,
        "endpoint": config.S3_ENDPOINT,
        "files": files,
        "models": [
            {"name": m.name, "label": m.label, "format": m.source.format, "path": m.source.path,
             "joins": [{"name": j.name, "path": j.source.path, "format": j.source.format} for j in m.joins],
             **per_model[m.name]}
            for m in registry.models.values()
        ],
    }


@router.get("/health")
def health():
    return {
        "ok": True, "models": list(registry.models), "s3_endpoint": config.S3_ENDPOINT,
        "llm_enabled": config.LLM_ENABLED,
        "llm_models": config.LLM_MODEL_CHOICES if config.LLM_ENABLED else [],
        "llm_default_model": config.LLM_MODEL,
    }
