"""app.nlq: the translator-decision re-validation core, exercised with zero
network calls via FakeTranslator (specs/012-conversational-analytics/,
Foundational phase T009-T011, US1 T010-T011, US4 T021-T022, US3 T026,
US2 T030-T031)."""
from __future__ import annotations

import pytest

from app import nlq
from app.llm import PriorTurn, RawToolCall, StreamEvent, TranslatorError


class FakeTranslator:
    """Scripted Translator: returns queued RawToolCall/Exception values in
    order, and records every call for assertions. translate_streaming()
    replays the same queue — each response may be preceded by a scripted
    list of display-only StreamEvents (`stream_events`, one list per queued
    response; omitted/exhausted just means "no live progress events, go
    straight to the final decision") so resolve_streaming() can be exercised
    with zero network calls too."""

    def __init__(self, responses, stream_events=None):
        self.responses = list(responses)
        self.stream_events = list(stream_events or [])
        self.calls = []

    def translate(self, question, catalog, prior_context):
        self.calls.append((question, catalog, prior_context))
        assert self.responses, "FakeTranslator ran out of scripted responses"
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def translate_streaming(self, question, catalog, prior_context):
        self.calls.append((question, catalog, prior_context))
        assert self.responses, "FakeTranslator ran out of scripted responses"
        resp = self.responses.pop(0)
        events = self.stream_events.pop(0) if self.stream_events else []
        yield from events
        if isinstance(resp, Exception):
            raise resp
        yield StreamEvent(kind="done", final=resp)


def _catalog(models):
    return nlq.build_catalog(models, [])


def test_build_catalog_all_models(models):
    catalog = nlq.build_catalog(models, [])
    assert {m.name for m in catalog} == set(models.keys())
    sales = next(m for m in catalog if m.name == "sales")
    assert {d["name"] for d in sales.dimensions} >= {"order_date", "category"}
    assert {m["name"] for m in sales.measures} >= {"revenue", "cost"}


def test_build_catalog_scoped(models):
    catalog = nlq.build_catalog(models, ["sales"])
    assert [m.name for m in catalog] == ["sales"]


# ── measure formulas in the catalog (a name/description alone isn't always
# enough to pick the right measure — see nlq._measure_catalog_entry) ──────

def test_build_catalog_includes_measure_formula_for_plain_measures(models):
    catalog = nlq.build_catalog(models, ["sales"])
    sales = catalog[0]
    revenue = next(m for m in sales.measures if m["name"] == "revenue")
    assert revenue["expr"] == models["sales"].measure("revenue").expr_source
    assert "unit_price" in revenue["expr"]


def test_build_catalog_includes_synonyms(models):
    catalog = nlq.build_catalog(models, ["sales"])
    sales = catalog[0]
    revenue = next(m for m in sales.measures if m["name"] == "revenue")
    assert set(revenue["synonyms"]) == {"sales", "turnover", "income"}
    order_date = next(d for d in sales.dimensions if d["name"] == "order_date")
    assert set(order_date["synonyms"]) == {"date", "purchase date"}
    # a measure/dimension with no declared synonyms still gets the key, as
    # an empty list — a predictable shape for every downstream consumer
    orders = next(m for m in sales.measures if m["name"] == "orders")
    assert orders["synonyms"] == []


def test_build_catalog_omits_formula_for_framed_measures(models):
    """A framed measure's expr_source is a fragment over an intermediary
    frame and is meaningless without that frame's context (see
    semantic.Measure.frame_source) — it must not leak into the catalog on
    its own."""
    catalog = nlq.build_catalog(models, ["clinical_ops_recruitment"])
    recruitment = catalog[0]
    framed = next(m for m in recruitment.measures if m["name"] == "median_months_to_75pct_randomised")
    assert "expr" not in framed
    plain = next(m for m in recruitment.measures if m["name"] == "screened_actual")
    assert "expr" in plain


def test_resolve_propose_query_unambiguous(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": ["category"], "measures": ["revenue"],
            "filters": [], "sort": None, "limit": None,
        }),
    ])
    decision = nlq.resolve("revenue by category", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.ProposeQuery)
    assert decision.model == "sales"
    assert decision.dimensions == ["category"]
    assert decision.measures == ["revenue"]


def test_resolve_rejects_unknown_dimension(models):
    """A propose_query naming a field the model doesn't declare must not be
    trusted — proves re-validation isn't just relaying the LLM (R2)."""
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": ["not_a_real_dimension"], "measures": ["revenue"],
        }),
    ])
    decision = nlq.resolve("bogus", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)


def test_resolve_rejects_unknown_measure(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {"model": "sales", "dimensions": [], "measures": ["not_a_measure"]}),
    ])
    decision = nlq.resolve("bogus", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)


def test_resolve_rejects_unknown_model(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {"model": "not_a_model", "dimensions": [], "measures": ["revenue"]}),
    ])
    decision = nlq.resolve("bogus", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)


def test_resolve_missing_model_declines_with_a_clear_message(models):
    """Regression test for the reported bug: a propose_query call with no
    `model` at all used to decline with the confusing "'None' is not a
    model this conversation can query." (Python's None rendered into the
    f-string) — now a plain, non-technical message instead."""
    translator = FakeTranslator([
        RawToolCall("propose_query", {"dimensions": [], "measures": ["revenue"]}),
    ])
    decision = nlq.resolve("bogus", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)
    assert "None" not in decision.reason_text
    assert decision.reason_text == "the assistant didn't specify which model to use for that."


def test_resolve_rejects_model_outside_scope(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {"model": "sales", "dimensions": [], "measures": ["revenue"]}),
    ])
    scoped_catalog = nlq.build_catalog(models, ["logistics"])
    decision = nlq.resolve("bogus", scoped_catalog, [], models, translator, scope=["logistics"])
    assert isinstance(decision, nlq.Decline)


def test_resolve_decline_passes_through(models):
    translator = FakeTranslator([RawToolCall("decline", {"reason_text": "no such metric here"})])
    decision = nlq.resolve("what is the meaning of life", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)
    assert decision.reason_text == "no such metric here"


def test_resolve_clarification_filters_invented_candidates(models):
    translator = FakeTranslator([
        RawToolCall("ask_clarification", {
            "question_text": "which model did you mean?",
            "candidates": ["sales", "an_invented_model_name"],
        }),
    ])
    decision = nlq.resolve("revenue", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.AskClarification)
    assert decision.candidates == ["sales"]


def test_resolve_clarification_with_no_real_candidates_declines(models):
    translator = FakeTranslator([
        RawToolCall("ask_clarification", {"question_text": "huh?", "candidates": ["invented"]}),
    ])
    decision = nlq.resolve("revenue", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)


def test_resolve_follow_up_reuses_and_revalidates_prior_context(models):
    prior = [PriorTurn(question_text="revenue by category", model="sales",
                        dimensions=["category"], measures=["revenue"], filters=[])]
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": ["order_date"], "measures": ["revenue"],
        }),
    ])
    decision = nlq.resolve("now break it down by date instead", _catalog(models), prior, models, translator)
    assert isinstance(decision, nlq.ProposeQuery)
    assert decision.dimensions == ["order_date"]


def test_resolve_stale_prior_context_model_removed_still_revalidates(models):
    """Even if prior_context references a model, the *current* proposal is
    what gets checked against the live models dict (FR-009) — simulated
    here by a proposal for a model that no longer exists."""
    prior = [PriorTurn(question_text="revenue by category", model="sales",
                        dimensions=["category"], measures=["revenue"], filters=[])]
    translator = FakeTranslator([
        RawToolCall("propose_query", {"model": "a_removed_model", "dimensions": [], "measures": ["revenue"]}),
    ])
    decision = nlq.resolve("and last quarter?", _catalog(models), prior, models, translator)
    assert isinstance(decision, nlq.Decline)


# ── filter op / grain re-validation (defense in depth alongside llm.py's
# schema/prompt fix) — a proposal naming something outside the engine's
# actual allowlist must decline cleanly here, never reach engine.run_query
# and surface as a raw, unexplained QueryError ──────────────────────────

def test_resolve_rejects_invalid_filter_op(models):
    """The exact bug reported: an LLM proposing '=' instead of 'eq' must be
    caught here as a clean Decline, not fall through to engine.run_query."""
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": [], "measures": ["revenue"],
            "filters": [{"field": "category", "op": "=", "value": "Widgets"}],
        }),
    ])
    decision = nlq.resolve("revenue where category = Widgets", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)
    assert "=" in decision.reason_text


def test_resolve_accepts_valid_filter_op(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": [], "measures": ["revenue"],
            "filters": [{"field": "category", "op": "eq", "value": "Widgets"}],
        }),
    ])
    decision = nlq.resolve("revenue for widgets", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.ProposeQuery)


def test_resolve_rejects_invalid_grain(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": [{"name": "order_date", "grain": "1qtr"}],
            "measures": ["revenue"],
        }),
    ])
    decision = nlq.resolve("revenue by quarter", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)
    assert "1qtr" in decision.reason_text


def test_resolve_accepts_valid_grain(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": [{"name": "order_date", "grain": "1q"}],
            "measures": ["revenue"],
        }),
    ])
    decision = nlq.resolve("revenue by quarter", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.ProposeQuery)


# ── show_last_query ──────────────────────────────────────────────────────

def test_resolve_show_last_query_returns_most_recent_prior_turn(models):
    prior = [
        PriorTurn(question_text="revenue by category", model="sales",
                  dimensions=["category"], measures=["revenue"], filters=[]),
        PriorTurn(question_text="now by date instead", model="sales",
                  dimensions=["order_date"], measures=["revenue"], filters=[],
                  sort={"by": "revenue", "desc": True}, limit=50),
    ]
    translator = FakeTranslator([RawToolCall("show_last_query", {})])
    decision = nlq.resolve("can you show me the query you just ran?", _catalog(models), prior, models, translator)
    assert isinstance(decision, nlq.ShowQuery)
    assert decision.model == "sales"
    assert decision.dimensions == ["order_date"]
    assert decision.sort == {"by": "revenue", "desc": True}
    assert decision.limit == 50


def test_resolve_show_last_query_without_prior_context_declines(models):
    translator = FakeTranslator([RawToolCall("show_last_query", {})])
    decision = nlq.resolve("show me the query", _catalog(models), [], models, translator)
    assert isinstance(decision, nlq.Decline)


# ── resolve_streaming (streaming twin of resolve() — same re-validation) ───

def test_resolve_streaming_yields_events_then_a_decision(models):
    translator = FakeTranslator(
        [RawToolCall("propose_query", {
            "model": "sales", "dimensions": ["category"], "measures": ["revenue"],
        })],
        stream_events=[[
            StreamEvent(kind="thinking", text="considering the question"),
            StreamEvent(kind="tool_name", tool_name="propose_query"),
            StreamEvent(kind="tool_input", tool_input={"model": "sales"}),
        ]],
    )
    items = list(nlq.resolve_streaming("revenue by category", _catalog(models), [], models, translator))
    *events, decision = items
    assert [e.kind for e in events] == ["thinking", "tool_name", "tool_input"]
    assert events[0].text == "considering the question"
    assert events[1].tool_name == "propose_query"
    assert events[2].tool_input == {"model": "sales"}
    assert isinstance(decision, nlq.ProposeQuery)
    assert decision.model == "sales"


def test_resolve_streaming_still_revalidates_like_resolve(models):
    """The exact re-validation resolve() does (the reported bug: an invalid
    filter op) must still apply when the same RawToolCall arrives via the
    streaming path — every event but the final one is display-only."""
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": [], "measures": ["revenue"],
            "filters": [{"field": "category", "op": "=", "value": "Widgets"}],
        }),
    ])
    items = list(nlq.resolve_streaming(
        "revenue where category = Widgets", _catalog(models), [], models, translator))
    decision = items[-1]
    assert isinstance(decision, nlq.Decline)
    assert "=" in decision.reason_text


def test_resolve_streaming_with_no_display_events_still_yields_a_decision(models):
    """A translator that scripts no intermediate StreamEvents at all (the
    common case in most of these tests) still ends in exactly one Decision —
    proves the "done" event alone is enough to drive resolve_streaming."""
    translator = FakeTranslator([RawToolCall("decline", {"reason_text": "not in the model"})])
    items = list(nlq.resolve_streaming("bogus", _catalog(models), [], models, translator))
    assert len(items) == 1
    assert isinstance(items[0], nlq.Decline)


def test_resolve_streaming_propagates_translator_error(models):
    class ErrorTranslator:
        def translate(self, *args, **kwargs):
            raise TranslatorError("boom")

        def translate_streaming(self, *args, **kwargs):
            raise TranslatorError("boom")

    with pytest.raises(TranslatorError):
        list(nlq.resolve_streaming("revenue", _catalog(models), [], models, ErrorTranslator()))
