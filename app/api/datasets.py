"""Dataset discovery for the Modelling workspace's source picker: every object
in the bucket grouped into pickable datasets (glob or delta-root sources),
annotated with which loaded models already read them.

Read-only. Reuses the same object walk and model-source matcher shape as
app/api/explorer.py; the grouping itself lives in semantic.group_objects."""
from __future__ import annotations

import fnmatch

from fastapi import APIRouter

from .. import config, s3, semantic
from ..registry import registry

router = APIRouter(tags=["datasets"])


def _model_matchers():
    """(model_name, role, match_fn) over each model's source/join/import globs —
    the explorer's matcher shape, reused so datasets can be tagged with readers."""
    prefix = f"s3://{config.BUCKET}/"
    matchers = []
    for m in registry.models.values():
        sources = (
            [("source", m.source)]
            + [(f"join: {j.name}", j.source) for j in m.joins]
            + [(f"import: {b.bundle.name}.{ds}", b.bundle.datasets[ds].source)
               for b in m.import_bindings for ds in b.included_datasets]
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
    return matchers


@router.get("/datasets")
def list_datasets():
    client = s3.client()
    objects = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.BUCKET):
        for obj in page.get("Contents", []):
            objects.append({"key": obj["Key"], "size": obj["Size"]})

    datasets = semantic.group_objects(objects, config.BUCKET)

    matchers = _model_matchers()
    for ds in datasets:
        seen: set[tuple[str, str]] = set()
        readers = []
        for o in ds["objects"]:
            for name, role, match in matchers:
                if match(o["key"]) and (name, role) not in seen:
                    seen.add((name, role))
                    readers.append({"name": name, "role": role})
        ds["models"] = readers

    return {"bucket": config.BUCKET, "endpoint": config.S3_ENDPOINT, "datasets": datasets}
