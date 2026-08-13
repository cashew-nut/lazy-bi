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
from collections import Counter
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import cache, config, engine, s3, semantic
from ..auth import require_role
from ..registry import registry

router = APIRouter(tags=["datasets"])


def _invalidate_reads() -> None:
    """Drop cached object listings and source frames after this process
    changes what is in the bucket.

    app/engine.py holds small sources' *contents*, not just their schemas, so
    an upload that overwrites a path a model reads would otherwise keep
    serving the old rows until the TTL lapsed — and an upload is precisely
    the moment someone is standing there waiting to see the new ones."""
    cache.clear()


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_UPLOAD_FORMATS = {".csv": "csv", ".parquet": "parquet"}


def _safe_relpath(raw: str) -> str | None:
    """A file's relative path within the upload, sanitized — accepts the
    forward-slash-joined subpath a folder pick sends (see uploadRow() in
    formkit.js, which strips the picked folder's own top segment before
    sending), rejecting path traversal / absolute paths / any segment that
    isn't a plain name. None if unsafe."""
    parts = raw.replace("\\", "/").split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    if not all(_SAFE_SEGMENT.match(p) for p in parts):
        return None
    return "/".join(parts)


def _reldir(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


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
            {"name": m.name, "label": m.label,
             "datasets": [{"name": n, "path": ds.source.path, "format": ds.source.format}
                          for n, ds in m.datasets.items()],
             **per_model[m.name]}
            for m in registry.models.values()
        ],
    }


@router.get("/datasets/schema")
def dataset_schema(path: str, format: str = "parquet"):
    """Columns of an arbitrary source path — feeds the guided form's
    relationship pickers (join / import keys) before any model exists."""
    if format not in semantic.SOURCE_FORMATS:
        raise HTTPException(status_code=400, detail=f"unsupported source format '{format}'")
    try:
        schema = engine.source_schema(semantic.Source(path=path, format=format))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"source not reachable: {exc}")
    return {"columns": [{"name": n, "dtype": str(t)} for n, t in schema.items()]}


@router.post("/datasets/local", status_code=201, dependencies=[Depends(require_role("author"))])
async def upload_local_dataset(name: str = Form(...), files: list[UploadFile] = File(...)):
    """Upload one or more .csv/.parquet files into the bucket under
    local/<name>/ — unmodeled, exactly like a raw_data/ file, so they
    immediately show up in GET /datasets ready to build a model on from the
    Modelling workspace's source picker. Nothing about this touches the
    git-tracked codebase: the bytes land in the bucket (the emulator or a
    real external bucket), never in a committed directory.

    Each file's own name carries its path relative to the upload — a plain
    filename for an individually-picked file, or a subpath for one that came
    from a folder pick (uploadRow() in formkit.js strips the picked folder's
    own top segment first) — so a folder's structure survives as-is under
    local/<name>/. A file with an unrecognized extension or an unsafe
    (traversal/absolute) path is skipped, not fatal to the rest of the
    batch; 400 only if nothing in the batch was usable.

    Also cached under config.LOCAL_DATA_DIR (gitignored, local disk) so the
    upload survives a restart even against the embedded emulator, which is
    in-memory and reseeded from scratch on every start (app/seed.py's
    _upload_local_data re-uploads this cache then) — otherwise the files
    would quietly vanish the moment the process restarted."""
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="name must be alphanumeric (a-z, 0-9, _, -)")

    client = s3.client()
    uploaded: list[dict] = []
    skipped: list[str] = []
    for f in files:
        raw = f.filename or ""
        rel = _safe_relpath(raw)
        ext = PurePosixPath(rel).suffix.lower() if rel else ""
        if rel is None or ext not in _UPLOAD_FORMATS:
            skipped.append(raw or "(unnamed)")
            continue

        key = f"local/{name}/{rel}"
        body = await f.read()
        client.put_object(Bucket=config.BUCKET, Key=key, Body=body)

        cache_path = config.LOCAL_DATA_DIR / name / rel
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(body)

        fmt = _UPLOAD_FORMATS[ext]
        uploaded.append({"key": key, "path": f"s3://{config.BUCKET}/{key}", "format": fmt, "rel": rel})

    if not uploaded:
        raise HTTPException(status_code=400, detail="no .csv/.parquet files found in the upload")

    # before the source_schema() call below, so this upload's own introspection
    # reads the bytes just written rather than a cached predecessor's
    _invalidate_reads()

    # a representative path/format for the caller's "set this as my source"
    # convenience (app/static/js/modelform.js) — a single glob when every
    # uploaded file landed in the same directory (the common case: several
    # files for one dataset, or a single-level folder pick), else just the
    # first file's own exact path, since there's no one glob that covers a
    # multi-directory upload. GET /datasets reflects every group either way.
    dirs = {_reldir(u["rel"]) for u in uploaded}
    if len(dirs) == 1:
        (only_dir,) = dirs
        dominant_ext = Counter(PurePosixPath(u["rel"]).suffix.lower() for u in uploaded).most_common(1)[0][0]
        fmt = _UPLOAD_FORMATS[dominant_ext]
        prefix = f"local/{name}/{only_dir}/" if only_dir else f"local/{name}/"
        path = f"s3://{config.BUCKET}/{prefix}*{dominant_ext}"
    else:
        path, fmt = uploaded[0]["path"], uploaded[0]["format"]

    try:
        schema = engine.source_schema(semantic.Source(path=path, format=fmt))
        columns = [{"name": n, "dtype": str(t)} for n, t in schema.items()]
    except Exception:
        columns = None
    return {
        "path": path, "format": fmt, "columns": columns,
        "uploaded": [{"key": u["key"], "path": u["path"], "format": u["format"]} for u in uploaded],
        "skipped": skipped,
    }


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
    _invalidate_reads()
