"""Sandbox notebook cell executor — invoked as a subprocess
(`python -m app.sandbox_runner`) by app/api/sandbox.py, the same isolation
shape as app/pipeline_runner.py: a script crash or infinite loop can never
take the app down, and a hard timeout is enforced by the parent killing the
process outright (a plain thread cannot be killed).

Unlike a pipeline run, a sandbox run is read-only (no materialization) and
answers a single HTTP request directly rather than going through a
serialized FIFO worker — pipelines serialize because a run *writes* a
shared target; a sandbox notebook only previews data, so concurrent runs
are safe and desirable for an interactive tool.

Executes every cell from the top through `run_upto` (inclusive) in one
shared namespace, so later cells see earlier cells' variables like a real
notebook kernel — there is no persistent kernel between separate runs,
though: each run replays the whole prefix from scratch, trading a bit of
redundant recomputation for never having stale/drifted state to reason
about. Reports one JSON result line (see contracts note in app/pipeline_
runner.py for why the runner protocol keeps stdout to exactly one line):
per-cell stdout/error, plus a JSON-safe preview of the cell's last bare
expression — Jupyter's auto-display convention — when it evaluates to a
polars frame or other displayable value.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import traceback
from datetime import date, datetime, time as dt_time
from decimal import Decimal

import polars as pl

from . import iceberg_util

ROW_LIMIT_DEFAULT = 200
TEXT_LIMIT = 4000


def _infer_format(path: str) -> str:
    """Extension-based guess. Delta and Iceberg are both bare directories, so
    a path with no recognized extension defaults to delta (matching
    app/pipelines.py/app/sandbox.py's default) — reading an iceberg table
    needs an explicit `format="iceberg"` argument."""
    lower = path.lower()
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith(".parquet"):
        return "parquet"
    return "delta"


def _make_read(storage_options: dict):
    def read(path: str, format: str | None = None) -> pl.LazyFrame:
        fmt = format or _infer_format(path)
        if fmt == "csv":
            return pl.scan_csv(path, storage_options=storage_options)
        if fmt == "delta":
            return pl.scan_delta(path, storage_options=storage_options)
        if fmt == "iceberg":
            return iceberg_util.scan(path)
        return pl.scan_parquet(path, storage_options=storage_options)
    return read


def _json_safe(value):
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return repr(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _table_display(df: pl.DataFrame, row_limit: int) -> dict:
    truncated = df.height > row_limit
    rows = df.head(row_limit).to_dicts()
    columns = [{"name": n, "dtype": str(t)} for n, t in df.schema.items()]
    return {
        "kind": "table", "columns": columns, "rows": [_json_safe(r) for r in rows],
        "truncated": truncated, "row_count": None if truncated else df.height,
    }


def _display(value, row_limit: int) -> dict | None:
    """Jupyter-style auto-display for a cell's last bare expression."""
    if value is None:
        return None
    if isinstance(value, pl.LazyFrame):
        try:
            value = value.limit(row_limit + 1).collect()
        except Exception as exc:
            return {"kind": "text", "text": f"<error materializing LazyFrame preview: {exc}>"}
    if isinstance(value, pl.Series):
        value = value.to_frame()
    if isinstance(value, pl.DataFrame):
        return _table_display(value, row_limit)
    text = repr(value)
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT] + "… (truncated)"
    return {"kind": "text", "text": text}


def _run_cell(source: str, namespace: dict, row_limit: int) -> dict:
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return {"ok": False, "stdout": "", "error": f"syntax error: {exc}", "display": None}
    last = tree.body[-1] if tree.body else None
    is_expr = isinstance(last, ast.Expr)
    body = tree.body[:-1] if is_expr else tree.body
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            if body:
                exec(  # noqa: S102 - trusted config, application-code trust (Principle VI); see module docstring
                    compile(ast.Module(body=body, type_ignores=[]), "<cell>", "exec"), namespace,
                )
            value = None
            if is_expr:
                value = eval(  # noqa: S307 - same trust level as the exec() above
                    compile(ast.Expression(body=last.value), "<cell>", "eval"), namespace,
                )
    except Exception as exc:
        return {"ok": False, "stdout": out.getvalue(), "error": f"{exc}\n{traceback.format_exc()}", "display": None}
    return {"ok": True, "stdout": out.getvalue(), "error": None, "display": _display(value, row_limit)}


def run_job(job: dict) -> dict:
    """Executes cells[0..run_upto] end to end. No stdio side effects of its
    own (the caller owns stdout redirection) — kept pure enough to unit-test
    directly, without going through a real subprocess, in tests/test_sandbox.py."""
    namespace: dict = {
        "pl": pl, "bucket": job.get("bucket", ""),
        "read": _make_read(job["storage"]["read"]),
    }
    row_limit = job.get("row_limit", ROW_LIMIT_DEFAULT)
    run_upto = job["run_upto"]
    cells = job["cells"]
    results = []
    stopped = False
    for i, cell in enumerate(cells):
        if i > run_upto or stopped:
            results.append({"id": cell["id"], "ok": None, "stdout": "", "error": None, "display": None})
            continue
        res = _run_cell(cell["source"], namespace, row_limit)
        results.append({"id": cell["id"], **res})
        if not res["ok"]:
            stopped = True
    return {"ok": True, "cells": results}


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        result = run_job(job)
    except Exception as exc:  # last-resort safety net: never exit without a result line
        result = {"ok": False, "error": f"runner error: {exc}\n{traceback.format_exc()}", "cells": []}
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
