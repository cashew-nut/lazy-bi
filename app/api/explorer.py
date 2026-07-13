"""Data explorer (bucket objects mapped to models) and health."""
from __future__ import annotations

import fnmatch

from fastapi import APIRouter

from .. import config, s3
from ..registry import registry

router = APIRouter(tags=["explorer"])


@router.get("/explorer")
def explorer():
    """Every object in the bucket, matched against each model's source and
    join globs so the UI can show which files feed which models."""
    client = s3.client()
    prefix = f"s3://{config.BUCKET}/"
    matchers = []  # (model_name, role, match_fn)
    for m in registry.models.values():
        sources = (
            [("source", m.source)]
            + [(f"join: {j.name}", j.source) for j in m.joins]
            + [(f"import: {binding.bundle.name}.{ds}", binding.bundle.datasets[ds].source)
               for binding in m.import_bindings for ds in binding.included_datasets]
        )
        for role, src in sources:
            if not src.path.startswith(prefix):
                continue
            rel = src.path[len(prefix):]
            if src.format == "delta":
                root = rel.rstrip("/") + "/"
                matchers.append((m.name, role, lambda k, r=root: k.startswith(r)))
            else:
                matchers.append((m.name, role, lambda k, p=rel: fnmatch.fnmatch(k, p)))

    files = []
    per_model = {name: {"files": 0, "bytes": 0} for name in registry.models}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.BUCKET):
        for obj in page.get("Contents", []):
            hits = [{"model": name, "role": role} for name, role, match in matchers if match(obj["Key"])]
            for h in {h["model"] for h in hits}:
                per_model[h]["files"] += 1
                per_model[h]["bytes"] += obj["Size"]
            files.append({
                "key": obj["Key"],
                "size": obj["Size"],
                "modified": obj["LastModified"].isoformat(timespec="seconds"),
                "models": hits,
            })
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
