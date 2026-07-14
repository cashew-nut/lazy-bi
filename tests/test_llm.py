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

from app import config, engine, llm
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


# ── propose_query's `model` field, constrained to the live catalog (the bug
# this fixes: with no declared vocabulary at all, the LLM could omit `model`
# — most visible under a single-model scope, where nlq.py's re-validation
# declined with the confusing "'None' is not a model this conversation can
# query.") ───────────────────────────────────────────────────────────────

def test_tools_for_catalog_constrains_model_to_catalog_names():
    catalog = [
        llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[]),
        llm.ModelCatalogEntry(name="logistics", label="Logistics", description="", dimensions=[], measures=[]),
    ]
    tools = llm._tools_for_catalog(catalog)
    propose = next(t for t in tools if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {
        "type": "string", "enum": ["sales", "logistics"],
    }
    # required/other tools are untouched
    assert propose["input_schema"]["required"] == ["model", "dimensions", "measures"]
    assert {t["name"] for t in tools} == {t["name"] for t in llm._TOOLS}


def test_tools_for_catalog_leaves_model_unconstrained_when_catalog_is_empty():
    """No models available to this conversation at all — nothing to
    constrain `model` to, so it stays the plain string it always was."""
    tools = llm._tools_for_catalog([])
    assert tools is llm._TOOLS
    propose = next(t for t in tools if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {"type": "string"}


def test_tools_for_catalog_does_not_mutate_the_shared_tools_list():
    catalog = [llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[])]
    llm._tools_for_catalog(catalog)
    propose = next(t for t in llm._TOOLS if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {"type": "string"}


# ── adaptive thinking is only sent to models that support it (the bug this
# fixes: Haiku doesn't, and got adaptive thinking unconditionally, 400ing
# with "adaptive thinking is not supported on this model") ────────────────

def test_thinking_kwargs_enabled_for_adaptive_capable_models():
    for model in ("claude-opus-4-8", "claude-sonnet-5"):
        assert llm._thinking_kwargs(model) == {"thinking": {"type": "adaptive", "display": "summarized"}}


def test_thinking_kwargs_omitted_for_haiku():
    assert llm._thinking_kwargs("claude-haiku-4-5-20251001") == {}


def test_adaptive_thinking_models_are_a_subset_of_llm_model_choices():
    """Guards against a typo drifting the two lists apart — every entry here
    must be one of the actually-selectable models (config.LLM_MODEL_CHOICES'
    own comment asks for the same discipline)."""
    assert llm._ADAPTIVE_THINKING_MODELS <= set(config.LLM_MODEL_CHOICES)


def test_translate_streaming_wires_thinking_and_model_enum_into_the_real_call(monkeypatch):
    """Integration-level guard, not just the pure helpers in isolation: proves
    AnthropicTranslator.translate_streaming() actually passes _thinking_kwargs()
    and _tools_for_catalog() through to messages.stream() — thinking omitted
    for a non-adaptive model (haiku; the exact reported 400), and
    propose_query's `model` constrained to the catalog's own names."""
    import anthropic

    captured = {}

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return iter(())

        def get_final_message(self):
            block = type("Block", (), {"type": "tool_use", "name": "decline", "input": {"reason_text": "x"}})()
            return type("Message", (), {"content": [block]})()

    class FakeMessages:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    catalog = [llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[])]

    haiku = llm.AnthropicTranslator(api_key="x", model="claude-haiku-4-5-20251001")
    list(haiku.translate_streaming("q", catalog, []))
    assert "thinking" not in captured
    propose = next(t for t in captured["tools"] if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {"type": "string", "enum": ["sales"]}

    captured.clear()
    sonnet = llm.AnthropicTranslator(api_key="x", model="claude-sonnet-5")
    list(sonnet.translate_streaming("q", catalog, []))
    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
