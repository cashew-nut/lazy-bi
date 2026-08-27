"""Sandbox notebook cell executor — invoked as a subprocess
(`python -m app.sandbox_runner`) by app/api/sandbox.py, the same isolation
shape as app/pipeline_runner.py: a runaway query can never take the app down,
and a hard timeout is enforced by the parent killing the process outright (a
plain thread cannot be killed, and neither can a cross join).

Unlike a pipeline run, a sandbox run is read-only (no materialization) and
answers a single HTTP request directly rather than going through a serialized
FIFO worker — pipelines serialize because a run *writes* a shared target; a
sandbox notebook only previews data, so concurrent runs are safe and desirable
for an interactive tool.

Runs every cell from the top through `run_upto` (inclusive) in one DuckDB
session, so a `CREATE TEMP VIEW` in cell 1 is visible in cell 3 like a real
notebook kernel — there is no persistent session between separate runs,
though: each run replays the whole prefix from scratch, trading a bit of
redundant recomputation for never having stale/drifted state to reason about.
Reports one JSON result line (see the contracts note in app/pipeline_runner.py
for why the runner protocol keeps stdout to exactly one line): per-cell error,
plus a preview of the rows the cell's last statement produced.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime, time as dt_time
from decimal import Decimal

import duckdb

from . import duck

ROW_LIMIT_DEFAULT = 200
TEXT_LIMIT = 4000


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


def _display(result, row_limit: int) -> dict | None:
    """The rows a cell's last statement produced, capped for preview.

    `None` for a statement that returns nothing worth showing (a CREATE, a
    SET) — the notebook renders those as a bare "ok" rather than as DuckDB's
    one-row Count. One row is fetched past the limit so "truncated" is honest
    without materializing the rest."""
    if result is None or result.description is None:
        return None
    columns = [{"name": name, "dtype": str(dtype)}
               for name, dtype in zip([d[0] for d in result.description],
                                      [d[1] for d in result.description])]
    rows = result.fetchmany(row_limit + 1)
    truncated = len(rows) > row_limit
    rows = rows[:row_limit]
    return {
        "kind": "table", "columns": columns,
        "rows": [_json_safe(dict(zip([c["name"] for c in columns], row))) for row in rows],
        "truncated": truncated, "row_count": None if truncated else len(rows),
    }


def _run_cell(source: str, cursor, row_limit: int) -> dict:
    """One cell: every statement in it, in order, with the last one's rows
    displayed. A cell is allowed to hold several statements — an admin
    pasting a setup script into one cell is a normal thing to do."""
    from .pipelines import PipelineError, parse_statements, returns_rows

    try:
        statements = parse_statements(source, "cell")
    except PipelineError as exc:
        return {"ok": False, "stdout": "", "error": str(exc).replace("cell: ", "", 1),
                "display": None}
    try:
        display = None
        for statement in statements:
            result = cursor.execute(statement.query)
            display = _display(result, row_limit) if returns_rows(statement) else None
    except duckdb.Error as exc:
        return {"ok": False, "stdout": "", "error": str(exc), "display": None}
    except Exception as exc:
        return {"ok": False, "stdout": "",
                "error": f"{exc}\n{traceback.format_exc()}", "display": None}
    return {"ok": True, "stdout": "", "error": None, "display": display}


def run_job(job: dict) -> dict:
    """Runs cells[0..run_upto] end to end. No stdio side effects of its own
    (the caller owns stdout redirection) — kept pure enough to unit-test
    directly, without going through a real subprocess, in tests/test_sandbox.py."""
    cursor = duck.cursor()
    row_limit = job.get("row_limit", ROW_LIMIT_DEFAULT)
    run_upto = job["run_upto"]
    cells = job["cells"]
    results = []
    stopped = False
    for i, cell in enumerate(cells):
        if i > run_upto or stopped:
            results.append({"id": cell["id"], "ok": None, "stdout": "", "error": None,
                            "display": None})
            continue
        res = _run_cell(cell["source"], cursor, row_limit)
        results.append({"id": cell["id"], **res})
        if not res["ok"]:
            stopped = True
    return {"ok": True, "cells": results}


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        result = run_job(job)
    except Exception as exc:  # last-resort safety net: never exit without a result line
        result = {"ok": False, "error": f"runner error: {exc}\n{traceback.format_exc()}",
                  "cells": []}
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
