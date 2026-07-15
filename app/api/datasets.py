"""Dataset discovery for the Modelling workspace's source picker: every object
in the bucket grouped into pickable datasets (glob or delta-root sources),
annotated with which loaded models already read them.

Read-only. Reuses semantic.model_source_matchers (shared with app/api/explorer.py)
and semantic.group_objects for the grouping itself."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import config, engine, s3, semantic
from ..registry import registry

router = APIRouter(tags=["datasets"])


@router.get("/datasets")
def list_datasets():
    client = s3.client()
    objects = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.BUCKET):
        for obj in page.get("Contents", []):
            objects.append({"key": obj["Key"], "size": obj["Size"]})

    datasets = semantic.group_objects(objects, config.BUCKET)

    matchers = semantic.model_source_matchers(registry.models.values(), config.BUCKET)
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


@router.get("/datasets/schema")
def dataset_schema(path: str, format: str = "parquet"):
    """Columns of an arbitrary source path — feeds the guided form's
    relationship pickers (join / import keys) before any model exists."""
    if format not in semantic.SOURCE_FORMATS:
        raise HTTPException(status_code=400, detail=f"unsupported source format '{format}'")
    try:
        schema = engine.scan_source(semantic.Source(path=path, format=format)).collect_schema()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"source not reachable: {exc}")
    return {"columns": [{"name": n, "dtype": str(t)} for n, t in schema.items()]}
