"""POST /api/conversations* — conversational analytics HTTP surface
(specs/012-conversational-analytics/, US1 T012-T014, US4 T023, US3 T027,
US2 T032, Polish T036). The real AnthropicTranslator is swapped for a
FakeTranslator on app.api.chat._translator for the duration of each test —
no network calls, deterministic scripted decisions."""
from __future__ import annotations

import pytest

from app.api import chat as chat_api
from app.llm import RawToolCall

from .test_nlq import FakeTranslator


@pytest.fixture
def fake_translator(monkeypatch):
    translator = FakeTranslator([])
    monkeypatch.setattr(chat_api, "_translator", translator)
    return translator


@pytest.fixture(autouse=True)
def llm_enabled(monkeypatch):
    monkeypatch.setattr(chat_api.config, "LLM_ENABLED", True)


def _propose_sales_by_category():
    return RawToolCall("propose_query", {
        "model": "sales", "dimensions": ["category"], "measures": ["revenue"],
        "filters": [], "sort": None, "limit": None,
    })


def test_ask_unambiguous_question_matches_direct_query(viewer_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    conv = viewer_client.post("/api/conversations", json={}).json()

    res = viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "revenue by category"})
    assert res.status_code == 200
    body = res.json()
    assert body["response"]["outcome"] == "answered"
    assert body["response"]["resolved_query"]["model"] == "sales"

    direct = viewer_client.post("/api/query", json={
        "model": "sales", "dimensions": ["category"], "measures": ["revenue"]}).json()
    assert body["response"]["result"]["rows"] == direct["rows"]
    assert body["response"]["result"]["row_count"] == direct["row_count"]


def test_ask_same_question_same_result_across_roles(viewer_client, admin_client, monkeypatch):
    for client in (viewer_client, admin_client):
        translator = FakeTranslator([_propose_sales_by_category()])
        monkeypatch.setattr(chat_api, "_translator", translator)
        conv = client.post("/api/conversations", json={}).json()
        res = client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "revenue by category"})
        assert res.json()["response"]["outcome"] == "answered"


def test_ask_disabled_without_llm_key(viewer_client, monkeypatch):
    monkeypatch.setattr(chat_api.config, "LLM_ENABLED", False)
    assert viewer_client.get("/api/conversations").status_code == 503
    assert viewer_client.post("/api/conversations", json={}).status_code == 503


def test_decline_never_executes_a_query(viewer_client, fake_translator, monkeypatch):
    called = {"hit": False}
    real_run_query = chat_api.engine.run_query

    def spy(*args, **kwargs):
        called["hit"] = True
        return real_run_query(*args, **kwargs)

    monkeypatch.setattr(chat_api.engine, "run_query", spy)
    fake_translator.responses.append(RawToolCall("decline", {"reason_text": "not in the semantic layer"}))

    conv = viewer_client.post("/api/conversations", json={}).json()
    res = viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "raw sql please"})
    body = res.json()
    assert body["response"]["outcome"] == "declined"
    assert body["response"]["resolved_query"] is None
    assert body["response"]["result"] is None
    assert called["hit"] is False


def test_declined_and_answered_empty_are_distinguishable(viewer_client, fake_translator):
    fake_translator.responses.append(RawToolCall("propose_query", {
        "model": "sales", "dimensions": ["category"], "measures": ["revenue"],
        "filters": [{"field": "category", "op": "eq", "value": "definitely-not-a-real-category"}],
    }))
    conv = viewer_client.post("/api/conversations", json={}).json()
    res = viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "revenue for nonsense category"})
    body = res.json()
    assert body["response"]["outcome"] == "answered_empty"
    assert body["response"]["outcome"] != "declined"


def test_clarification_then_answer(viewer_client, fake_translator):
    fake_translator.responses.append(RawToolCall("ask_clarification", {
        "question_text": "did you mean revenue or cost?", "candidates": ["revenue", "cost"],
    }))
    conv = viewer_client.post("/api/conversations", json={}).json()
    first = viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "how much did we make"})
    assert first.json()["response"]["outcome"] == "clarification"

    fake_translator.responses.append(_propose_sales_by_category())
    second = viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "revenue"})
    assert second.json()["response"]["outcome"] == "answered"

    # the clarification exchange must not be replayed as a re-ask
    assert len(fake_translator.calls) == 2


def test_follow_up_passes_prior_context_to_translator(viewer_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    conv = viewer_client.post("/api/conversations", json={}).json()
    viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "revenue by category"})

    fake_translator.responses.append(RawToolCall("propose_query", {
        "model": "sales", "dimensions": ["order_date"], "measures": ["revenue"],
    }))
    viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "now by date instead"})

    _, _, prior_context = fake_translator.calls[-1]
    assert len(prior_context) == 1
    assert prior_context[0].model == "sales"
    assert prior_context[0].measures == ["revenue"]


def test_conversation_persists_and_is_owner_scoped(viewer_client, author_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    conv = viewer_client.post("/api/conversations", json={}).json()
    viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "revenue by category"})

    reread = viewer_client.get(f"/api/conversations/{conv['id']}").json()
    assert len(reread["messages"]) == 2
    assert reread["title"] == "revenue by category"

    assert conv["id"] not in [c["id"] for c in author_client.get("/api/conversations").json()]
    assert author_client.get(f"/api/conversations/{conv['id']}").status_code == 404


def test_conversation_scope_validation(viewer_client):
    assert viewer_client.post("/api/conversations", json={"model_scope": ["not_a_model"]}).status_code == 400
    ok = viewer_client.post("/api/conversations", json={"model_scope": ["sales"]})
    assert ok.status_code == 201
    assert ok.json()["model_scope"] == ["sales"]


def test_conversation_llm_model_validation_and_roundtrip(viewer_client):
    bad = viewer_client.post("/api/conversations", json={"llm_model": "not-a-real-model"})
    assert bad.status_code == 400

    ok = viewer_client.post("/api/conversations", json={"llm_model": "claude-opus-4-8"})
    assert ok.status_code == 201
    assert ok.json()["llm_model"] == "claude-opus-4-8"

    conv_id = ok.json()["id"]
    patched = viewer_client.patch(f"/api/conversations/{conv_id}", json={"llm_model": "claude-haiku-4-5-20251001"})
    assert patched.status_code == 200
    assert patched.json()["llm_model"] == "claude-haiku-4-5-20251001"

    bad_patch = viewer_client.patch(f"/api/conversations/{conv_id}", json={"llm_model": "nope"})
    assert bad_patch.status_code == 400


def test_ask_uses_a_dedicated_translator_for_a_non_default_model(viewer_client, monkeypatch):
    from app import config as app_config

    calls = []
    made = []

    class Spy(FakeTranslator):
        def translate(self, *args, **kwargs):
            calls.append(True)
            return super().translate(*args, **kwargs)

    def fake_make(model):
        t = Spy([_propose_sales_by_category()])
        made.append(model)
        return t

    monkeypatch.setattr(chat_api, "AnthropicTranslator", lambda model=None: fake_make(model))
    conv = viewer_client.post("/api/conversations", json={"llm_model": "claude-opus-4-8"}).json()
    res = viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "revenue by category"})
    assert res.json()["response"]["outcome"] == "answered"
    assert made == ["claude-opus-4-8"]


def test_delete_conversation(viewer_client):
    conv = viewer_client.post("/api/conversations", json={}).json()
    assert viewer_client.delete(f"/api/conversations/{conv['id']}").status_code == 204
    assert viewer_client.get(f"/api/conversations/{conv['id']}").status_code == 404


def test_ask_show_last_query_returns_prior_resolved_query_without_reasking(viewer_client, fake_translator):
    """Regression test for the reported bug: asking to see the query used to
    be routed through the same ambiguous NL translator as any other question
    and could come back as a nonsense decline. show_last_query answers it
    deterministically from the conversation's own history instead."""
    fake_translator.responses.append(_propose_sales_by_category())
    conv = viewer_client.post("/api/conversations", json={}).json()
    viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "revenue by category"})

    fake_translator.responses.append(RawToolCall("show_last_query", {}))
    res = viewer_client.post(f"/api/conversations/{conv['id']}/ask",
                              json={"question": "can you return the query that you tried to me?"})
    body = res.json()
    assert body["response"]["outcome"] == "query_shown"
    assert body["response"]["resolved_query"]["model"] == "sales"
    assert body["response"]["resolved_query"]["measures"] == ["revenue"]
    assert body["response"]["result"] is None  # no new query is executed


def test_ask_show_last_query_with_no_prior_turns_declines(viewer_client, fake_translator):
    fake_translator.responses.append(RawToolCall("show_last_query", {}))
    conv = viewer_client.post("/api/conversations", json={}).json()
    res = viewer_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "show me the query"})
    assert res.json()["response"]["outcome"] == "declined"


def test_ask_invalid_filter_op_declines_instead_of_raw_engine_error(viewer_client, fake_translator):
    """Before the fix, an op outside engine.FILTER_OPS (e.g. '=') reached
    engine.run_query and surfaced as outcome:"error" with a raw QueryError
    message. It must now be a clean "declined" instead."""
    fake_translator.responses.append(RawToolCall("propose_query", {
        "model": "sales", "dimensions": [], "measures": ["revenue"],
        "filters": [{"field": "category", "op": "=", "value": "Widgets"}],
    }))
    conv = viewer_client.post("/api/conversations", json={}).json()
    res = viewer_client.post(f"/api/conversations/{conv['id']}/ask",
                              json={"question": "revenue where category = Widgets"})
    assert res.json()["response"]["outcome"] == "declined"
