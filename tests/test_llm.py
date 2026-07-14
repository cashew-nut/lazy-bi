"""app.llm: the tool schema/system prompt the Translator sends to the LLM.

Regression guards for the bug this module was patched for: filters[].op had
no declared vocabulary at all (the model guessed '=' instead of 'eq', which
engine.run_query then rejected with an unexplained "unsupported filter op"
error) and the grain field's only guidance was a wrong example ("1qtr" isn't
a real grain). These tests assert the schema/prompt stay derived from
engine.FILTER_OPS / semantic.TIME_GRAINS, not a second hand-written copy that
can drift out of sync with them again.
"""
from __future__ import annotations

from app import engine, llm
from app.semantic import TIME_GRAINS


def _tool(name):
    return next(t for t in llm._TOOLS if t["name"] == name)


def test_filter_op_enum_matches_engine_filter_ops():
    op_schema = _tool("propose_query")["input_schema"]["properties"]["filters"]["items"]["properties"]["op"]
    assert set(op_schema["enum"]) == engine.FILTER_OPS
    # the exact bug reported: '=' must never be an accepted op
    assert "=" not in op_schema["enum"]


def test_grain_enum_matches_time_grains_not_the_old_wrong_example():
    dim_schema = _tool("propose_query")["input_schema"]["properties"]["dimensions"]["items"]["oneOf"][1]
    grain_schema = dim_schema["properties"]["grain"]
    assert set(grain_schema["enum"]) == set(TIME_GRAINS)
    assert "1qtr" not in grain_schema["enum"]
    assert "1q" in grain_schema["enum"]


def test_system_prompt_declares_filter_ops_and_grains():
    for op in engine.FILTER_OPS:
        assert op in llm._SYSTEM_PROMPT
    for grain in TIME_GRAINS:
        assert grain in llm._SYSTEM_PROMPT


def test_show_last_query_tool_is_declared_with_no_required_args():
    tool = _tool("show_last_query")
    assert tool["input_schema"].get("required", []) == []


def test_all_four_tool_kinds_present():
    assert {t["name"] for t in llm._TOOLS} == {
        "propose_query", "ask_clarification", "show_last_query", "decline",
    }


def test_all_tools_have_eager_input_streaming_enabled():
    """Lets AnthropicTranslator.translate_streaming() show a tool's args
    taking shape live (input_json events), instead of only ever seeing the
    whole JSON blob appear at once at the end."""
    assert all(t.get("eager_input_streaming") is True for t in llm._TOOLS)


# ── measure formula ground truth (a name/description alone isn't always
# enough to disambiguate measures — see nlq._measure_catalog_entry) ───────

def test_catalog_text_includes_measure_formula_when_present():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="", dimensions=[],
            measures=[{"name": "revenue", "label": "Revenue", "description": "",
                       "expr": "sum(unit_price * quantity)"}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "computed as: sum(unit_price * quantity)" in text


def test_catalog_text_omits_formula_marker_when_absent():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="", dimensions=[],
            measures=[{"name": "orders", "label": "Orders", "description": ""}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "computed as" not in text


def test_system_prompt_explains_the_formula_field():
    assert "computed as" in llm._SYSTEM_PROMPT


# ── synonyms (alternate business vocabulary) ───────────────────────────────

def test_catalog_text_includes_synonyms_for_dimensions_and_measures():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="",
            dimensions=[{"name": "order_date", "label": "Order Date", "type": "time",
                         "description": "", "synonyms": ["date", "purchase date"]}],
            measures=[{"name": "revenue", "label": "Revenue", "description": "",
                       "synonyms": ["sales", "turnover"]}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "also called: date, purchase date" in text
    assert "also called: sales, turnover" in text


def test_catalog_text_omits_synonyms_marker_when_absent():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="",
            dimensions=[{"name": "category", "label": "Category", "type": "categorical",
                         "description": "", "synonyms": []}],
            measures=[{"name": "orders", "label": "Orders", "description": "", "synonyms": []}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "also called" not in text


def test_system_prompt_explains_synonyms_and_requires_declared_name():
    assert "also called" in llm._SYSTEM_PROMPT
    assert "never a synonym string" in llm._SYSTEM_PROMPT
