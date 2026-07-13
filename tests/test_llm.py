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
