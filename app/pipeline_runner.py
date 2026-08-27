"""Pipeline run executor (specs/018-duckdb-sql-engine/) — invoked as a
short-lived subprocess (`python -m app.pipeline_runner`) by the parent job
worker (app/pipeline_jobs.py). Never imported into the main FastAPI process:
this keeps a runaway query from ever taking the app down, and lets the parent
enforce a hard timeout by killing the process (a thread cannot be killed, and
neither can a cross join). Reads one JSON job spec from stdin, runs the
pipeline's SQL against views over its real sources, materializes the result via
app.materialize, and prints exactly one JSON result line to stdout — see
contracts/pipelines-api.md's runner protocol.

A pipeline's SQL is admin-authored and keeps the table functions a measure may
never name, so it can read and write arbitrary bucket paths (Principle VI).
That reach — not code execution, which SQL does not offer — is what the admin
gate is measuring. The one hygiene measure here is protecting the stdout
protocol itself: anything the run prints goes to stderr for the duration, so it
can never be interleaved with the single JSON result line the parent parses.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import traceback

from . import duck
from .materialize import MaterializeError, materialize
from .pipelines import Materialization, Target, parse_statements, returns_rows

# what a pipeline's SQL may call its result instead of ending on a SELECT
OUTPUT_NAME = "output"


def _source_relation(fmt: str, path: str) -> str:
    """Mirrors app/duck.py's relation(): the same table functions, the same
    resolved file lists and the same catalog-free iceberg convention the query
    engine reads through."""
    return duck.relation(path, fmt)


def run_job(job: dict) -> dict:
    """Executes one job spec end to end. No stdio side effects of its own (the
    caller owns stdout redirection) — kept pure enough to unit-test directly,
    without going through a real subprocess, in tests/test_pipelines.py."""
    pipeline = job["pipeline"]
    write_options = job["storage"]["write"]

    cursor = duck.cursor()
    try:
        for source in pipeline["sources"]:
            cursor.execute(
                f'CREATE OR REPLACE TEMP VIEW "{source["name"]}" AS '
                f'SELECT * FROM {_source_relation(source["format"], source["path"])}')
    except Exception as exc:
        return {"ok": False, "error": f"source error: {exc}\n{traceback.format_exc()}"}

    try:
        table = _run_sql(cursor, pipeline["sql"], pipeline["name"])
    except Exception as exc:
        return {"ok": False, "error": f"sql error: {exc}\n{traceback.format_exc()}"}
    if table is None:
        return {"ok": False,
                "error": "the pipeline's sql produced no rows to materialize — it must end "
                         f"with a SELECT, or create a relation named '{OUTPUT_NAME}'"}
    output_schema = [{"name": f.name, "dtype": str(f.type)} for f in table.schema]
    target = Target(**pipeline["target"])
    materialization = Materialization(**pipeline["materialization"])

    try:
        stats = materialize(table, target, materialization, write_options)
    except MaterializeError as exc:
        return {"ok": False, "error": str(exc), "output_schema": output_schema}
    except Exception as exc:
        return {"ok": False,
                "error": f"materialize error: {exc}\n{traceback.format_exc()}",
                "output_schema": output_schema}

    return {"ok": True, "output_schema": output_schema, **stats}


def _run_sql(cursor, sql: str, owner: str):
    """Run every statement in order and return the rows this run materializes:
    a relation the script named `output` if it made one, else the result of its
    final statement — or None if neither produced rows.

    The last statement's rows are fetched *before* anything else is asked of
    the cursor: a DuckDB cursor holds one pending result, so the lookup for a
    named `output` would otherwise replace the very result it is deciding
    about. The named relation still wins, so a script that builds one up and
    then runs a diagnostic SELECT at the end materializes what it meant to."""
    last = None
    for statement in parse_statements(sql, owner):
        result = cursor.execute(statement.query)
        last = result.to_arrow_table() if returns_rows(statement) else None

    named = cursor.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE lower(table_name) = ? "
        "UNION ALL SELECT count(*) FROM duckdb_views() WHERE lower(view_name) = ?",
        [OUTPUT_NAME, OUTPUT_NAME]).fetchall()
    if any(row[0] for row in named):
        return cursor.execute(f'SELECT * FROM "{OUTPUT_NAME}"').to_arrow_table()
    return last


def main() -> None:
    job = json.loads(sys.stdin.read())
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            result = run_job(job)
    except Exception as exc:  # last-resort safety net: never exit without a result line
        result = {"ok": False, "error": f"runner error: {exc}\n{traceback.format_exc()}"}
    if captured.getvalue():
        sys.stderr.write(captured.getvalue())
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
