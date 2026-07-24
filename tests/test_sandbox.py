"""Sandbox notebooks: core text transforms (app/sandbox.py), the cell runner
(app/sandbox_runner.py, called directly — no subprocess needed to unit-test
its pure run_job()), and SandboxStore CRUD.
"""
import pytest

from app import pipelines, sandbox

# --- combine_cells -----------------------------------------------------------


def test_combine_cells_joins_with_blank_line():
    assert sandbox.combine_cells(["a = 1", "b = 2"]) == "a = 1\n\nb = 2"


def test_combine_cells_skips_blank_cells():
    assert sandbox.combine_cells(["a = 1", "   ", ""]) == "a = 1"


# --- extract_reads / rewrite_reads_to_sources --------------------------------


def test_extract_reads_infers_format_from_extension():
    script = 'df = read("s3://cash-intel/sales/x.parquet")'
    sources = sandbox.extract_reads(script)
    assert sources == [{"name": "x", "path": "s3://cash-intel/sales/x.parquet", "format": "parquet"}]


def test_extract_reads_explicit_format_wins():
    script = 'df = read("s3://cash-intel/silver/orders", format="delta")'
    sources = sandbox.extract_reads(script)
    assert sources[0]["format"] == "delta"


def test_extract_reads_explicit_iceberg_format():
    script = 'df = read("s3://cash-intel/support/tickets", format="iceberg")'
    sources = sandbox.extract_reads(script)
    assert sources[0]["format"] == "iceberg"


def test_extract_reads_csv_extension():
    sources = sandbox.extract_reads('read("s3://b/ref/products.csv")')
    assert sources[0]["format"] == "csv"


def test_extract_reads_no_extension_defaults_delta():
    sources = sandbox.extract_reads('read("s3://b/logistics/shipments")')
    assert sources[0]["format"] == "delta"


def test_extract_reads_dedupes_same_path():
    script = (
        'a = read("s3://b/sales/x.parquet")\n'
        'b = read("s3://b/sales/x.parquet")\n'
    )
    sources = sandbox.extract_reads(script)
    assert len(sources) == 1


def test_extract_reads_unique_names_for_colliding_basenames():
    script = (
        'a = read("s3://b/one/data.parquet")\n'
        'b = read("s3://b/two/data.parquet")\n'
    )
    sources = sandbox.extract_reads(script)
    names = [s["name"] for s in sources]
    assert len(names) == len(set(names)) == 2


def test_extract_reads_sanitizes_non_identifier_basenames():
    sources = sandbox.extract_reads('read("s3://b/silver/orders-v2")')
    assert sources[0]["name"] == "orders_v2"


def test_extract_reads_glob_basename_falls_back_to_parent_segment():
    # "*.parquet" alone sanitizes to nothing useful — the dataset folder
    # name ("sales") makes a far more meaningful pipeline source name than a
    # generic fallback.
    sources = sandbox.extract_reads('read("s3://cash-intel/sales/*.parquet")')
    assert sources[0]["name"] == "sales"


def test_extract_reads_ignores_calls_mentioned_only_in_a_comment():
    script = (
        '# e.g. read("s3://fake/not-a-real-source.parquet") explains the idea\n'
        'df = read("s3://cash-intel/sales/real.parquet")\n'
    )
    sources = sandbox.extract_reads(script)
    assert len(sources) == 1
    assert sources[0]["path"] == "s3://cash-intel/sales/real.parquet"


def test_rewrite_reads_to_sources_never_touches_comments():
    script = (
        '# see read("s3://fake/example.parquet") for the idea\n'
        'df = read("s3://cash-intel/sales/real.parquet")\n'
    )
    sources = sandbox.extract_reads(script)
    rewritten = sandbox.rewrite_reads_to_sources(script, sources)
    assert 'read("s3://fake/example.parquet")' in rewritten  # comment untouched
    assert 'sources["real"]' in rewritten
    assert rewritten.count("read(") == 1  # only the comment's mention remains


def test_extract_reads_preserves_first_appearance_order():
    script = 'read("s3://b/z.parquet")\nread("s3://b/a.parquet")\n'
    sources = sandbox.extract_reads(script)
    assert [s["path"] for s in sources] == ["s3://b/z.parquet", "s3://b/a.parquet"]


def test_rewrite_reads_to_sources_replaces_call_sites():
    script = 'df = read("s3://b/sales/x.parquet").filter(pl.col("a") > 0)'
    sources = sandbox.extract_reads(script)
    rewritten = sandbox.rewrite_reads_to_sources(script, sources)
    assert rewritten == f'df = sources["{sources[0]["name"]}"].filter(pl.col("a") > 0)'


def test_rewrite_reads_to_sources_multiple_calls():
    script = (
        'a = read("s3://b/one.parquet")\n'
        'b = read("s3://b/two.csv", format="csv")\n'
        'output = a.join(b, on="k")\n'
    )
    sources = sandbox.extract_reads(script)
    rewritten = sandbox.rewrite_reads_to_sources(script, sources)
    assert 'read(' not in rewritten
    assert 'sources["one"]' in rewritten
    assert 'sources["two"]' in rewritten


# --- has_output_assignment ----------------------------------------------------


def test_has_output_assignment_true():
    assert sandbox.has_output_assignment('x = 1\noutput = x\n')


def test_has_output_assignment_false():
    assert not sandbox.has_output_assignment('x = 1\nresult = x\n')


def test_has_output_assignment_ignores_indented_assignment():
    # only a *top-level* `output =` counts — one inside a function/if-block
    # is not what a pipeline script's contract means (see contracts/pipeline-yaml.md)
    assert not sandbox.has_output_assignment('if True:\n    output = 1\n')


# --- build_pipeline_yaml ------------------------------------------------------


def test_build_pipeline_yaml_parses_as_valid_pipeline_after_filling_placeholders():
    sources = [{"name": "sales", "path": "s3://b/sales/*.parquet", "format": "parquet"}]
    script = 'output = sources["sales"].head(5)'
    yaml_text = sandbox.build_pipeline_yaml("my nb", script, sources)
    filled = yaml_text.replace("s3://REPLACE/ME/target   # TODO: set a real target path before saving",
                                "s3://b/silver/out")
    p = pipelines.parse_pipeline_text(filled)
    assert p.name == "my_nb"
    assert list(p.sources) == ["sales"]
    assert p.materialization.mode == "replace"


def test_build_pipeline_yaml_slugifies_name():
    yaml_text = sandbox.build_pipeline_yaml("My Cool NB!", "output = 1", [])
    assert "name: my_cool_nb" in yaml_text


def test_build_pipeline_yaml_no_sources_gets_placeholder():
    yaml_text = sandbox.build_pipeline_yaml("nb", "output = 1", [])
    assert "s3://REPLACE/ME" in yaml_text


def test_build_pipeline_yaml_preserves_script_body():
    script = 'a = 1\noutput = a'
    yaml_text = sandbox.build_pipeline_yaml("nb", script, [])
    assert "  a = 1" in yaml_text
    assert "  output = a" in yaml_text


# --- sandbox_runner.run_job (direct call, no subprocess) ---------------------

from app import sandbox_runner  # noqa: E402


def _job(cells, run_upto=None):
    return {
        "cells": [{"id": str(i), "source": c} for i, c in enumerate(cells)],
        "run_upto": run_upto if run_upto is not None else len(cells) - 1,
        "bucket": "test-bucket",
        "row_limit": 200,
        "storage": {"read": {}},
    }


def test_run_job_simple_arithmetic_last_expr_displayed():
    result = sandbox_runner.run_job(_job(["x = 1 + 1", "x * 10"]))
    cells = result["cells"]
    assert cells[0]["ok"] is True and cells[0]["display"] is None  # assignment, nothing to display
    assert cells[1]["ok"] is True
    assert cells[1]["display"] == {"kind": "text", "text": "20"}


def test_run_job_state_carries_across_cells():
    result = sandbox_runner.run_job(_job(["x = 5", "y = x + 1", "y"]))
    assert result["cells"][2]["display"] == {"kind": "text", "text": "6"}


def test_run_job_stdout_captured_per_cell():
    result = sandbox_runner.run_job(_job(['print("hello")', 'print("world")']))
    assert result["cells"][0]["stdout"] == "hello\n"
    assert result["cells"][1]["stdout"] == "world\n"


def test_run_job_error_stops_subsequent_cells():
    result = sandbox_runner.run_job(_job(["1 / 0", "z = 1", "z"]))
    cells = result["cells"]
    assert cells[0]["ok"] is False
    assert "ZeroDivisionError" in cells[0]["error"]
    assert cells[1]["ok"] is None and cells[2]["ok"] is None  # never run


def test_run_job_run_upto_stops_early():
    result = sandbox_runner.run_job(_job(["a = 1", "b = 2", "c = 3"], run_upto=1))
    cells = result["cells"]
    assert cells[0]["ok"] is True and cells[1]["ok"] is True
    assert cells[2]["ok"] is None  # beyond run_upto, not executed


def test_run_job_syntax_error_reported_without_crashing():
    result = sandbox_runner.run_job(_job(["def broken(:"]))
    assert result["cells"][0]["ok"] is False
    assert "syntax error" in result["cells"][0]["error"]


def test_run_job_dataframe_display_shape():
    import polars as pl  # noqa: F401  (imported inside the cell's namespace)
    result = sandbox_runner.run_job(_job(['pl.DataFrame({"a": [1, 2], "b": ["x", "y"]})']))
    disp = result["cells"][0]["display"]
    assert disp["kind"] == "table"
    assert disp["columns"] == [{"name": "a", "dtype": "Int64"}, {"name": "b", "dtype": "String"}]
    assert disp["rows"] == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert disp["truncated"] is False
    assert disp["row_count"] == 2


def test_run_job_lazyframe_display_collects_and_truncates():
    job = _job(['pl.LazyFrame({"a": list(range(5))})'])
    job["row_limit"] = 2
    result = sandbox_runner.run_job(job)
    disp = result["cells"][0]["display"]
    assert disp["kind"] == "table"
    assert disp["truncated"] is True
    assert len(disp["rows"]) == 2
    assert disp["row_count"] is None


def test_run_job_read_helper_uses_storage_options_and_infers_format(monkeypatch):
    calls = []

    def fake_scan_parquet(path, storage_options=None):
        calls.append(("parquet", path, storage_options))
        import polars as pl
        return pl.DataFrame({"a": [1]}).lazy()

    monkeypatch.setattr(sandbox_runner.pl, "scan_parquet", fake_scan_parquet)
    job = _job(['read("s3://b/x.parquet")'])
    job["storage"]["read"] = {"aws_endpoint_url": "http://x"}
    result = sandbox_runner.run_job(job)
    assert result["cells"][0]["ok"] is True
    assert calls == [("parquet", "s3://b/x.parquet", {"aws_endpoint_url": "http://x"})]


def test_run_job_read_helper_iceberg_requires_explicit_format(monkeypatch):
    calls = []

    def fake_iceberg_scan(path):
        calls.append(path)
        import polars as pl
        return pl.DataFrame({"a": [1]}).lazy()

    monkeypatch.setattr(sandbox_runner.iceberg_util, "scan", fake_iceberg_scan)
    job = _job(['read("s3://cash-intel/support/tickets", format="iceberg")'])
    result = sandbox_runner.run_job(job)
    assert result["cells"][0]["ok"] is True
    assert calls == ["s3://cash-intel/support/tickets"]


def test_run_job_none_display_for_plain_statement():
    result = sandbox_runner.run_job(_job(["x = 1"]))
    assert result["cells"][0]["display"] is None


def test_run_job_text_display_truncates_long_repr():
    result = sandbox_runner.run_job(_job(["'x' * 5000"]))
    disp = result["cells"][0]["display"]
    assert disp["kind"] == "text"
    assert disp["text"].endswith("… (truncated)")
    assert len(disp["text"]) < 5000


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
