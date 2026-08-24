"""Pipeline materialization: the platform's own write step (specs/018-duckdb-
sql-engine/, replacing spec 014's polars shape). A pipeline never writes to the
bucket itself — it only produces an `output` relation (see
app/pipeline_runner.py); this module performs the actual `replace`/`upsert`
write, so materialization semantics (atomic replace, keyed merge, delete-policy
handling) are enforced uniformly regardless of what the pipeline's SQL does.
Runs entirely inside the runner subprocess.

The guards are SQL, over the run's own output registered as a relation — the
same language the pipeline that produced it is written in. Delta is still
written by `deltalake`: DuckDB reads Delta but does not write it.
"""
from __future__ import annotations

import io
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq
from deltalake import DeltaTable, write_deltalake
from deltalake.exceptions import TableNotFoundError

from . import duck, s3
from .pipelines import Materialization, Target

# the run's output, registered under this name for the guard queries below
OUTPUT_RELATION = "__output"


class MaterializeError(Exception):
    """A guard failure or write error. Every guard in this module runs before
    any write happens, so raising here always leaves the target exactly as it
    was before the run (SC-003) — the caller (pipeline_runner) reports this as
    a failed run, never a partial one."""


def as_table(output) -> pa.Table:
    """A pipeline's `output` as an Arrow table, whatever it arrived as: a
    DuckDB relation, an Arrow table, or a record batch reader."""
    if isinstance(output, pa.Table):
        return output
    for method in ("to_arrow_table", "fetch_arrow_table", "arrow"):
        fetch = getattr(output, method, None)
        if callable(fetch):
            result = fetch()
            return result if isinstance(result, pa.Table) else pa.Table.from_batches(
                list(result), schema=result.schema)
    raise MaterializeError(
        f"a pipeline's 'output' must be a relation, got {type(output).__name__}")


def materialize(output, target: Target, materialization: Materialization,
                storage_options: dict) -> dict:
    """Write `output` to `target` per `materialization`. Returns
    {"rows_written", "rows_deleted", "rows_flagged"}."""
    table = as_table(output)
    if materialization.mode == "replace":
        return _replace(table, target, storage_options)
    return _upsert(table, target, materialization, storage_options)


def _split_s3_path(path: str) -> tuple[str, str]:
    if not path.startswith("s3://"):
        raise MaterializeError(f"target path must be an s3:// url, got '{path}'")
    rest = path[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not key:
        raise MaterializeError(f"target path '{path}' has no object key")
    return bucket, key


def _replace(table: pa.Table, target: Target, storage_options: dict) -> dict:
    if target.format == "delta":
        # a single transaction: readers see the old table or the new one,
        # never a partial write (Constitution: failed runs never corrupt).
        write_deltalake(target.path, table, mode="overwrite", schema_mode="overwrite",
                        storage_options=storage_options)
    else:  # parquet — a single-object PUT is atomic on S3 (pattern: seed.py)
        buf = io.BytesIO()
        pq.write_table(table, buf)
        bucket, key = _split_s3_path(target.path)
        s3.client().put_object(Bucket=bucket, Key=key, Body=buf.getvalue())
    return {"rows_written": table.num_rows, "rows_deleted": 0, "rows_flagged": 0}


def _open_target(target: Target, storage_options: dict) -> Optional[DeltaTable]:
    try:
        return DeltaTable(target.path, storage_options=storage_options)
    except TableNotFoundError:
        return None


def _cursor(table: pa.Table):
    """A cursor with the run's output registered, for the guard queries."""
    cursor = duck.cursor()
    cursor.register(OUTPUT_RELATION, table)
    return cursor


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _guard_keys(table: pa.Table, keys: list[str]) -> None:
    missing = [k for k in keys if k not in table.column_names]
    if missing:
        raise MaterializeError(f"upsert key column(s) {missing} missing from pipeline output")
    cursor = _cursor(table)
    nulls = cursor.execute(
        f"SELECT count(*) FROM {OUTPUT_RELATION} WHERE "
        + " OR ".join(f"{_q(k)} IS NULL" for k in keys)).fetchone()[0]
    if nulls:
        raise MaterializeError(f"upsert output has null value(s) in key column(s) {keys}")
    key_list = ", ".join(_q(k) for k in keys)
    duplicates = cursor.execute(
        f"SELECT COALESCE(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM {OUTPUT_RELATION} "
        f"GROUP BY {key_list})").fetchone()[0]
    if duplicates:
        raise MaterializeError(
            f"upsert output has {int(duplicates)} duplicate key value(s) in {keys}")


def _guard_schema(table: pa.Table, existing: pa.Schema,
                  soft_delete_column: Optional[str]) -> None:
    """Compare the pipeline's raw output schema against the existing target's —
    excluding the soft-delete flag column, which is platform-managed and never
    expected in a pipeline's own output."""
    expected = {field.name: str(field.type) for field in existing}
    if soft_delete_column:
        expected.pop(soft_delete_column, None)
    actual = {field.name: str(field.type) for field in table.schema}
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        mismatched = sorted(c for c in set(expected) & set(actual) if expected[c] != actual[c])
        raise MaterializeError(
            "upsert output schema incompatible with existing target: "
            f"missing={missing} extra={extra} type_mismatch={mismatched}")


def _with_flag(table: pa.Table, column: str) -> pa.Table:
    """The output with the platform-managed soft-delete flag driven to False on
    every row this run touches — `when_matched_update_all` then clears it on
    any key that reappears after having been flagged."""
    values = pa.array([False] * table.num_rows, type=pa.bool_())
    if column in table.column_names:
        return table.set_column(table.column_names.index(column), column, values)
    return table.append_column(column, values)


def _upsert(table: pa.Table, target: Target, materialization: Materialization,
            storage_options: dict) -> dict:
    keys = materialization.keys
    _guard_keys(table, keys)

    dt = _open_target(target, storage_options)
    if dt is None:
        # first run against a target that doesn't exist yet: an initial write,
        # equivalent to replace for this one run (research U4) — nothing to
        # guard against and nothing to delete/flag. The flag column still needs
        # to exist on the table from this point on, so later runs' schema guard
        # (and when_matched_update_all) see it.
        if materialization.on_delete == "soft_delete":
            table = _with_flag(table, materialization.soft_delete_column)
        write_deltalake(target.path, table, mode="overwrite", storage_options=storage_options)
        return {"rows_written": table.num_rows, "rows_deleted": 0, "rows_flagged": 0}

    # schema guard runs against the pipeline's own output — before the
    # platform-managed soft-delete column (never part of that output) gets
    # added below, so the comparison isn't fooled by its own injected column.
    # pa.schema() rather than the returned object directly: deltalake hands
    # back an arro3 schema whose str(type) is a debug repr, which would make
    # every column look mismatched against the output's pyarrow types
    existing_schema = pa.schema(dt.schema().to_arrow())
    _guard_schema(table, existing_schema, materialization.soft_delete_column)

    existing_names = {field.name for field in existing_schema}
    if (materialization.on_delete == "soft_delete"
            and materialization.soft_delete_column not in existing_names):
        # The flag column must already exist on the target — it's only ever
        # added automatically on a *first* upsert run (above). Retrofitting
        # soft_delete onto an existing target (created by `replace`, or by an
        # earlier upsert with a different on_delete) needs an explicit schema
        # migration outside this run: deltalake's own merge-time schema
        # evolution mis-populates when_not_matched_by_source_update for a brand
        # new column (verified: it leaves those rows null instead of true), so
        # silently "fixing it up" here would produce wrong flags rather than a
        # clear error.
        raise MaterializeError(
            f"upsert target is missing the soft-delete column "
            f"'{materialization.soft_delete_column}' — replace the target once with "
            f"that column present (e.g. via a one-off replace run) before switching "
            f"this pipeline to on_delete: soft_delete")

    if materialization.on_delete == "soft_delete":
        table = _with_flag(table, materialization.soft_delete_column)

    if (table.num_rows == 0 and materialization.on_delete == "sync"
            and not materialization.allow_empty_sync):
        raise MaterializeError(
            "upsert output is empty and on_delete is 'sync' — this would delete every row in the "
            "target; set materialization.allow_empty_sync: true if that is really intended")

    rows_deleted = 0
    rows_flagged = 0
    if materialization.on_delete == "predicate":
        result = dt.delete(materialization.delete_predicate)
        rows_deleted = result.get("num_deleted_rows", 0) or 0

    if materialization.on_delete == "soft_delete" and keys:
        # "rows flagged this run" = target rows not present in this run's output
        # by key — whether newly flagged or re-affirmed, since the
        # not-matched-by-source update touches all of them every run.
        cursor = _cursor(table)
        on = " AND ".join(f"t.{_q(k)} IS NOT DISTINCT FROM o.{_q(k)}" for k in keys)
        rows_flagged = cursor.execute(
            f"SELECT count(*) FROM delta_scan(?) AS t "
            f"WHERE NOT EXISTS (SELECT 1 FROM {OUTPUT_RELATION} AS o WHERE {on})",
            [target.path]).fetchone()[0]

    merger = dt.merge(
        table, predicate=" AND ".join(f"target.{k} = source.{k}" for k in keys),
        source_alias="source", target_alias="target",
    ).when_matched_update_all().when_not_matched_insert_all()

    if materialization.on_delete == "sync":
        merger = merger.when_not_matched_by_source_delete()
    elif materialization.on_delete == "soft_delete":
        merger = merger.when_not_matched_by_source_update(
            {materialization.soft_delete_column: "true"})

    stats = merger.execute()
    # num_target_rows_updated bundles matched-row updates together with any
    # when_not_matched_by_source_update (the soft-delete flag write) — since
    # rows_flagged above counts exactly that not-matched-by-source set,
    # subtracting it recovers the true "rows actually upserted" count.
    rows_written = (stats.get("num_target_rows_inserted", 0) or 0) + \
                   max(0, (stats.get("num_target_rows_updated", 0) or 0) - rows_flagged)
    if materialization.on_delete == "sync":
        rows_deleted = stats.get("num_target_rows_deleted", 0) or 0

    return {"rows_written": rows_written, "rows_deleted": rows_deleted,
            "rows_flagged": rows_flagged}
