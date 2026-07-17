"""Self-learning chat memories: the assistant records model-scoped facts it
learns through chat (missing synonyms, vocabulary notes), those facts feed
every later conversation's catalog, and admins curate the pool via
/api/models/{name}/memories. Nothing user-scoped is ever stored or read —
memories describe the semantic model, not the person asking."""
from __future__ import annotations

import pytest

from app import nlq
from app.api import chat as chat_api
from app.llm import RawToolCall, _catalog_text
from app.registry import registry

from .test_nlq import FakeTranslator


@pytest.fixture
def fake_translator(monkeypatch):
    translator = FakeTranslator([])
    monkeypatch.setattr(chat_api, "_translator", translator)
    return translator


@pytest.fixture(autouse=True)
def llm_enabled(monkeypatch):
    monkeypatch.setattr(chat_api.config, "LLM_ENABLED", True)


@pytest.fixture(autouse=True)
def clean_memories(anon_client):
    """Each test starts and ends with an empty memory pool — memories are
    global (model-scoped) state, unlike per-user conversations."""
    yield
    store = registry.memory_store
    for mems in store.all_by_model().values():
        for m in mems:
            store.delete(m["id"])


def _propose_with_memory(memories):
    return RawToolCall("propose_query", {
        "model": "sales", "dimensions": ["category"], "measures": ["revenue"],
        "filters": [], "sort": None, "limit": None, "memories": memories,
    })


def _ask(client, translator, response, question="revenue by category"):
    translator.responses.append(response)
    conv = client.post("/api/conversations", json={}).json()
    res = client.post(f"/api/conversations/{conv['id']}/ask", json={"question": question})
    assert res.status_code == 200
    return res.json()


# ── the learning loop: record via chat, persist, feed the next catalog ────

def test_ask_records_a_learned_synonym_against_the_model(viewer_client, fake_translator):
    body = _ask(viewer_client, fake_translator, _propose_with_memory(
        [{"model": "sales", "kind": "synonym", "subject": "revenue", "content": "gross takings"}]))
    assert body["response"]["outcome"] == "answered"
    assert len(body["learned"]) == 1
    assert body["learned"][0]["model"] == "sales"
    assert body["learned"][0]["kind"] == "synonym"
    assert body["learned"][0]["subject"] == "revenue"
    assert body["learned"][0]["content"] == "gross takings"
    assert body["learned"][0]["source"] == "chat"

    stored = viewer_client.get("/api/models/sales/memories").json()
    assert [(m["kind"], m["subject"], m["content"]) for m in stored] == [
        ("synonym", "revenue", "gross takings")]


def test_learned_synonym_feeds_the_next_asks_catalog(viewer_client, fake_translator):
    _ask(viewer_client, fake_translator, _propose_with_memory(
        [{"model": "sales", "kind": "synonym", "subject": "revenue", "content": "gross takings"}]))

    _ask(viewer_client, fake_translator, RawToolCall("propose_query", {
        "model": "sales", "dimensions": [], "measures": ["revenue"]}), question="gross takings")
    _, catalog, _ = fake_translator.calls[-1]
    sales = next(m for m in catalog if m.name == "sales")
    revenue = next(m for m in sales.measures if m["name"] == "revenue")
    assert "gross takings" in revenue["synonyms"]
    # declared synonyms are still there — learned ones merge, never replace
    assert "turnover" in revenue["synonyms"]


def test_learned_note_feeds_the_next_asks_catalog_and_prompt(viewer_client, fake_translator):
    _ask(viewer_client, fake_translator, _propose_with_memory(
        [{"model": "sales", "kind": "note", "content": "fiscal year starts in February"}]))

    _ask(viewer_client, fake_translator, RawToolCall("decline", {"reason_text": "n/a"}),
         question="anything")
    _, catalog, _ = fake_translator.calls[-1]
    sales = next(m for m in catalog if m.name == "sales")
    assert sales.learned_notes == ["fiscal year starts in February"]
    assert "learned fact: fiscal year starts in February" in _catalog_text(catalog)


def test_memories_can_ride_on_a_decline(viewer_client, fake_translator):
    body = _ask(viewer_client, fake_translator, RawToolCall("decline", {
        "reason_text": "no such data",
        "memories": [{"model": "sales", "kind": "note", "content": "users ask about weather here a lot"}],
    }), question="revenue by weather")
    assert body["response"]["outcome"] == "declined"
    assert len(body["learned"]) == 1
    assert viewer_client.get("/api/models/sales/memories").json()[0]["kind"] == "note"


def test_ask_stream_response_event_carries_learned(viewer_client, fake_translator):
    from .test_chat_api import _parse_sse

    fake_translator.responses.append(_propose_with_memory(
        [{"model": "sales", "kind": "synonym", "subject": "revenue", "content": "gross takings"}]))
    conv = viewer_client.post("/api/conversations", json={}).json()
    res = viewer_client.post(f"/api/conversations/{conv['id']}/ask/stream",
                              json={"question": "gross takings by category"})
    body = _parse_sse(res.text)[-1][1]
    assert body["response"]["outcome"] == "answered"
    assert len(body["learned"]) == 1
    assert body["learned"][0]["content"] == "gross takings"


def test_relearning_the_same_fact_is_a_silent_no_op(viewer_client, fake_translator):
    for _ in range(2):
        body = _ask(viewer_client, fake_translator, _propose_with_memory(
            [{"model": "sales", "kind": "synonym", "subject": "revenue", "content": "Gross Takings"}]))
    # the second ask stored nothing new (case-insensitive duplicate) and
    # reported nothing learned — but still answered normally
    assert body["response"]["outcome"] == "answered"
    assert body["learned"] == []
    assert len(viewer_client.get("/api/models/sales/memories").json()) == 1


# ── LLM-proposed memories are re-validated, never trusted ─────────────────

def test_invalid_memories_are_dropped_without_changing_the_answer(viewer_client, fake_translator):
    body = _ask(viewer_client, fake_translator, _propose_with_memory([
        {"model": "sales", "kind": "synonym", "subject": "not_a_field", "content": "x"},
        {"model": "not_a_model", "kind": "note", "content": "y"},
        {"model": "sales", "kind": "preference", "content": "the user likes bar charts"},
        {"model": "sales", "kind": "note", "content": ""},
        "not-even-a-dict",
        {"model": "sales", "kind": "synonym", "subject": "revenue", "content": "turnover"},  # already declared
    ]))
    assert body["response"]["outcome"] == "answered"
    assert body["learned"] == []
    assert viewer_client.get("/api/models/sales/memories").json() == []


def test_memory_outside_conversation_scope_is_dropped(viewer_client, fake_translator):
    fake_translator.responses.append(RawToolCall("propose_query", {
        "model": "sales", "dimensions": [], "measures": ["revenue"],
        "memories": [{"model": "logistics", "kind": "note", "content": "out of scope fact"}],
    }))
    conv = viewer_client.post("/api/conversations", json={"model_scope": ["sales"]}).json()
    body = viewer_client.post(f"/api/conversations/{conv['id']}/ask",
                               json={"question": "revenue"}).json()
    assert body["learned"] == []
    assert viewer_client.get("/api/models/logistics/memories").json() == []


def test_memories_per_turn_are_capped(viewer_client, fake_translator):
    body = _ask(viewer_client, fake_translator, _propose_with_memory(
        [{"model": "sales", "kind": "note", "content": f"fact number {i}"} for i in range(6)]))
    assert len(body["learned"]) == nlq.MAX_MEMORIES_PER_TURN
    assert len(viewer_client.get("/api/models/sales/memories").json()) == nlq.MAX_MEMORIES_PER_TURN


# ── admin curation API ─────────────────────────────────────────────────────

def test_admin_can_create_edit_and_delete_memories(admin_client):
    created = admin_client.post("/api/models/sales/memories", json={
        "kind": "synonym", "subject": "revenue", "content": "gross takings"})
    assert created.status_code == 201
    memory = created.json()
    assert memory["source"] == "admin"

    patched = admin_client.patch(f"/api/models/sales/memories/{memory['id']}",
                                  json={"content": "net takings"})
    assert patched.status_code == 200
    assert patched.json()["content"] == "net takings"
    assert patched.json()["updated_at"] >= memory["created_at"]

    assert admin_client.delete(f"/api/models/sales/memories/{memory['id']}").status_code == 204
    assert admin_client.get("/api/models/sales/memories").json() == []


def test_memory_mutations_are_admin_only(viewer_client, author_client, admin_client):
    for client in (viewer_client, author_client):
        assert client.post("/api/models/sales/memories",
                           json={"kind": "note", "content": "x"}).status_code == 403
        assert client.patch("/api/models/sales/memories/1", json={}).status_code == 403
        assert client.delete("/api/models/sales/memories/1").status_code == 403
    # reads are open to every authenticated role
    assert viewer_client.get("/api/models/sales/memories").status_code == 200


def test_admin_create_validates_like_chat_learning(admin_client):
    bad_kind = admin_client.post("/api/models/sales/memories",
                                  json={"kind": "preference", "content": "likes pie charts"})
    assert bad_kind.status_code == 400
    bad_target = admin_client.post("/api/models/sales/memories",
                                    json={"kind": "synonym", "subject": "nope", "content": "x"})
    assert bad_target.status_code == 400
    redundant = admin_client.post("/api/models/sales/memories",
                                   json={"kind": "synonym", "subject": "revenue", "content": "turnover"})
    assert redundant.status_code == 400
    no_content = admin_client.post("/api/models/sales/memories",
                                    json={"kind": "note", "content": "   "})
    assert no_content.status_code == 400


def test_admin_duplicate_create_conflicts(admin_client):
    body = {"kind": "note", "content": "fiscal year starts in February"}
    assert admin_client.post("/api/models/sales/memories", json=body).status_code == 201
    assert admin_client.post("/api/models/sales/memories", json=body).status_code == 409


def test_memory_routes_404_for_unknown_model_and_cross_model_ids(admin_client):
    assert admin_client.get("/api/models/nope/memories").status_code == 404
    assert admin_client.post("/api/models/nope/memories",
                             json={"kind": "note", "content": "x"}).status_code == 404
    memory = admin_client.post("/api/models/sales/memories",
                               json={"kind": "note", "content": "a sales fact"}).json()
    # the same id is not addressable under a different model
    assert admin_client.patch(f"/api/models/logistics/memories/{memory['id']}",
                              json={"content": "y"}).status_code == 404
    assert admin_client.delete(f"/api/models/logistics/memories/{memory['id']}").status_code == 404
    assert admin_client.patch("/api/models/sales/memories/999999", json={}).status_code == 404


def test_admin_edited_memory_is_what_feeds_the_catalog(admin_client, viewer_client, fake_translator):
    memory = admin_client.post("/api/models/sales/memories", json={
        "kind": "synonym", "subject": "revenue", "content": "gross takings"}).json()
    admin_client.patch(f"/api/models/sales/memories/{memory['id']}",
                       json={"content": "monthly takings"})

    _ask(viewer_client, fake_translator, RawToolCall("decline", {"reason_text": "n/a"}))
    _, catalog, _ = fake_translator.calls[-1]
    revenue = next(m for m in next(c for c in catalog if c.name == "sales").measures
                   if m["name"] == "revenue")
    assert "monthly takings" in revenue["synonyms"]
    assert "gross takings" not in revenue["synonyms"]


def test_deleted_memory_stops_feeding_the_catalog(admin_client, viewer_client, fake_translator):
    memory = admin_client.post("/api/models/sales/memories", json={
        "kind": "note", "content": "temporary fact"}).json()
    admin_client.delete(f"/api/models/sales/memories/{memory['id']}")

    _ask(viewer_client, fake_translator, RawToolCall("decline", {"reason_text": "n/a"}))
    _, catalog, _ = fake_translator.calls[-1]
    assert next(c for c in catalog if c.name == "sales").learned_notes == []


# ── nothing user-scoped: the store has no per-user retrieval axis ──────────

def test_memories_are_shared_across_users_not_per_user(viewer_client, author_client, fake_translator, monkeypatch):
    """A fact learned in one user's conversation grounds every other user's
    next ask — memories attach to the model, never to the signed-in user."""
    _ask(viewer_client, fake_translator, _propose_with_memory(
        [{"model": "sales", "kind": "synonym", "subject": "revenue", "content": "gross takings"}]))

    other = FakeTranslator([RawToolCall("decline", {"reason_text": "n/a"})])
    monkeypatch.setattr(chat_api, "_translator", other)
    conv = author_client.post("/api/conversations", json={}).json()
    author_client.post(f"/api/conversations/{conv['id']}/ask", json={"question": "anything"})
    _, catalog, _ = other.calls[-1]
    revenue = next(m for m in next(c for c in catalog if c.name == "sales").measures
                   if m["name"] == "revenue")
    assert "gross takings" in revenue["synonyms"]


def test_memory_rows_never_key_on_a_user_id():
    """Schema-level guard for the privacy requirement: the memories table
    must carry no user_id column at all — created_by is a display/audit
    label, and there is deliberately nothing to scope a read by."""
    from app import memorystore

    assert "user_id" not in memorystore.SCHEMA
    assert "created_by" in memorystore.SCHEMA
