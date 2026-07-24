"""app.sandbox / app.sandbox_agent: the coding agent's context building and —
the part that matters — the re-validation every unvalidated model reply goes
through before it can reach a notebook cell or a pipeline yaml. Zero network
calls: the LLM seam is exercised through the same scripted-fake pattern
tests/test_nlq.py uses for the chat translator.
"""
from __future__ import annotations

import pytest
import yaml

from app import sandbox, sandbox_agent


# ── proposed-cell validation ─────────────────────────────────────────────

def test_valid_cells_pass_through_with_targets_resolved():
    cells, warnings = sandbox.validate_agent_cells(
        [{"target": "c1", "source": "df = read('s3://b/x.parquet')"},
         {"target": sandbox.NEW_CELL, "source": "df.head()"}],
        ["c1", "c2"],
    )
    assert warnings == []
    assert [c["target_id"] for c in cells] == ["c1", None]
    assert all(c["syntax_error"] is None for c in cells)


def test_unknown_target_is_downgraded_to_a_new_cell():
    cells, warnings = sandbox.validate_agent_cells(
        [{"target": "does_not_exist", "source": "x = 1"}], ["c1"])
    assert cells[0]["target_id"] is None
    assert any("isn't a cell" in w for w in warnings)


def test_duplicate_target_keeps_only_the_first():
    cells, warnings = sandbox.validate_agent_cells(
        [{"target": "c1", "source": "x = 1"}, {"target": "c1", "source": "x = 2"}], ["c1"])
    assert [c["source"] for c in cells] == ["x = 1"]
    assert any("second proposal" in w for w in warnings)


def test_syntax_error_is_reported_not_dropped():
    """A scratch notebook is exactly where a half-written cell is still
    useful — the admin fixes it in place rather than losing the proposal."""
    cells, warnings = sandbox.validate_agent_cells(
        [{"target": "new", "source": "df = read('x'"}], ["c1"])
    assert len(cells) == 1
    assert "syntax error" in cells[0]["syntax_error"]
    assert warnings == []


def test_sourceless_and_malformed_cells_are_dropped():
    cells, warnings = sandbox.validate_agent_cells(
        ["nonsense", {"target": "c1"}, {"target": "c1", "source": "   "}], ["c1"])
    assert cells == []
    assert warnings


def test_non_list_reply_is_reported():
    cells, warnings = sandbox.validate_agent_cells({"cells": "oops"}, ["c1"])
    assert cells == []
    assert warnings == ["the agent returned no usable cells"]


def test_cell_count_is_capped():
    proposed = [{"target": "new", "source": f"x = {i}"} for i in range(sandbox.MAX_AGENT_CELLS + 5)]
    cells, warnings = sandbox.validate_agent_cells(proposed, [])
    assert len(cells) == sandbox.MAX_AGENT_CELLS
    assert any("kept the first" in w for w in warnings)


# ── lineage validation ───────────────────────────────────────────────────

def test_lineage_keeps_grounded_entries():
    entries, warnings = sandbox.validate_lineage(
        [{"field": "net_revenue", "from": ["orders.unit_price", "orders.quantity"],
          "transform": "unit_price × quantity\nper order line"}],
        ["orders"], ["net_revenue"],
    )
    assert warnings == []
    assert entries == [{
        "field": "net_revenue", "from": ["orders.unit_price", "orders.quantity"],
        "transform": "unit_price × quantity per order line",
    }]


def test_lineage_drops_fields_the_run_never_produced():
    entries, warnings = sandbox.validate_lineage(
        [{"field": "invented", "from": ["orders.id"]}], ["orders"], ["order_id"])
    assert entries == []
    assert any("not a column the run produced" in w for w in warnings)


def test_lineage_drops_entries_whose_refs_are_all_undeclared():
    """app/pipelines.py refuses to load a pipeline citing an unknown source,
    so a generated section must never contain one."""
    entries, warnings = sandbox.validate_lineage(
        [{"field": "revenue", "from": ["mystery_frame.amount"]}], ["orders"], ["revenue"])
    assert entries == []
    assert any("cited no declared source" in w for w in warnings)


def test_lineage_keeps_partially_valid_refs_with_a_warning():
    entries, warnings = sandbox.validate_lineage(
        [{"field": "revenue", "from": ["orders.amount", "mystery.x"]}], ["orders"], ["revenue"])
    assert entries[0]["from"] == ["orders.amount"]
    assert any("undeclared sources" in w for w in warnings)


def test_lineage_without_a_run_takes_field_names_at_face_value():
    entries, warnings = sandbox.validate_lineage(
        [{"field": "whatever", "from": ["orders.x"]}], ["orders"], [])
    assert [e["field"] for e in entries] == ["whatever"]
    assert warnings == []


def test_lineage_deduplicates_fields_and_refs():
    entries, warnings = sandbox.validate_lineage(
        [{"field": "a", "from": ["s.x", "s.x"]}, {"field": "a", "from": ["s.y"]}], ["s"], ["a"])
    assert entries == [{"field": "a", "from": ["s.x"], "transform": ""}]
    assert any("duplicate" in w for w in warnings)


# ── generated yaml ───────────────────────────────────────────────────────

def test_generated_yaml_with_lineage_loads_as_a_real_pipeline():
    from app import pipelines

    entries, _ = sandbox.validate_lineage(
        [{"field": "net_revenue", "from": ["orders.unit_price"], "transform": "per line: price × qty"}],
        ["orders"], ["net_revenue"],
    )
    text = sandbox.build_pipeline_yaml(
        "my nb", 'output = sources["orders"]',
        [{"name": "orders", "format": "parquet", "path": "s3://cash-intel/sales/*.parquet"}],
        entries, "Cleaned order lines: enriched, deduped.",
    )
    parsed = pipelines.parse_pipeline_text(
        text.replace("s3://REPLACE/ME/target   # TODO: set a real target path before saving",
                     "s3://cash-intel/pipeline_test/from_sandbox"))
    assert parsed.description == "Cleaned order lines: enriched, deduped."
    assert parsed.to_public()["lineage"] == [
        {"field": "net_revenue", "from": ["orders.unit_price"], "transform": "per line: price × qty"}]


def test_generated_yaml_quotes_scalars_that_need_it():
    """A description or transform containing ':' or '#' would silently break
    the document if written as a plain scalar."""
    text = sandbox.build_pipeline_yaml(
        "nb", "output = 1", [{"name": "s", "format": "csv", "path": "s3://b/x.csv"}],
        [{"field": "a", "from": ["s.x"], "transform": "note: has a colon #and a hash"}],
        "desc: with a colon",
    )
    loaded = yaml.safe_load(text)
    assert loaded["description"] == "desc: with a colon"
    assert loaded["lineage"][0]["transform"] == "note: has a colon #and a hash"


def test_yaml_without_lineage_is_unchanged_shape():
    text = sandbox.build_pipeline_yaml("nb", "output = 1", [])
    loaded = yaml.safe_load(text)
    assert "lineage" not in loaded
    assert "description" not in loaded


# ── bucket listing collapse ──────────────────────────────────────────────

def test_bucket_entries_collapse_table_roots():
    entries = sandbox.bucket_entries([
        {"key": "logistics/shipments/_delta_log/00000.json", "size": 10},
        {"key": "logistics/shipments/part-0.parquet", "size": 100},
        {"key": "support/tickets/metadata/v1.metadata.json", "size": 5},
        {"key": "support/tickets/data/part-0.parquet", "size": 50},
        {"key": "ref/products.csv", "size": 7},
        {"key": "sales/2024.parquet", "size": 9},
        {"key": "notes/readme.txt", "size": 3},
    ], "cash-intel")
    by_path = {e["path"]: e for e in entries}
    assert by_path["s3://cash-intel/logistics/shipments"] == {
        "path": "s3://cash-intel/logistics/shipments", "format": "delta", "size": 110}
    assert by_path["s3://cash-intel/support/tickets"]["format"] == "iceberg"
    assert by_path["s3://cash-intel/ref/products.csv"]["format"] == "csv"
    assert by_path["s3://cash-intel/sales/2024.parquet"]["format"] == "parquet"
    # a table's internal files never show up on their own, and an unreadable
    # object isn't offered as a read() target at all
    assert not any("_delta_log" in p or p.endswith(".txt") for p in by_path)


# ── prompt/context assembly ──────────────────────────────────────────────

def _notebook(**kwargs):
    defaults = dict(
        name="scratch",
        cells=[sandbox_agent.CellContext(
            id="c1", source="df = read('s3://b/x.parquet')", status="error",
            error="ComputeError: column 'nope' not found", stdout="hello",
            columns=[{"name": "order_id", "dtype": "Int64"}], row_count=3)],
        files=[{"path": "s3://cash-intel/ref/products.csv", "format": "csv", "size": 7}],
        bucket="cash-intel",
    )
    return sandbox_agent.NotebookContext(**{**defaults, **kwargs})


def test_prompt_carries_cells_errors_and_schemas():
    prompt = sandbox_agent.build_assist_prompt("make it fast", _notebook(), [])
    assert "df = read('s3://b/x.parquet')" in prompt
    assert "id: c1" in prompt
    assert "ComputeError" in prompt
    assert "order_id Int64" in prompt
    assert "s3://cash-intel/ref/products.csv" in prompt
    assert "make it fast" in prompt


def test_prompt_truncates_a_huge_cell(monkeypatch):
    monkeypatch.setattr(sandbox_agent.config, "SANDBOX_AGENT_CELL_CHARS", 50)
    notebook = _notebook(cells=[sandbox_agent.CellContext(id="c1", source="x = 1  # " + "y" * 5000)])
    prompt = sandbox_agent.build_assist_prompt("go", notebook, [])
    assert "truncated" in prompt
    assert len(prompt) < 2000


def test_prompt_keeps_the_tail_of_a_long_traceback(monkeypatch):
    monkeypatch.setattr(sandbox_agent.config, "SANDBOX_AGENT_OUTPUT_CHARS", 40)
    notebook = _notebook(cells=[sandbox_agent.CellContext(
        id="c1", source="1/0", status="error", error="x" * 500 + "ZeroDivisionError: boom")])
    prompt = sandbox_agent.build_assist_prompt("fix", notebook, [])
    assert "ZeroDivisionError: boom" in prompt


def test_prompt_caps_the_file_listing(monkeypatch):
    monkeypatch.setattr(sandbox_agent.config, "SANDBOX_AGENT_FILES", 2)
    files = [{"path": f"s3://b/f{i}.parquet", "format": "parquet"} for i in range(10)]
    prompt = sandbox_agent.build_assist_prompt("go", _notebook(files=files), [])
    assert "and 8 more" in prompt
    assert "f5.parquet" not in prompt


def test_prompt_trims_history_to_the_configured_window(monkeypatch):
    monkeypatch.setattr(sandbox_agent.config, "SANDBOX_AGENT_HISTORY_TURNS", 2)
    history = [sandbox_agent.AgentTurn(request=f"q{i}", reply=f"a{i}") for i in range(5)]
    prompt = sandbox_agent.build_assist_prompt("go", _notebook(), history)
    assert "q4" in prompt and "q3" in prompt
    assert "q2" not in prompt


def test_target_enum_is_built_from_the_live_notebook():
    tools = sandbox_agent._tools_for_notebook(_notebook())
    write = next(t for t in tools if t["name"] == "write_cells")
    target = write["input_schema"]["properties"]["cells"]["items"]["properties"]["target"]
    assert target["enum"] == ["c1", sandbox_agent.NEW_CELL]
    # the shared _ASSIST_TOOLS template is never mutated in place
    shared = sandbox_agent._ASSIST_TOOLS[0]["input_schema"]["properties"]["cells"]["items"]
    assert "enum" not in shared["properties"]["target"]


def test_empty_notebook_leaves_the_target_unconstrained():
    assert sandbox_agent._tools_for_notebook(_notebook(cells=[])) is sandbox_agent._ASSIST_TOOLS


def test_lineage_prompt_names_declared_sources_and_output_columns():
    prompt = sandbox_agent.build_lineage_prompt(sandbox_agent.LineageContext(
        pipeline_name="silver_orders",
        script='output = sources["orders"].head()',
        sources=[{"name": "orders", "format": "parquet", "path": "s3://b/o/*.parquet"}],
        output_columns=[{"name": "order_id", "dtype": "Int64"}],
    ))
    assert "orders (parquet) <- s3://b/o/*.parquet" in prompt
    assert "order_id Int64" in prompt
    assert 'output = sources["orders"].head()' in prompt


def test_lineage_prompt_says_when_there_was_no_run():
    prompt = sandbox_agent.build_lineage_prompt(sandbox_agent.LineageContext(
        pipeline_name="p", script="output = 1", sources=[], output_columns=[]))
    assert "has not been run" in prompt


def test_system_prompt_is_sent_as_a_cached_block():
    """The doctrine block is long, static and resent every turn — the cache
    breakpoint is this feature's main cost lever, so it's asserted, not
    assumed."""
    blocks = sandbox_agent._system_blocks("hello")
    assert blocks == [{"type": "text", "text": "hello",
                       "cache_control": {"type": "ephemeral"}}]


@pytest.mark.parametrize("phrase", ["read(", "collect", "map_elements", "group_by"])
def test_system_prompt_covers_the_runtime_and_the_perf_rules(phrase):
    assert phrase in sandbox_agent._ASSIST_SYSTEM_PROMPT


def test_system_prompt_forbids_test_writing():
    """The explicit cost/latency decision: the notebook is the feedback
    channel, so the agent must not spend tokens on tests or scaffolding."""
    text = sandbox_agent._ASSIST_SYSTEM_PROMPT.lower()
    assert "no tests" in text
    assert "benchmark" in text
