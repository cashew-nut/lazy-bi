"""POST /api/chat/panel/ask/stream — the modelling workspace's ephemeral
right-hand chat panel (app/api/chat.py's _panel_* helpers). Same FakeTranslator
substitution pattern as test_chat_api.py; the whole point under test is that
nothing here is persisted, unlike a saved conversation's ask/stream."""
from __future__ import annotations

import json

import pytest

from app.api import chat as chat_api
from app.llm import RawToolCall, TranslatorError

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


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in text.strip("\n").split("\n\n"):
        if not chunk.strip():
            continue
        event_name, data = "message", ""
        for line in chunk.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data += line[len("data: "):]
        if data:
            events.append((event_name, json.loads(data)))
    return events


def test_panel_ask_answers_and_persists_nothing(viewer_client, fake_translator):
    # session-scoped viewer_client/DB (conftest.py) is shared with every
    # other test module, so "nothing persisted" is checked as a before/after
    # delta rather than assuming an empty list — other tests' conversations
    # may already exist for this same user.
    before = len(viewer_client.get("/api/conversations").json())
    fake_translator.responses.append(_propose_sales_by_category())

    res = viewer_client.post("/api/chat/panel/ask/stream", json={
        "question": "revenue by category", "model_scope": ["sales"],
    })
    assert res.status_code == 200
    events = _parse_sse(res.text)
    kinds = [e for e, _ in events]
    assert "response" in kinds
    response_data = dict(events)["response"]
    assert response_data["response"]["outcome"] == "answered"
    assert response_data["response"]["resolved_query"]["model"] == "sales"
    assert response_data["response"]["id"] is None
    assert response_data["question"]["id"] is None

    # nothing was written to conversation storage
    assert len(viewer_client.get("/api/conversations").json()) == before


def test_panel_ask_requires_exactly_one_model_in_scope(viewer_client, fake_translator):
    res = viewer_client.post("/api/chat/panel/ask/stream", json={"question": "x", "model_scope": []})
    assert res.status_code == 400
    res = viewer_client.post("/api/chat/panel/ask/stream", json={
        "question": "x", "model_scope": ["sales", "sales"],
    })
    assert res.status_code == 400


def test_panel_ask_rejects_unknown_model(viewer_client, fake_translator):
    res = viewer_client.post("/api/chat/panel/ask/stream", json={
        "question": "x", "model_scope": ["not_a_real_model"],
    })
    assert res.status_code == 400


def test_panel_ask_disabled_without_llm_key(viewer_client, monkeypatch):
    monkeypatch.setattr(chat_api.config, "LLM_ENABLED", False)
    res = viewer_client.post("/api/chat/panel/ask/stream", json={
        "question": "x", "model_scope": ["sales"],
    })
    assert res.status_code == 503


def test_panel_ask_description_override_reaches_the_catalog(viewer_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    res = viewer_client.post("/api/chat/panel/ask/stream", json={
        "question": "revenue by category", "model_scope": ["sales"],
        "description": "UNSAVED DRAFT DESCRIPTION TEXT",
    })
    assert res.status_code == 200
    _, catalog, _ = fake_translator.calls[0]
    sales_entry = next(m for m in catalog if m.name == "sales")
    assert sales_entry.description == "UNSAVED DRAFT DESCRIPTION TEXT"


def test_panel_ask_history_feeds_prior_context(viewer_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    res = viewer_client.post("/api/chat/panel/ask/stream", json={
        "question": "and last quarter?", "model_scope": ["sales"],
        "history": [{
            "question_text": "revenue by category", "model": "sales",
            "dimensions": ["category"], "measures": ["revenue"], "filters": [],
        }],
    })
    assert res.status_code == 200
    _, _, prior_context = fake_translator.calls[0]
    assert len(prior_context) == 1
    assert prior_context[0].question_text == "revenue by category"
    assert prior_context[0].model == "sales"


def test_panel_ask_translator_error_is_not_persisted(viewer_client, fake_translator):
    before = len(viewer_client.get("/api/conversations").json())
    fake_translator.responses.append(TranslatorError("boom"))
    res = viewer_client.post("/api/chat/panel/ask/stream", json={
        "question": "revenue by category", "model_scope": ["sales"],
    })
    assert res.status_code == 200
    events = dict(_parse_sse(res.text))
    assert events["response"]["response"]["outcome"] == "error"
    assert len(viewer_client.get("/api/conversations").json()) == before
