"""app.nlq: the translator-decision re-validation core, exercised with zero
network calls via FakeTranslator (specs/012-conversational-analytics/,
Foundational phase T009-T011, US1 T010-T011, US4 T021-T022, US3 T026,
US2 T030-T031)."""
from __future__ import annotations

from app import nlq
from app.auth import User
from app.llm import PriorTurn, RawToolCall

VIEWER = User(id=1, username="viewer", display_name="Viewer", role="viewer")


class FakeTranslator:
    """Scripted Translator: returns queued RawToolCall/Exception values in
    order, and records every call for assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def translate(self, question, catalog, prior_context):
        self.calls.append((question, catalog, prior_context))
        assert self.responses, "FakeTranslator ran out of scripted responses"
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


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


def test_resolve_propose_query_unambiguous(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": ["category"], "measures": ["revenue"],
            "filters": [], "sort": None, "limit": None,
        }),
    ])
    decision = nlq.resolve("revenue by category", _catalog(models), [], VIEWER, models, translator)
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
    decision = nlq.resolve("bogus", _catalog(models), [], VIEWER, models, translator)
    assert isinstance(decision, nlq.Decline)


def test_resolve_rejects_unknown_measure(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {"model": "sales", "dimensions": [], "measures": ["not_a_measure"]}),
    ])
    decision = nlq.resolve("bogus", _catalog(models), [], VIEWER, models, translator)
    assert isinstance(decision, nlq.Decline)


def test_resolve_rejects_unknown_model(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {"model": "not_a_model", "dimensions": [], "measures": ["revenue"]}),
    ])
    decision = nlq.resolve("bogus", _catalog(models), [], VIEWER, models, translator)
    assert isinstance(decision, nlq.Decline)


def test_resolve_rejects_model_outside_scope(models):
    translator = FakeTranslator([
        RawToolCall("propose_query", {"model": "sales", "dimensions": [], "measures": ["revenue"]}),
    ])
    scoped_catalog = nlq.build_catalog(models, ["logistics"])
    decision = nlq.resolve("bogus", scoped_catalog, [], VIEWER, models, translator, scope=["logistics"])
    assert isinstance(decision, nlq.Decline)


def test_resolve_decline_passes_through(models):
    translator = FakeTranslator([RawToolCall("decline", {"reason_text": "no such metric here"})])
    decision = nlq.resolve("what is the meaning of life", _catalog(models), [], VIEWER, models, translator)
    assert isinstance(decision, nlq.Decline)
    assert decision.reason_text == "no such metric here"


def test_resolve_clarification_filters_invented_candidates(models):
    translator = FakeTranslator([
        RawToolCall("ask_clarification", {
            "question_text": "which model did you mean?",
            "candidates": ["sales", "an_invented_model_name"],
        }),
    ])
    decision = nlq.resolve("revenue", _catalog(models), [], VIEWER, models, translator)
    assert isinstance(decision, nlq.AskClarification)
    assert decision.candidates == ["sales"]


def test_resolve_clarification_with_no_real_candidates_declines(models):
    translator = FakeTranslator([
        RawToolCall("ask_clarification", {"question_text": "huh?", "candidates": ["invented"]}),
    ])
    decision = nlq.resolve("revenue", _catalog(models), [], VIEWER, models, translator)
    assert isinstance(decision, nlq.Decline)


def test_resolve_follow_up_reuses_and_revalidates_prior_context(models):
    prior = [PriorTurn(question_text="revenue by category", model="sales",
                        dimensions=["category"], measures=["revenue"], filters=[])]
    translator = FakeTranslator([
        RawToolCall("propose_query", {
            "model": "sales", "dimensions": ["order_date"], "measures": ["revenue"],
        }),
    ])
    decision = nlq.resolve("now break it down by date instead", _catalog(models), prior, VIEWER, models, translator)
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
    decision = nlq.resolve("and last quarter?", _catalog(models), prior, VIEWER, models, translator)
    assert isinstance(decision, nlq.Decline)
