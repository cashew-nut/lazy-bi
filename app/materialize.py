"""Pipeline materialization: the platform's own write step (specs/014-polars-
pipeline-module/). A pipeline script never writes to the bucket itself — it
only produces an `output` frame (see app/pipeline_runner.py); this module
performs the actual `replace`/`upsert` write, so materialization semantics
(atomic replace, keyed merge, delete-policy handling) are enforced
uniformly regardless of what the script does. Runs entirely inside the
runner subprocess.
"""
from __future__ import annotations

import io
from typing import Optional

import polars as pl
from deltalake import DeltaTable, write_deltalake
from deltalake.exceptions import TableNotFoundError

from . import s3
from .pipelines import Materialization, Target


class MaterializeError(Exception):
    """A guard failure or write error. Every guard in this module runs
    before any write happens, so raising here always leaves the target
    exactly as it was before the run (SC-003) — the caller (pipeline_runner)
    reports this as a failed run, never a partial one."""


def materialize(output, target: Target, materialization: Materialization,
                 storage_options: dict) -> dict:
    """Collect `output` (a LazyFrame or DataFrame) and write it to `target`
    per `materialization`. Returns {"rows_written", "rows_deleted",
    "rows_flagged"}."""
    df = output.collect() if isinstance(output, pl.LazyFrame) else output
    if not isinstance(df, pl.DataFrame):
        raise MaterializeError(
            f"script's 'output' must be a polars LazyFrame or DataFrame, got {type(df).__name__}"
        )
    if materialization.mode == "replace":
        return _replace(df, target, storage_options)
    return _upsert(df, target, materialization, storage_options)


def _split_s3_path(path: str) -> tuple[str, str]:
    if not path.startswith("s3://"):
        raise MaterializeError(f"target path must be an s3:// url, got '{path}'")
    rest = path[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not key:
        raise MaterializeError(f"target path '{path}' has no object key")
    return bucket, key


def _replace(df: pl.DataFrame, target: Target, storage_options: dict) -> dict:
    if target.format == "delta":
        # a single transaction: readers see the old table or the new one,
        # never a partial write (Constitution: failed runs never corrupt).
        write_deltalake(target.path, df, mode="overwrite", schema_mode="overwrite",
                         storage_options=storage_options)
    else:  # parquet — a single-object PUT is atomic on S3 (pattern: seed.py)
        buf = io.BytesIO()
        df.write_parquet(buf)
        bucket, key = _split_s3_path(target.path)
        s3.client().put_object(Bucket=bucket, Key=key, Body=buf.getvalue())
    return {"rows_written": df.height, "rows_deleted": 0, "rows_flagged": 0}


def _open_target(target: Target, storage_options: dict) -> Optional[DeltaTable]:
    try:
        return DeltaTable(target.path, storage_options=storage_options)
    except TableNotFoundError:
        return None


def _guard_keys(df: pl.DataFrame, keys: list[str]) -> None:
    missing = [k for k in keys if k not in df.columns]
    if missing:
        raise MaterializeError(f"upsert key column(s) {missing} missing from script output")
    null_mask = pl.any_horizontal([pl.col(k).is_null() for k in keys])
    if df.filter(null_mask).height > 0:
        raise MaterializeError(f"upsert output has null value(s) in key column(s) {keys}")
    dup_count = df.height - df.select(keys).unique().height
    if dup_count > 0:
        raise MaterializeError(f"upsert output has {dup_count} duplicate key value(s) in {keys}")


def _guard_schema(df: pl.DataFrame, existing_schema: dict, soft_delete_column: Optional[str]) -> None:
    """Compare the script's raw output schema against the existing target's
    schema — excluding the soft-delete flag column, which is
    platform-managed and never expected in a script's own output."""
    expected = dict(existing_schema)
    if soft_delete_column:
        expected.pop(soft_delete_column, None)
    actual = {name: str(dtype) for name, dtype in df.schema.items()}
    expected = {name: str(dtype) for name, dtype in expected.items()}
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        mismatched = sorted(
            c for c in set(expected) & set(actual) if expected[c] != actual[c]
        )
        raise MaterializeError(
            "upsert output schema incompatible with existing target: "
            f"missing={missing} extra={extra} type_mismatch={mismatched}"
        )


def _upsert(df: pl.DataFrame, target: Target, materialization: Materialization,
            storage_options: dict) -> dict:
    keys = materialization.keys
    _guard_keys(df, keys)

    dt = _open_target(target, storage_options)
    if dt is None:
        # first run against a target that doesn't exist yet: an initial
        # write, equivalent to replace for this one run (research U4) —
        # nothing to guard against and nothing to delete/flag. The flag
        # column still needs to exist on the table from this point on, so
        # later runs' schema guard (and when_matched_update_all) see it.
        if materialization.on_delete == "soft_delete":
            df = df.with_columns(pl.lit(False).alias(materialization.soft_delete_column))
        write_deltalake(target.path, df, mode="overwrite", storage_options=storage_options)
        return {"rows_written": df.height, "rows_deleted": 0, "rows_flagged": 0}

    # schema guard runs against the script's own output — before the
    # platform-managed soft-delete column (never part of that output) gets
    # added below, so the comparison isn't fooled by its own injected column.
    existing_schema = dict(pl.scan_delta(target.path, storage_options=storage_options).collect_schema())
    _guard_schema(df, existing_schema, materialization.soft_delete_column)

    if materialization.on_delete == "soft_delete" and materialization.soft_delete_column not in existing_schema:
        # The flag column must already exist on the target — it's only ever
        # added automatically on a *first* upsert run (above). Retrofitting
        # soft_delete onto an existing target (created by `replace`, or by an
        # earlier upsert with a different on_delete) needs an explicit
        # schema migration outside this run: deltalake's own merge-time
        # schema evolution mis-populates when_not_matched_by_source_update
        # for a brand new column (verified: it leaves those rows null
        # instead of true), so silently "fixing it up" here would produce
        # wrong flags rather than a clear error.
        raise MaterializeError(
            f"upsert target is missing the soft-delete column "
            f"'{materialization.soft_delete_column}' — replace the target once with "
            f"that column present (e.g. via a one-off replace run) before switching "
            f"this pipeline to on_delete: soft_delete"
        )

    # the soft-delete flag is platform-managed: the script never produces
    # it, so it is driven explicitly to False on every row this run touches
    # (research U2) — `when_matched_update_all` then clears it on any key
    # that reappears after having been flagged.
    if materialization.on_delete == "soft_delete":
        df = df.with_columns(pl.lit(False).alias(materialization.soft_delete_column))

    if df.height == 0 and materialization.on_delete == "sync" and not materialization.allow_empty_sync:
        raise MaterializeError(
            "upsert output is empty and on_delete is 'sync' — this would delete every row in the "
            "target; set materialization.allow_empty_sync: true if that is really intended"
        )

    rows_deleted = 0
    rows_flagged = 0
    if materialization.on_delete == "predicate":
        result = dt.delete(materialization.delete_predicate)
        rows_deleted = result.get("num_deleted_rows", 0) or 0

    if materialization.on_delete == "soft_delete" and keys:
        # "rows flagged this run" = target rows not present in this run's
        # output by key — whether newly flagged or re-affirmed, since the
        # not-matched-by-source update touches all of them every run.
        existing_lf = pl.scan_delta(target.path, storage_options=storage_options)
        rows_flagged = (
            existing_lf.join(df.lazy(), on=keys, how="anti").select(pl.len()).collect().item()
        )

    merger = dt.merge(
        df, predicate=" AND ".join(f"target.{k} = source.{k}" for k in keys),
        source_alias="source", target_alias="target",
    ).when_matched_update_all().when_not_matched_insert_all()

    if materialization.on_delete == "sync":
        merger = merger.when_not_matched_by_source_delete()
    elif materialization.on_delete == "soft_delete":
        merger = merger.when_not_matched_by_source_update(
            {materialization.soft_delete_column: "true"}
        )

    stats = merger.execute()
    # num_target_rows_updated bundles matched-row updates together with any
    # when_not_matched_by_source_update (the soft-delete flag write) — since
    # rows_flagged above counts exactly that not-matched-by-source set,
    # subtracting it recovers the true "rows actually upserted" count.
    rows_written = (stats.get("num_target_rows_inserted", 0) or 0) + \
                   max(0, (stats.get("num_target_rows_updated", 0) or 0) - rows_flagged)
    if materialization.on_delete == "sync":
        rows_deleted = stats.get("num_target_rows_deleted", 0) or 0

    return {"rows_written": rows_written, "rows_deleted": rows_deleted, "rows_flagged": rows_flagged}
