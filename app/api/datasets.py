"""Dataset discovery: every object in every bucket this app browses, grouped
into pickable datasets (glob or delta-root sources), annotated with which
loaded models already read them, plus per-model file/byte totals and the
per-bucket count. One listing serves both the Modelling workspace's source
picker and its landing page (dataset tree + model stats) — the two used to hit
S3 separately.

"Every bucket" is normally one, and two when a real object store is configured
next to the demo one; each is walked through its own endpoint and credential,
and each walk is bounded, because a real bucket can hold more objects than any
page should try to page through.

Read-only. Reuses semantic.model_source_matchers (shared with app/api/explorer.py),
semantic.per_model_stats and semantic.group_objects for the grouping itself."""
from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import cache, config, duck, engine, s3, semantic
from ..auth import require_role
from ..registry import registry

router = APIRouter(tags=["datasets"])


def _invalidate_reads() -> None:
    """Drop cached object listings and source frames after this process
    changes what is in the bucket.

    app/duck.py pins small sources as local tables and DuckDB caches the bytes
    of everything else, so an upload that overwrites a path a model reads would
    otherwise keep serving the old rows until the TTL lapsed — and an upload is
    precisely the moment someone is standing there waiting to see the new
    ones."""
    cache.clear()
    duck.invalidate()


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
    """Every dataset the platform can browse, across every bucket it reads.

    Normally that is one bucket. With a real store configured next to the
    demo one it is both, each walked through its own endpoint and credential
    (app/s3.py's browsable_buckets) and merged into one tree — so the demo
    datasets and a real bucket's datasets sit side by side, each still
    readable, instead of the demo half resolving against an account that has
    never heard of it.

    Every walk is bounded (see s3.walk). `truncated` says a bucket held more
    than this app will page through; CI_BUCKET_PREFIX narrows the walk to the
    part of a shared bucket that is actually this deployment's."""
    datasets: list[dict] = []
    per_model: dict[str, dict] = {name: {"files": 0, "bytes": 0} for name in registry.models}
    stores, object_count, total_bytes, truncated = [], 0, 0, False
    unreachable: list[dict] = []

    for bucket, prefix in s3.browsable_buckets():
        try:
            objects, cut = s3.walk(bucket, prefix)
        except Exception as exc:
            # One unreachable bucket must not blank the whole picker: report
            # it and keep whatever the other one holds. A wrong credential or
            # a bucket in another account is exactly the case someone needs
            # the rest of this page to diagnose.
            unreachable.append({"bucket": bucket, "error": str(exc)})
            continue
        matchers = semantic.model_source_matchers(registry.models.values(), bucket)
        found = semantic.group_objects(objects, bucket)
        for ds in found:
            seen: set[tuple[str, str]] = set()
            readers = []
            for o in ds["objects"]:
                for name, role, match in matchers:
                    if match(o["key"]) and (name, role) not in seen:
                        seen.add((name, role))
                        readers.append({"name": name, "role": role})
            ds["models"] = readers
        datasets += found
        for name, stat in semantic.per_model_stats(objects, matchers, registry.models).items():
            per_model[name]["files"] += stat["files"]
            per_model[name]["bytes"] += stat["bytes"]
        size = sum(o["size"] for o in objects)
        store = config.store_for(bucket)
        stores.append({
            "bucket": bucket, "prefix": prefix,
            "endpoint": store.label,
            "demo": store.demo, "object_count": len(objects), "bytes": size,
            "truncated": cut,
        })
        object_count, total_bytes, truncated = object_count + len(objects), total_bytes + size, truncated or cut

    datasets.sort(key=lambda d: (d["bucket"] != config.BUCKET, d["key"]))
    primary = config.primary_store()
    return {
        "bucket": config.BUCKET,
        "endpoint": primary.label,
        "prefix": config.BUCKET_PREFIX,
        "object_count": object_count,
        "bytes": total_bytes,
        "truncated": truncated,
        "stores": stores,
        "unreachable": unreachable,
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

    client = s3.client(config.BUCKET)
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

        # Written whatever the bucket is: the disk copy costs little and is
        # what lets a deployment move back to the emulator without losing
        # uploads. What is *conditional* is putting it back on start —
        # app/seed.py's restore_local_uploads only does that for an ephemeral
        # store, so a real bucket is never re-uploaded into on every restart.
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
    client = s3.client(config.BUCKET)
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
