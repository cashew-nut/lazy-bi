"""Sandbox notebooks: core text transforms (app/sandbox.py), the cell runner
(app/sandbox_runner.py, called directly — no subprocess needed to unit-test
its pure run_job()), and SandboxStore CRUD.
"""
import pytest

from app import pipelines, sandbox

# --- combine_cells -----------------------------------------------------------


def test_combine_cells_joins_with_a_statement_separator():
    assert sandbox.combine_cells(["SELECT 1", "SELECT 2"]) == "SELECT 1;\n\nSELECT 2;"


def test_combine_cells_does_not_double_a_semicolon_the_cell_already_has():
    assert sandbox.combine_cells(["SELECT 1;", "SELECT 2"]) == "SELECT 1;\n\nSELECT 2;"


def test_combine_cells_skips_blank_cells():
    assert sandbox.combine_cells(["SELECT 1", "   ", ""]) == "SELECT 1;"


# --- extract_reads / rewrite_reads_to_sources --------------------------------


def test_extract_reads_names_the_format_from_the_function():
    script = "SELECT * FROM read_parquet('s3://cash-intel/sales/x.parquet')"
    assert sandbox.extract_reads(script) == [
        {"name": "x", "path": "s3://cash-intel/sales/x.parquet", "format": "parquet"}]


def test_extract_reads_delta():
    sources = sandbox.extract_reads("SELECT * FROM delta_scan('s3://cash-intel/silver/orders')")
    assert sources[0]["format"] == "delta"


def test_extract_reads_iceberg():
    """SQL names the reader explicitly, so an iceberg table root is never
    mistaken for a delta one the way the old `read()` helper's extension guess
    could be."""
    sources = sandbox.extract_reads("SELECT * FROM iceberg_scan('s3://cash-intel/support/tickets')")
    assert sources[0]["format"] == "iceberg"


def test_extract_reads_csv():
    sources = sandbox.extract_reads("SELECT * FROM read_csv('s3://b/ref/products.csv')")
    assert sources[0]["format"] == "csv"


def test_extract_reads_dedupes_same_path():
    script = ("SELECT * FROM read_parquet('s3://b/sales/x.parquet') "
              "UNION ALL SELECT * FROM read_parquet('s3://b/sales/x.parquet')")
    assert len(sandbox.extract_reads(script)) == 1


def test_extract_reads_unique_names_for_colliding_basenames():
    script = ("SELECT * FROM read_parquet('s3://b/one/data.parquet') a "
              "JOIN read_parquet('s3://b/two/data.parquet') b USING (k)")
    names = [s["name"] for s in sandbox.extract_reads(script)]
    assert len(names) == len(set(names)) == 2


def test_extract_reads_sanitizes_non_identifier_basenames():
    sources = sandbox.extract_reads("SELECT * FROM delta_scan('s3://b/silver/orders-v2')")
    assert sources[0]["name"] == "orders_v2"


def test_extract_reads_glob_basename_falls_back_to_parent_segment():
    # "*.parquet" alone sanitizes to nothing useful — the dataset folder name
    # ("sales") makes a far more meaningful pipeline source name than a
    # generic fallback.
    sources = sandbox.extract_reads("SELECT * FROM read_parquet('s3://cash-intel/sales/*.parquet')")
    assert sources[0]["name"] == "sales"


def test_extract_reads_ignores_calls_mentioned_only_in_a_comment():
    script = (
        "-- e.g. read_parquet('s3://fake/not-a-real-source.parquet') explains the idea\n"
        "SELECT * FROM read_parquet('s3://cash-intel/sales/real.parquet')\n"
    )
    sources = sandbox.extract_reads(script)
    assert len(sources) == 1
    assert sources[0]["path"] == "s3://cash-intel/sales/real.parquet"


def test_extract_reads_ignores_a_block_comment():
    script = (
        "/* read_parquet('s3://fake/example.parquet')\n   spans two lines */\n"
        "SELECT * FROM read_parquet('s3://cash-intel/sales/real.parquet')\n"
    )
    assert [s["path"] for s in sandbox.extract_reads(script)] == \
        ["s3://cash-intel/sales/real.parquet"]


def test_rewrite_reads_to_sources_never_touches_comments():
    script = (
        "-- see read_parquet('s3://fake/example.parquet') for the idea\n"
        "SELECT * FROM read_parquet('s3://cash-intel/sales/real.parquet')\n"
    )
    sources = sandbox.extract_reads(script)
    rewritten = sandbox.rewrite_reads_to_sources(script, sources)
    assert "read_parquet('s3://fake/example.parquet')" in rewritten  # comment untouched
    assert '"real"' in rewritten
    assert rewritten.count("read_parquet(") == 1  # only the comment's mention remains


def test_extract_reads_preserves_first_appearance_order():
    script = ("SELECT * FROM read_parquet('s3://b/z.parquet') "
              "UNION ALL SELECT * FROM read_parquet('s3://b/a.parquet')")
    assert [s["path"] for s in sandbox.extract_reads(script)] == \
        ["s3://b/z.parquet", "s3://b/a.parquet"]


def test_rewrite_reads_to_sources_replaces_call_sites():
    """A pipeline registers each declared source as a view under its name, so
    the rewritten SQL reads FROM that name."""
    script = "SELECT a FROM read_parquet('s3://b/sales/x.parquet') WHERE a > 0"
    sources = sandbox.extract_reads(script)
    rewritten = sandbox.rewrite_reads_to_sources(script, sources)
    assert rewritten == 'SELECT a FROM "x" WHERE a > 0'


def test_rewrite_reads_to_sources_multiple_calls():
    script = (
        "SELECT * FROM read_parquet('s3://b/one.parquet') a\n"
        "JOIN read_csv('s3://b/two.csv') b USING (k)\n"
    )
    sources = sandbox.extract_reads(script)
    rewritten = sandbox.rewrite_reads_to_sources(script, sources)
    assert "read_parquet(" not in rewritten and "read_csv(" not in rewritten
    assert '"one"' in rewritten and '"two"' in rewritten


# --- has_output_assignment ----------------------------------------------------


def test_ending_on_a_select_satisfies_the_output_contract():
    assert sandbox.has_output_assignment("SELECT * FROM sales")
    assert sandbox.has_output_assignment("CREATE TEMP TABLE t AS SELECT 1;\nSELECT * FROM t")
    assert sandbox.has_output_assignment("WITH r AS (SELECT 1) SELECT * FROM r")


def test_a_named_output_relation_satisfies_it_too():
    assert sandbox.has_output_assignment("CREATE OR REPLACE TEMP VIEW output AS SELECT 1;")
    assert sandbox.has_output_assignment("CREATE TEMP TABLE output AS SELECT 1;\nSET threads = 2;")


def test_neither_is_reported_rather_than_guessed():
    assert not sandbox.has_output_assignment("CREATE TEMP TABLE t AS SELECT 1;")
    assert not sandbox.has_output_assignment("SET threads = 2;")


def test_output_mentioned_only_in_a_comment_does_not_count():
    assert not sandbox.has_output_assignment(
        "-- CREATE TEMP VIEW output AS SELECT 1\nCREATE TEMP TABLE t AS SELECT 1;")


# --- build_pipeline_yaml ------------------------------------------------------


def test_build_pipeline_yaml_parses_as_valid_pipeline_after_filling_placeholders():
    sources = [{"name": "sales", "path": "s3://b/sales/*.parquet", "format": "parquet"}]
    script = "SELECT * FROM sales LIMIT 5"
    yaml_text = sandbox.build_pipeline_yaml("my nb", script, sources)
    filled = yaml_text.replace("s3://REPLACE/ME/target   # TODO: set a real target path before saving",
                               "s3://b/silver/out")
    p = pipelines.parse_pipeline_text(filled)
    assert p.name == "my_nb"
    assert list(p.sources) == ["sales"]
    assert p.materialization.mode == "replace"


def test_build_pipeline_yaml_slugifies_name():
    yaml_text = sandbox.build_pipeline_yaml("My Cool NB!", "SELECT 1", [])
    assert "name: my_cool_nb" in yaml_text


def test_build_pipeline_yaml_no_sources_gets_placeholder():
    yaml_text = sandbox.build_pipeline_yaml("nb", "SELECT 1", [])
    assert "s3://REPLACE/ME" in yaml_text


def test_build_pipeline_yaml_preserves_the_sql_body():
    script = "CREATE TEMP TABLE t AS SELECT 1;\nSELECT * FROM t"
    yaml_text = sandbox.build_pipeline_yaml("nb", script, [])
    assert "  CREATE TEMP TABLE t AS SELECT 1;" in yaml_text
    assert "  SELECT * FROM t" in yaml_text


# --- sandbox_runner.run_job (direct call, no subprocess) ---------------------

from app import sandbox_runner  # noqa: E402


def _job(cells, run_upto=None):
    return {
        "cells": [{"id": str(i), "source": c} for i, c in enumerate(cells)],
        "run_upto": run_upto if run_upto is not None else len(cells) - 1,
        "bucket": "test-bucket",
        "row_limit": 200,
    }


def test_run_job_displays_the_rows_a_cell_produced():
    result = sandbox_runner.run_job(_job(["SELECT 1 + 1 AS x"]))
    cell = result["cells"][0]
    assert cell["ok"] is True
    assert cell["display"]["kind"] == "table"
    assert cell["display"]["rows"] == [{"x": 2}]


def test_run_job_state_carries_across_cells():
    """One session per run, so a temp view in cell 1 is visible in cell 3 —
    the notebook-kernel property, without a kernel."""
    result = sandbox_runner.run_job(_job([
        "CREATE OR REPLACE TEMP VIEW v AS SELECT 5 AS x",
        "CREATE OR REPLACE TEMP VIEW w AS SELECT x + 1 AS y FROM v",
        "SELECT * FROM w",
    ]))
    assert result["cells"][2]["display"]["rows"] == [{"y": 6}]


def test_run_job_error_stops_subsequent_cells():
    result = sandbox_runner.run_job(_job(["SELECT * FROM no_such_table", "SELECT 1", "SELECT 2"]))
    cells = result["cells"]
    assert cells[0]["ok"] is False
    assert "no_such_table" in cells[0]["error"]
    assert cells[1]["ok"] is None and cells[2]["ok"] is None  # never run


def test_run_job_run_upto_stops_early():
    result = sandbox_runner.run_job(_job(["SELECT 1", "SELECT 2", "SELECT 3"], run_upto=1))
    cells = result["cells"]
    assert cells[0]["ok"] is True and cells[1]["ok"] is True
    assert cells[2]["ok"] is None  # beyond run_upto, not executed


def test_run_job_syntax_error_reported_without_crashing():
    result = sandbox_runner.run_job(_job(["SELECT FROM WHERE"]))
    assert result["cells"][0]["ok"] is False
    assert "syntax error" in result["cells"][0]["error"]


def test_run_job_display_carries_column_types():
    result = sandbox_runner.run_job(_job(["SELECT 1 AS a, 'x' AS b"]))
    disp = result["cells"][0]["display"]
    assert [c["name"] for c in disp["columns"]] == ["a", "b"]
    assert disp["rows"] == [{"a": 1, "b": "x"}]
    assert disp["truncated"] is False
    assert disp["row_count"] == 1


def test_run_job_display_truncates_at_the_row_limit():
    job = _job(["SELECT * FROM range(5) t(a)"])
    job["row_limit"] = 2
    disp = sandbox_runner.run_job(job)["cells"][0]["display"]
    assert disp["kind"] == "table"
    assert disp["truncated"] is True
    assert len(disp["rows"]) == 2
    assert disp["row_count"] is None


def test_run_job_can_read_the_bucket(seeded):
    """The one capability that makes a notebook worth having: a cell may name
    a table function and read a real path."""
    result = sandbox_runner.run_job(_job([
        "SELECT count(*) AS n FROM read_parquet('s3://cash-intel/sales/*.parquet')"]))
    assert result["cells"][0]["ok"] is True, result["cells"][0]["error"]
    assert result["cells"][0]["display"]["rows"][0]["n"] == 60000


def test_run_job_none_display_for_a_statement_that_returns_nothing():
    result = sandbox_runner.run_job(_job(["CREATE OR REPLACE TEMP TABLE t AS SELECT 1"]))
    assert result["cells"][0]["ok"] is True
    assert result["cells"][0]["display"] is None


def test_run_job_runs_every_statement_in_a_cell():
    result = sandbox_runner.run_job(_job([
        "CREATE OR REPLACE TEMP TABLE t AS SELECT 7 AS x; SELECT * FROM t"]))
    assert result["cells"][0]["display"]["rows"] == [{"x": 7}]


# --- SandboxStore CRUD --------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    from app.sandboxstore import SandboxStore

    return SandboxStore(tmp_path / "test_sandbox.db")


def test_store_create_and_get(store):
    nb = store.create("scratch", [{"id": "c1", "source": "1 + 1"}])
    assert nb["name"] == "scratch"
    fetched = store.get(nb["id"])
    assert fetched["cells"] == [{"id": "c1", "source": "1 + 1"}]


def test_store_list_omits_cells(store):
    store.create("scratch", [{"id": "c1", "source": "1"}])
    listed = store.list()
    assert "cells" not in listed[0]
    assert listed[0]["name"] == "scratch"


def test_store_update(store):
    nb = store.create("scratch", [{"id": "c1", "source": "1"}])
    updated = store.update(nb["id"], "renamed", [{"id": "c1", "source": "2"}])
    assert updated["name"] == "renamed"
    assert updated["cells"][0]["source"] == "2"


def test_store_update_unknown_returns_none(store):
    assert store.update(999, "x", []) is None


def test_store_delete(store):
    nb = store.create("scratch", [])
    assert store.delete(nb["id"]) is True
    assert store.get(nb["id"]) is None
    assert store.delete(nb["id"]) is False
