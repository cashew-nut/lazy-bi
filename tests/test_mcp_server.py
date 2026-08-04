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

from app import skills
from app.agents import Agent
from app.registry import registry

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
