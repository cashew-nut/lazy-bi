"""Dataset discovery: every object in the bucket grouped into pickable datasets
(glob or delta-root sources), annotated with which loaded models already read
them, plus per-model file/byte totals and the bucket-wide count. One listing
serves both the Modelling workspace's source picker and its landing page
(dataset tree + model stats) — the two used to hit S3 separately.

Read-only. Reuses semantic.model_source_matchers (shared with app/api/explorer.py),
semantic.per_model_stats and semantic.group_objects for the grouping itself."""
from __future__ import annotations

import re
import shutil
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import config, engine, s3, semantic
from ..auth import require_role
from ..registry import registry

router = APIRouter(tags=["datasets"])

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_UPLOAD_FORMATS = {".csv": "csv", ".parquet": "parquet"}


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

    per_model = semantic.per_model_stats(objects, matchers, registry.models)

    return {
        "bucket": config.BUCKET,
        "endpoint": config.S3_ENDPOINT,
        "object_count": len(objects),
        "bytes": sum(o["size"] for o in objects),
        "datasets": datasets,
        "models": [
            {"name": m.name, "label": m.label, "format": m.source.format, "path": m.source.path,
             "joins": [{"name": j.name, "path": j.source.path, "format": j.source.format} for j in m.joins],
             **per_model[m.name]}
            # a multi-fact model reads no objects of its own — its facts are
            # listed here in their own right
            for m in registry.models.values() if not m.is_composite
        ],
    }


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


@router.post("/datasets/local", status_code=201, dependencies=[Depends(require_role("author"))])
async def upload_local_dataset(name: str = Form(...), file: UploadFile = File(...)):
    """Upload a .csv/.parquet file into the bucket under local/<name>/ —
    unmodeled, exactly like a raw_data/ file, so it immediately shows up in
    GET /datasets ready to build a model on from the Modelling workspace's
    source picker. Nothing about this touches the git-tracked codebase: the
    bytes land in the bucket (the emulator or a real external bucket), never
    in a committed directory.

    Also cached under config.LOCAL_DATA_DIR (gitignored, local disk) so the
    upload survives a restart even against the embedded emulator, which is
    in-memory and reseeded from scratch on every start (app/seed.py's
    _upload_local_data re-uploads this cache then) — otherwise the file
    would quietly vanish the moment the process restarted."""
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="name must be alphanumeric (a-z, 0-9, _, -)")
    filename = PurePosixPath(file.filename or "").name
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in _UPLOAD_FORMATS:
        raise HTTPException(status_code=400, detail="only .csv and .parquet files are supported")
    if not _SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=400, detail="filename must be alphanumeric (a-z, 0-9, _, -, .)")

    key = f"local/{name}/{filename}"
    body = await file.read()
    s3.client().put_object(Bucket=config.BUCKET, Key=key, Body=body)

    cache_path = config.LOCAL_DATA_DIR / name / filename
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(body)

    fmt = _UPLOAD_FORMATS[ext]
    path = f"s3://{config.BUCKET}/{key}"
    try:
        schema = engine.scan_source(semantic.Source(path=path, format=fmt)).collect_schema()
        columns = [{"name": n, "dtype": str(t)} for n, t in schema.items()]
    except Exception:
        columns = None
    return {"path": path, "format": fmt, "key": key, "columns": columns}


@router.delete("/datasets/local/{name}", status_code=204, dependencies=[Depends(require_role("author"))])
def delete_local_dataset(name: str):
    """Remove every object uploaded under local/<name>/ (and its disk cache
    — see upload_local_dataset above) — the counterpart to
    upload_local_dataset above. 404s if the name doesn't exist so a typo
    isn't silently a no-op."""
    client = s3.client()
    prefix = f"local/{name}/"
    keys = [
        o["Key"]
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=config.BUCKET, Prefix=prefix)
        for o in page.get("Contents", [])
    ]
    if not keys:
        raise HTTPException(status_code=404, detail=f"no local dataset named '{name}'")
    client.delete_objects(Bucket=config.BUCKET, Delete={"Objects": [{"Key": k} for k in keys]})
    shutil.rmtree(config.LOCAL_DATA_DIR / name, ignore_errors=True)
