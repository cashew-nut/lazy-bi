"""The mounted /mcp app (specs/017-agent-skills-mcp-server/): smoke tests
that the real ASGI mount + lifespan composition + auth gate actually work
(T010) — deliberately via a plain ASGI TestClient POST, not fastmcp's
in-memory client, since an in-memory client bypasses the mount/lifespan
wiring this test exists to catch (research.md R2, R5). ask_question/
list_models end-to-end coverage lives here too (T018-T019, T023), added in
later phases.
"""
from __future__ import annotations

import json

import pytest

from app import skills, skills_analytics
from app.agents import Agent
from app.llm import RawToolCall, TranslatorError
from app.registry import registry

from .test_nlq import FakeTranslator

_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def mcp_call(client, method: str, params: dict | None = None, id: int = 1):
    body = {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
    return client.post("/mcp/", json=body, headers=_MCP_HEADERS)


def mcp_result(response) -> dict:
    assert response.status_code == 200, response.text
    for line in response.text.split("\n"):
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"no SSE data line in response: {response.text!r}")


def _initialize(client, id: int = 1):
    return mcp_call(client, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    }, id=id)


def test_unauthenticated_connection_is_rejected_before_any_protocol_exchange(anon_client):
    r = _initialize(anon_client)
    assert r.status_code == 401


def test_authenticated_initialize_handshake_succeeds(viewer_client):
    r = _initialize(viewer_client)
    assert r.status_code == 200
    result = mcp_result(r)["result"]
    assert result["serverInfo"]["name"] == "lazy-bi"


@pytest.fixture
def viewer_only_fixture_skill():
    def handler(user, args):
        return {"who": user.username, "args": args}

    skill = skills.Skill(
        name="_fixture_echo", description="test-only echo", min_role="viewer",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        output_schema={"type": "object"}, handler=handler,
    )
    skills.register_skill(skill)
    yield skill
    skills.unregister_skill(skill.name)


@pytest.fixture
def fixture_agent(viewer_only_fixture_skill, monkeypatch):
    agent = Agent(name="_fixture_agent", label="Fixture", description="",
                   skills=[viewer_only_fixture_skill.name])
    monkeypatch.setitem(registry.agents, agent.name, agent)
    return agent


def test_tools_call_round_trips_through_the_real_mount(viewer_client, fixture_agent):
    r = mcp_call(viewer_client, "tools/call",
                  {"name": "_fixture_echo", "arguments": {"msg": "hi"}})
    result = mcp_result(r)["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {
        "who": "viewer-tester", "args": {"msg": "hi"},
    }


# ── ask_question, the real analytics skill (T018-T019, US1) ────────────────
# agents/analytics.yaml is loaded for real at app startup — no fixture agent
# needed here, unlike the synthetic-skill tests above.

@pytest.fixture
def fake_translator(monkeypatch):
    translator = FakeTranslator([])
    monkeypatch.setattr(skills_analytics, "_translator", translator)
    return translator


@pytest.fixture(autouse=True)
def llm_enabled(monkeypatch):
    monkeypatch.setattr(skills_analytics.config, "LLM_ENABLED", True)


def _propose_sales_by_category():
    return RawToolCall("propose_query", {
        "model": "sales", "dimensions": ["category"], "measures": ["revenue"],
        "filters": [], "sort": None, "limit": None,
    })


def _ask(client, question: str, conversation_id: int | None = None) -> dict:
    args = {"question": question}
    if conversation_id is not None:
        args["conversation_id"] = conversation_id
    r = mcp_call(client, "tools/call", {"name": "ask_question", "arguments": args})
    result = mcp_result(r)["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def test_ask_question_answered_returns_real_data(viewer_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    body = _ask(viewer_client, "revenue by category")
    assert body["response"]["outcome"] == "answered"
    assert body["response"]["result"]["row_count"] > 0
    assert isinstance(body["conversation_id"], int)


def test_ask_question_ambiguous_returns_clarification(viewer_client, fake_translator):
    fake_translator.responses.append(RawToolCall(
        "ask_clarification", {"question_text": "which model?", "candidates": ["sales", "marketing"]}))
    body = _ask(viewer_client, "revenue")
    assert body["response"]["outcome"] == "clarification"
    assert "sales" in body["response"]["answer_text"]


def test_ask_question_out_of_scope_returns_decline(viewer_client, fake_translator):
    fake_translator.responses.append(RawToolCall("decline", {"reason_text": "not in the semantic layer"}))
    body = _ask(viewer_client, "raw sql please")
    assert body["response"]["outcome"] == "declined"


def test_ask_question_follow_up_reuses_conversation_and_prior_context(viewer_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    first = _ask(viewer_client, "revenue by category")
    conv_id = first["conversation_id"]

    fake_translator.responses.append(_propose_sales_by_category())
    second = _ask(viewer_client, "now by month", conversation_id=conv_id)
    assert second["conversation_id"] == conv_id
    assert len(fake_translator.calls) == 2
    assert fake_translator.calls[1][2], "second call should carry prior-turn context"


def test_ask_question_conversation_not_owned_by_caller_is_not_found(viewer_client, admin_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    admins = _ask(admin_client, "revenue by category")

    fake_translator.responses.append(_propose_sales_by_category())
    body = _ask(viewer_client, "x", conversation_id=admins["conversation_id"])
    assert body["response"]["outcome"] == "error"
    assert "not found" in body["response"]["answer_text"]


def test_ask_question_not_configured_returns_error_without_calling_translator(viewer_client, monkeypatch, fake_translator):
    monkeypatch.setattr(skills_analytics.config, "LLM_ENABLED", False)
    body = _ask(viewer_client, "revenue by category")
    assert body["response"]["outcome"] == "error"
    assert "not configured" in body["response"]["answer_text"]
    assert fake_translator.calls == []


def test_ask_question_translator_error_returns_error_outcome(viewer_client, fake_translator):
    fake_translator.responses.append(TranslatorError("boom"))
    body = _ask(viewer_client, "revenue by category")
    assert body["response"]["outcome"] == "error"


def test_ask_question_is_audited(viewer_client, fake_translator):
    fake_translator.responses.append(_propose_sales_by_category())
    _ask(viewer_client, "revenue by category")
    events = registry.auth_store.audit_events()
    assert any(e["action"] == "mcp_skill:ask_question" for e in events)
    assert any(e["action"] == "chat_ask" for e in events)  # nlq.handle_decision's own audit, reused as-is


def test_ask_question_visible_in_the_browser_chat_too(viewer_client, fake_translator):
    """Proves ask_question and the browser's conversational-analytics chat
    share one ConversationStore, not a parallel MCP-only history."""
    fake_translator.responses.append(_propose_sales_by_category())
    body = _ask(viewer_client, "revenue by category")
    r = viewer_client.get(f"/api/conversations/{body['conversation_id']}")
    assert r.status_code == 200
    assert r.json()["messages"][-1]["outcome"] == "answered"


# ── rate limiting (T019) ────────────────────────────────────────────────

def test_ask_question_rate_limit_blocks_without_calling_translator(viewer_client, fake_translator, monkeypatch):
    monkeypatch.setattr(skills, "rate_limiter", skills._RateLimiter(per_minute=1))
    fake_translator.responses.append(_propose_sales_by_category())
    first = _ask(viewer_client, "revenue by category")
    assert first["response"]["outcome"] == "answered"

    second = _ask(viewer_client, "revenue by category")
    assert second["response"]["outcome"] == "rate_limited"
    assert len(fake_translator.calls) == 1, "the blocked call must never reach the translator"

    events = registry.auth_store.audit_events()
    assert any(e["action"] == "mcp_skill:ask_question:rate_limited" for e in events)


# ── discovery: role-filtered tools/list, and list_models (T020-T023, US2) ──

def _tool_names(client) -> set[str]:
    return {t["name"] for t in mcp_result(mcp_call(client, "tools/list"))["result"]["tools"]}


def test_discovery_lists_analytics_skills_for_every_role(viewer_client, author_client, admin_client):
    for client in (viewer_client, author_client, admin_client):
        assert {"ask_question", "list_models"} <= _tool_names(client)


@pytest.fixture
def admin_only_fixture_skill():
    def handler(user, args):
        return {"ok": True}

    skill = skills.Skill(
        name="_fixture_admin_only", description="test-only admin skill", min_role="admin",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"}, handler=handler,
    )
    skills.register_skill(skill)
    yield skill
    skills.unregister_skill(skill.name)


@pytest.fixture
def fixture_agent_with_admin_skill(admin_only_fixture_skill, monkeypatch):
    agent = Agent(name="_fixture_agent_admin", label="Fixture", description="",
                   skills=[admin_only_fixture_skill.name])
    monkeypatch.setitem(registry.agents, agent.name, agent)
    return agent


def test_tools_list_hides_higher_role_skill_from_lower_role_connection(
    viewer_client, admin_client, fixture_agent_with_admin_skill
):
    """Discovery-time filtering (research.md R7): a viewer never sees an
    admin-only tool in the list at all — not merely gets refused calling it."""
    assert "_fixture_admin_only" not in _tool_names(viewer_client)
    assert "_fixture_admin_only" in _tool_names(admin_client)


def test_calling_a_hidden_higher_role_skill_directly_is_still_refused(
    viewer_client, fixture_agent_with_admin_skill
):
    """Invocation-time backstop (skills.SkillPermissionError) for a client
    that calls a skill name directly, bypassing discovery (spec.md edge case)."""
    r = mcp_call(viewer_client, "tools/call", {"name": "_fixture_admin_only", "arguments": {}})
    result = mcp_result(r)["result"]
    assert result["isError"] is True
    assert "admin" in result["content"][0]["text"]


def test_list_models_matches_build_catalog(viewer_client):
    from app import nlq
    r = mcp_call(viewer_client, "tools/call", {"name": "list_models", "arguments": {}})
    result = mcp_result(r)["result"]
    assert result["isError"] is False
    body = json.loads(result["content"][0]["text"])

    expected = nlq.build_catalog(registry.models, [], memories=registry.memory_store.all_by_model())
    assert {m["name"] for m in body["models"]} == {e.name for e in expected}
    assert len(body["models"]) == len(expected)
    sales = next(m for m in body["models"] if m["name"] == "sales")
    assert any(d["name"] == "category" for d in sales["dimensions"])
    assert any(m["name"] == "revenue" for m in sales["measures"])


def test_list_models_is_not_rate_limited(viewer_client, monkeypatch):
    monkeypatch.setattr(skills, "rate_limiter", skills._RateLimiter(per_minute=0))
    r = mcp_call(viewer_client, "tools/call", {"name": "list_models", "arguments": {}})
    assert mcp_result(r)["result"]["isError"] is False
