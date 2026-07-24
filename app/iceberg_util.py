"""Iceberg table reads: no catalog involved. A model's `source.path` for an
iceberg source is the table's root directory (like a Delta table root); the
current snapshot is found by listing `<root>/metadata/` and taking the
highest-versioned `*.metadata.json` file — the same self-describing-directory
convention app/engine.py already relies on for Delta's `_delta_log`, just
walked by hand since Iceberg has no single well-known "latest" filename.

Iceberg is read-only in this app: creating or writing a table needs a
catalog to allocate a location/schema/snapshot atomically, which only
app/seed.py's demo data uses (a throwaway in-memory pyiceberg SqlCatalog,
discarded once the table is written) — nothing at request time depends on a
catalog existing.
"""
from __future__ import annotations

import re

import polars as pl

from . import config, s3

_METADATA_RE = re.compile(r"^(\d+)-.*\.metadata\.json$")


def _split_s3_path(path: str) -> tuple[str, str]:
    if not path.startswith("s3://"):
        raise ValueError(f"iceberg source path must be an s3:// url, got '{path}'")
    rest = path[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key.rstrip("/")


def resolve_metadata_path(path: str) -> str:
    """`path` is an iceberg table's root directory; return the s3:// path of
    its current (highest-versioned) metadata.json."""
    bucket, root = _split_s3_path(path)
    prefix = f"{root}/metadata/"
    client = s3.client()
    paginator = client.get_paginator("list_objects_v2")
    best_version, best_key = -1, None
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            m = _METADATA_RE.match(name)
            if m and int(m.group(1)) > best_version:
                best_version, best_key = int(m.group(1)), obj["Key"]
    if best_key is None:
        raise ValueError(f"no iceberg metadata.json found under '{path}/metadata/'")
    return f"s3://{bucket}/{best_key}"


def scan(path: str) -> pl.LazyFrame:
    """Lazily scan an iceberg table given its root directory."""
    metadata_path = resolve_metadata_path(path)
    return pl.scan_iceberg(metadata_path, storage_options=config.iceberg_storage_options())
