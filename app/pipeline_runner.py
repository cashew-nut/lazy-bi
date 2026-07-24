"""Pipeline run executor (specs/014-polars-pipeline-module/) — invoked as a
short-lived subprocess (`python -m app.pipeline_runner`) by the parent job
worker (app/pipeline_jobs.py). Never imported into the main FastAPI process:
this keeps a script crash or infinite loop from ever taking the app down,
and lets the parent enforce a hard timeout by killing the process (a plain
thread cannot be killed). Reads one JSON job spec from stdin, execs the
pipeline's script against real source scans, materializes the result via
app.materialize, and prints exactly one JSON result line to stdout — see
contracts/pipelines-api.md's runner protocol.

A pipeline script is real, admin-authored Python at application-code trust
(Principle VI) — like a model's `frame:` snippet, it is not sandboxed beyond
process isolation; the builtins available are unrestricted so ordinary
patterns (imports, comprehensions, helper functions) work as expected. The
one hygiene measure here is protecting the stdout protocol itself: script
output (prints, library warnings) is redirected to stderr for the duration
of the run so it can never be interleaved with — or mistaken for — the
single JSON result line the parent parses.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import traceback

import polars as pl

from . import iceberg_util
from .materialize import MaterializeError, materialize
from .pipelines import Materialization, Target


def _scan_source(fmt: str, path: str, storage_options: dict) -> pl.LazyFrame:
    """Mirrors app/engine.py's _scan_source: lazy end to end, whatever the
    source format. Iceberg ignores `storage_options` (same global bucket
    credentials as everything else) — see app/iceberg_util.py."""
    if fmt == "csv":
        return pl.scan_csv(path, storage_options=storage_options)
    if fmt == "delta":
        return pl.scan_delta(path, storage_options=storage_options)
    if fmt == "iceberg":
        return iceberg_util.scan(path)
    return pl.scan_parquet(path, storage_options=storage_options)


def _output_schema(output) -> list[dict] | None:
    try:
        schema = output.collect_schema() if isinstance(output, pl.LazyFrame) else output.schema
        return [{"name": n, "dtype": str(t)} for n, t in schema.items()]
    except Exception:
        return None  # best-effort only — the real failure surfaces from materialize() below


def run_job(job: dict) -> dict:
    """Executes one job spec end to end. No stdio side effects of its own
    (the caller owns stdout redirection) — kept pure enough to unit-test
    directly, without going through a real subprocess, in tests/test_pipelines.py."""
    pipeline = job["pipeline"]
    read_options = job["storage"]["read"]
    write_options = job["storage"]["write"]

    sources = {
        s["name"]: _scan_source(s["format"], s["path"], read_options)
        for s in pipeline["sources"]
    }
    namespace: dict = {"pl": pl, "sources": sources}
    try:
        exec(  # noqa: S102 - trusted config, application-code trust (Principle VI); see module docstring
            compile(pipeline["script"], f"<pipeline '{pipeline['name']}'>", "exec"),
            namespace,
        )
    except Exception as exc:
        return {"ok": False, "error": f"script error: {exc}\n{traceback.format_exc()}"}

    if "output" not in namespace:
        return {"ok": False, "error": "script did not assign a variable named 'output'"}
    output = namespace["output"]
    if not isinstance(output, (pl.LazyFrame, pl.DataFrame)):
        return {
            "ok": False,
            "error": f"'output' must be a polars LazyFrame or DataFrame, got {type(output).__name__}",
        }

    output_schema = _output_schema(output)
    target = Target(**pipeline["target"])
    materialization = Materialization(**pipeline["materialization"])

    try:
        stats = materialize(output, target, materialization, write_options)
    except MaterializeError as exc:
        return {"ok": False, "error": str(exc), "output_schema": output_schema}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"materialize error: {exc}\n{traceback.format_exc()}",
            "output_schema": output_schema,
        }

    return {"ok": True, "output_schema": output_schema, **stats}


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
