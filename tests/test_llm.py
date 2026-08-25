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

from app import config, engine, llm, llmclient
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


# ── relative dates (the bug this fixes: the prompt described "a keyword …
# or an offset like 'today-90d'" as two separate things, so the model
# composed them into 'start_of_year-1y' — the only sensible spelling of
# "last year" — and the engine, which only accepted offsets on "today",
# crashed the request on it) ─────────────────────────────────────────────

def test_relative_date_syntax_is_the_engine_s_own_statement_of_it():
    """One definition, quoted verbatim by schema and prompt alike — not a
    second hand-written copy that can drift from what the engine parses."""
    assert llm._RELATIVE_DATE_SYNTAX is engine.RELATIVE_DATE_SYNTAX
    value_schema = _tool("propose_query")["input_schema"]["properties"]["filters"]["items"]["properties"]["value"]
    assert engine.RELATIVE_DATE_SYNTAX in value_schema["description"]
    assert engine.RELATIVE_DATE_SYNTAX in llm._SYSTEM_PROMPT


def test_relative_date_syntax_declares_every_keyword_and_unit():
    for keyword in engine.RELATIVE_DATE_KEYWORDS:
        assert keyword in engine.RELATIVE_DATE_SYNTAX
    for unit, name in engine.RELATIVE_OFFSET_UNITS.items():
        assert f"{unit} ({name})" in engine.RELATIVE_DATE_SYNTAX


def test_relative_date_syntax_rules_out_what_the_model_kept_inventing():
    syntax = engine.RELATIVE_DATE_SYNTAX
    for invented in ("last_year", "last_month", "ytd", "today-1y+2d"):
        assert invented in syntax, "the prompt must name the forms that don't parse"
        assert engine.resolve_relative_date(invented) is None, "…and they must really not parse"


def test_system_prompt_shows_worked_relative_date_filters_that_all_resolve():
    """Every example the prompt tells the model to copy has to be a token
    the engine actually accepts — a wrong example is how the grain field's
    '1qtr' bug happened."""
    prompt = llm._SYSTEM_PROMPT
    examples = ["start_of_year", "start_of_year-1y", "end_of_year-1y", "start_of_month-1mo",
                "end_of_month-1mo", "start_of_quarter-1q", "end_of_quarter-1q", "today-90d", "today-1y"]
    for token in examples:
        assert f"'{token}'" in prompt
        assert engine.resolve_relative_date(token) is not None
    # and that a whole period is two filters, which is what "last year" needs
    assert "gte 'start_of_year-1y' and lte 'end_of_year-1y'" in prompt


def test_show_last_query_tool_is_declared_with_no_required_args():
    tool = _tool("show_last_query")
    assert tool["input_schema"].get("required", []) == []


# ── follow-ups are propose_query calls, not show_last_query (the bug this
# fixes: "can you break this down by quarter" after an answered turn came
# back as the *previous* query shown verbatim — the prompt shipped prior
# turns with no instruction about what to do with them, and
# show_last_query's description claimed anything referring to "a previous
# answer in this conversation") ──────────────────────────────────────────

def test_show_last_query_description_excludes_refinement_follow_ups():
    description = _tool("show_last_query")["description"]
    assert "break this down by quarter" in description
    assert "propose_query" in description


def test_system_prompt_tells_the_model_to_build_follow_ups_from_the_last_turn():
    prompt = llm._SYSTEM_PROMPT
    assert "Follow-ups" in prompt
    assert "break this down by quarter" in prompt
    # the two halves that make a follow-up resolvable at all: which turn it
    # attaches to, and that the unmentioned parts must be repeated
    assert "most recent prior turn" in prompt
    assert "complete on its own" in prompt


def test_system_prompt_reserves_show_last_query_for_seeing_the_query_itself():
    assert "show_last_query ONLY when the user" in llm._SYSTEM_PROMPT


def test_prior_context_text_marks_the_most_recent_turn():
    """Which turn a follow-up adjusts can't be left implicit — the LLM sees
    a flat list, not a conversation."""
    text = llm._prior_context_text([
        llm.PriorTurn(question_text="revenue by category", model="sales",
                      dimensions=["category"], measures=["revenue"], filters=[]),
        llm.PriorTurn(question_text="top 5 products", model="sales",
                      dimensions=["product"], measures=["revenue"], filters=[], limit=5),
    ])
    lines = text.splitlines()
    assert "most recent" not in lines[0]
    assert "most recent" in lines[1]


def test_prior_context_text_includes_sort_and_limit():
    """Carried in PriorTurn but never rendered, so a follow-up to "top 5
    products" had nothing to tell it to keep the limit."""
    text = llm._prior_context_text([
        llm.PriorTurn(question_text="top 5 products", model="sales", dimensions=["product"],
                      measures=["revenue"], filters=[], sort={"by": "revenue", "desc": True}, limit=5),
    ])
    assert "limit=5" in text
    assert "sort={'by': 'revenue', 'desc': True}" in text


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
                       "expr": "SUM(unit_price * quantity)"}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "computed as: SUM(unit_price * quantity)" in text


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


# ── inline measures (chat-authored running_total()/lag()) ─────────────────

def test_propose_query_tool_declares_inline_measures():
    schema = _tool("propose_query")["input_schema"]["properties"]["inline_measures"]
    item_props = schema["items"]["properties"]
    assert set(schema["items"]["required"]) == {"name", "expr"}
    assert {"name", "expr", "label", "format"} <= set(item_props)


def test_system_prompt_explains_inline_measures():
    assert "running_total" in llm._SYSTEM_PROMPT
    assert "lag(measure" in llm._SYSTEM_PROMPT
    assert "inline_measures" in llm._SYSTEM_PROMPT


# ── categorical sample values ("common sense" case/format matching) ───────

def test_catalog_text_includes_sample_values_when_present():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="",
            dimensions=[{"name": "category", "label": "Category", "type": "categorical",
                         "description": "", "synonyms": [], "sample_values": ["Cyberware", "Netrunning"]}],
            measures=[],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "sample values: Cyberware, Netrunning" in text


def test_catalog_text_omits_sample_values_marker_when_absent():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="",
            dimensions=[{"name": "order_date", "label": "Order Date", "type": "time",
                         "description": "", "synonyms": []}],
            measures=[],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "sample values" not in text


def test_system_prompt_explains_sample_values():
    assert "sample values" in llm._SYSTEM_PROMPT


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


# ── extended thinking is only sent to models that support it (the bug this
# fixes: Haiku doesn't, and got adaptive thinking unconditionally, 400ing
# with "adaptive thinking is not supported on this model") ────────────────

def test_thinking_kwargs_enabled_for_adaptive_capable_models():
    client = llmclient.AnthropicClient()
    for model in ("claude-opus-4-8", "claude-sonnet-5"):
        request = llmclient.ChatRequest(model=model, max_tokens=1, system="", tools=[], prompt="", thinking=True)
        assert client._thinking_kwargs(request) == {"thinking": {"type": "adaptive", "display": "summarized"}}


def test_thinking_kwargs_omitted_for_haiku():
    request = llmclient.ChatRequest(
        model="claude-haiku-4-5-20251001", max_tokens=1, system="", tools=[], prompt="", thinking=True)
    assert llmclient.AnthropicClient()._thinking_kwargs(request) == {}


def test_thinking_kwargs_omitted_when_the_caller_did_not_ask():
    """translate() (non-streamed) has nothing to display thinking in, so it
    never asks for it — even on a model that supports it."""
    request = llmclient.ChatRequest(
        model="claude-sonnet-5", max_tokens=1, system="", tools=[], prompt="", thinking=False)
    assert llmclient.AnthropicClient()._thinking_kwargs(request) == {}


def test_thinking_models_are_a_subset_of_llm_model_choices():
    """Guards against a typo drifting the two lists apart — every entry here
    must be one of the actually-selectable models (config.LLM_MODEL_CHOICES'
    own comment asks for the same discipline)."""
    assert config.LLM_THINKING_MODELS <= set(config.LLM_MODEL_CHOICES)


def test_translate_streaming_wires_thinking_and_model_enum_into_the_real_call(monkeypatch):
    """Integration-level guard, not just the pure helpers in isolation: proves
    LLMTranslator.translate_streaming() actually passes its thinking request
    and _tools_for_catalog() all the way through to messages.stream() —
    thinking omitted for a non-adaptive model (haiku; the exact reported 400),
    and propose_query's `model` constrained to the catalog's own names."""
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
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    catalog = [llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[])]

    haiku = llm.LLMTranslator(api_key="x", model="claude-haiku-4-5-20251001")
    list(haiku.translate_streaming("q", catalog, []))
    assert "thinking" not in captured
    propose = next(t for t in captured["tools"] if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {"type": "string", "enum": ["sales"]}

    captured.clear()
    sonnet = llm.LLMTranslator(api_key="x", model="claude-sonnet-5")
    list(sonnet.translate_streaming("q", catalog, []))
    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}

    # …and the UI's THINKING toggle, off, keeps it off on the very same model
    captured.clear()
    list(sonnet.translate_streaming("q", catalog, [], thinking=False))
    assert "thinking" not in captured


def test_translate_streaming_thinking_flag_beats_the_server_default(monkeypatch):
    """The toggle is the last word in both directions: an explicit False on a
    deployment that defaults thinking on, and an explicit True on one that
    defaults it off. None (the caller with no opinion) follows the default."""
    seen = []

    class RecordingClient:
        def stream(self, req):
            seen.append(req.thinking)
            yield llmclient.ClientEvent(kind="done", final=llmclient.ToolCall(name="decline", args={}))

    translator = llm.LLMTranslator(api_key="x", model="claude-sonnet-5", client=RecordingClient())
    catalog = [llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[])]

    monkeypatch.setattr(config, "LLM_THINKING_DEFAULT", True)
    list(translator.translate_streaming("q", catalog, []))
    list(translator.translate_streaming("q", catalog, [], thinking=False))
    monkeypatch.setattr(config, "LLM_THINKING_DEFAULT", False)
    list(translator.translate_streaming("q", catalog, []))
    list(translator.translate_streaming("q", catalog, [], thinking=True))
    assert seen == [True, False, False, True]


def test_translate_streaming_reaches_an_openai_endpoint_unchanged(monkeypatch):
    """The same translator, the same prompt and tools, against an OpenAI-format
    endpoint — the point of the whole abstraction. Asserts the wire translation
    the OpenAI side needs: function-shaped tools carrying the *same* JSON
    Schema (model enum included), forced tool choice, and a system message
    rather than a system parameter."""
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://gateway.internal/v1")
    monkeypatch.setattr(config, "LLM_PROVIDER", "auto")
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return iter(())

    _install_fake_openai(monkeypatch, fake_create)
    catalog = [llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[])]

    translator = llm.LLMTranslator(api_key="x", model="gpt-4o")
    try:
        list(translator.translate_streaming("q", catalog, []))
    except llm.TranslatorError:
        pass    # the empty stream ends with "model did not call any tool"

    assert captured["tool_choice"] == "required"
    assert captured["max_tokens"] == 1024
    assert [m["role"] for m in captured["messages"]] == ["system", "user"]
    assert captured["messages"][0]["content"] == llm._SYSTEM_PROMPT
    propose = next(t for t in captured["tools"] if t["function"]["name"] == "propose_query")
    assert propose["type"] == "function"
    assert propose["function"]["parameters"]["properties"]["model"] == {"type": "string", "enum": ["sales"]}
    # Anthropic-only hints must not leak through as unknown parameters
    assert "eager_input_streaming" not in propose["function"]


def _install_fake_openai(monkeypatch, create):
    """Stand-in for the openai SDK's client, for tests that only care about
    what reaches chat.completions.create()."""
    import openai

    class FakeCompletions:
        def create(self, **kwargs):
            kwargs.pop("stream", None)
            return create(**kwargs)

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
