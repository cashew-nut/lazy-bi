"""POST /api/sandbox/agent/stream and convert's opt-in lineage generation,
through TestClient with a scripted fake agent in place of the Anthropic
client — same substitution pattern as tests/test_chat_panel_api.py, so the
whole path (context assembly -> streaming -> re-validation -> audit) runs
with zero network calls. Role gating itself is swept in
tests/test_role_matrix.py.
"""
from __future__ import annotations

import json

import pytest

from app.api import sandbox as sandbox_api
from app.sandbox_agent import AgentError, AgentStreamEvent, RawAgentCall

SALES_SOURCE = "s3://cash-intel/sales/*.parquet"


class FakeAgent:
    """Scripted CodingAgent: replays queued replies (a RawAgentCall, or an
    Exception to raise) and records the context it was handed."""

    def __init__(self, replies=None, lineage=None):
        self.replies = list(replies or [])
        self.lineage = list(lineage or [])
        self.calls = []
        self.lineage_calls = []

    def assist_streaming(self, request, notebook, history):
        self.calls.append((request, notebook, history))
        assert self.replies, "FakeAgent ran out of scripted replies"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield AgentStreamEvent(kind="tool_name", tool_name=reply.kind)
        yield AgentStreamEvent(kind="tool_input", tool_input={"partial": True})
        yield AgentStreamEvent(kind="done", final=reply)

    def describe_lineage(self, context):
        self.lineage_calls.append(context)
        assert self.lineage, "FakeAgent ran out of scripted lineage replies"
        reply = self.lineage.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def fake_agent(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr(sandbox_api, "_agent", agent)
    return agent


@pytest.fixture(autouse=True)
def agent_enabled(monkeypatch):
    monkeypatch.setattr(sandbox_api.config, "LLM_ENABLED", True)


def _cell(cell_id, source, output=None):
    cell = {"id": cell_id, "source": source}
    if output:
        cell["output"] = output
    return cell


def _parse_sse(text):
    events = []
    for chunk in text.strip("\n").split("\n\n"):
        if not chunk.strip():
            continue
        name, data = "message", ""
        for line in chunk.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data += line[len("data: "):]
        if data:
            events.append((name, json.loads(data)))
    return events


def _ask(client, **body):
    body.setdefault("request", "make it fast")
    body.setdefault("cells", [_cell("c1", "df = read('x')")])
    res = client.post("/api/sandbox/agent/stream", json=body)
    assert res.status_code == 200, res.text
    return _parse_sse(res.text)


# ── the coding agent ─────────────────────────────────────────────────────

def test_agent_streams_progress_then_a_validated_proposal(client, fake_agent):
    fake_agent.replies.append(RawAgentCall("write_cells", {
        "cells": [{"target": "c1", "source": "df = read('x').select('a')"}],
        "notes": "projection pushdown",
    }))
    events = _ask(client)

    assert [name for name, _ in events] == ["tool_name", "tool_input", "response"]
    assert events[0][1]["tool_name"] == "write_cells"
    response = events[-1][1]
    assert response["kind"] == "cells"
    assert response["notes"] == "projection pushdown"
    assert response["cells"] == [
        {"target_id": "c1", "source": "df = read('x').select('a')", "syntax_error": None}]
    assert response["warnings"] == []


def test_agent_sees_the_live_notebook_including_run_output(client, fake_agent):
    fake_agent.replies.append(RawAgentCall("answer", {"text": "ok"}))
    _ask(client, name="scratch", cells=[_cell("c1", "1/0", {
        "status": "error", "error": "ZeroDivisionError: division by zero", "stdout": "x",
        "columns": [{"name": "order_id", "dtype": "Int64"}], "row_count": 3,
    })])

    _, notebook, _ = fake_agent.calls[-1]
    assert notebook.name == "scratch"
    cell = notebook.cells[0]
    assert cell.status == "error"
    assert "ZeroDivisionError" in cell.error
    assert cell.columns == [{"name": "order_id", "dtype": "Int64"}]
    # the real (moto-seeded) bucket is listed for the agent, collapsed to
    # things a read() call can name
    assert any(f["path"].endswith("ref/products.csv") for f in notebook.files)
    assert not any("_delta_log" in f["path"] for f in notebook.files)


def test_agent_history_is_passed_through(client, fake_agent):
    fake_agent.replies.append(RawAgentCall("answer", {"text": "ok"}))
    _ask(client, history=[{"request": "earlier ask", "reply": "earlier reply"}])
    _, _, history = fake_agent.calls[-1]
    assert [(h.request, h.reply) for h in history] == [("earlier ask", "earlier reply")]


def test_proposal_naming_an_unknown_cell_becomes_a_new_cell(client, fake_agent):
    fake_agent.replies.append(RawAgentCall("write_cells", {
        "cells": [{"target": "ghost", "source": "x = 1"}]}))
    response = _ask(client)[-1][1]
    assert response["cells"][0]["target_id"] is None
    assert any("isn't a cell" in w for w in response["warnings"])


def test_a_syntactically_broken_proposal_is_reported_not_swallowed(client, fake_agent):
    fake_agent.replies.append(RawAgentCall("write_cells", {
        "cells": [{"target": "c1", "source": "df = read('x'"}]}))
    response = _ask(client)[-1][1]
    assert "syntax error" in response["cells"][0]["syntax_error"]


def test_answer_tool_returns_prose_and_no_cells(client, fake_agent):
    fake_agent.replies.append(RawAgentCall("answer", {"text": "that scan already pushes down"}))
    response = _ask(client)[-1][1]
    assert response["kind"] == "answer"
    assert response["text"] == "that scan already pushes down"
    assert response["cells"] == []


def test_empty_proposal_is_surfaced_as_an_error(client, fake_agent):
    fake_agent.replies.append(RawAgentCall("write_cells", {"cells": []}))
    response = _ask(client)[-1][1]
    assert response["kind"] == "error"


def test_agent_failure_becomes_a_response_event_not_a_500(client, fake_agent):
    fake_agent.replies.append(AgentError("connection error"))
    events = _ask(client)
    response = events[-1][1]
    assert response["kind"] == "error"
    assert "temporarily unavailable" in response["text"]


def test_empty_request_rejected(client, fake_agent):
    res = client.post("/api/sandbox/agent/stream", json={"request": "  ", "cells": []})
    assert res.status_code == 400


def test_unknown_llm_model_rejected(client, fake_agent):
    res = client.post("/api/sandbox/agent/stream",
                      json={"request": "go", "cells": [], "llm_model": "gpt-9"})
    assert res.status_code == 400


def test_agent_is_503_when_no_llm_is_configured(client, fake_agent, monkeypatch):
    monkeypatch.setattr(sandbox_api.config, "LLM_ENABLED", False)
    res = client.post("/api/sandbox/agent/stream", json={"request": "go", "cells": []})
    assert res.status_code == 503


def test_agent_calls_are_audited(client, fake_agent):
    from app.registry import registry

    fake_agent.replies.append(RawAgentCall("write_cells", {"cells": [{"target": "c1", "source": "x = 1"}]}))
    _ask(client)
    assert "sandbox.agent" in {e["action"] for e in registry.auth_store.audit_events()}


# ── convert with agent-generated lineage ─────────────────────────────────

def _convert(client, **body):
    body.setdefault("name", "orders nb")
    body.setdefault("cells", [
        _cell("c1", f'df = read("{SALES_SOURCE}")'),
        _cell("c2", "output = df.head(5)"),
    ])
    res = client.post("/api/sandbox/convert", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_convert_without_the_option_never_calls_the_agent(client, fake_agent):
    data = _convert(client)
    assert fake_agent.lineage_calls == []
    assert data["lineage"] == []
    assert "lineage:" not in data["yaml"]


def test_convert_with_lineage_renders_a_validated_section(client, fake_agent):
    fake_agent.lineage.append({
        "description": "Order lines: cleaned.",
        "lineage": [
            {"field": "order_id", "from": ["sales.order_id"], "transform": "pass-through"},
            {"field": "hallucinated", "from": ["sales.x"], "transform": "invented"},
        ],
    })
    data = _convert(client, with_lineage=True,
                    output_columns=[{"name": "order_id", "dtype": "Int64"}])

    assert data["lineage"] == [
        {"field": "order_id", "from": ["sales.order_id"], "transform": "pass-through"}]
    assert "hallucinated" not in data["yaml"]
    assert any("not a column the run produced" in w for w in data["warnings"])

    # the lineage call is given the rewritten script and the detected sources
    context = fake_agent.lineage_calls[-1]
    assert 'sources["sales"]' in context.script
    assert [s["name"] for s in context.sources] == ["sales"]

    from app import pipelines
    parsed = pipelines.parse_pipeline_text(data["yaml"].replace(
        "s3://REPLACE/ME/target   # TODO: set a real target path before saving",
        "s3://cash-intel/pipeline_test/converted"))
    assert parsed.description == "Order lines: cleaned."
    assert parsed.to_public()["lineage"][0]["field"] == "order_id"


def test_convert_with_lineage_warns_when_the_notebook_was_never_run(client, fake_agent):
    fake_agent.lineage.append({
        "lineage": [{"field": "anything", "from": ["sales.order_id"], "transform": "x"}]})
    data = _convert(client, with_lineage=True)
    assert data["lineage"][0]["field"] == "anything"
    assert any("without a run" in w for w in data["warnings"])


def test_convert_survives_an_agent_failure(client, fake_agent):
    """Losing a converted notebook to a flaky API call would be absurd — the
    conversion still returns the yaml it would have produced anyway."""
    fake_agent.lineage.append(AgentError("gateway timeout"))
    data = _convert(client, with_lineage=True)
    assert data["lineage"] == []
    assert "sources[" in data["yaml"]
    assert any("lineage generation failed" in w for w in data["warnings"])


def test_convert_with_lineage_is_503_when_no_llm_is_configured(client, fake_agent, monkeypatch):
    monkeypatch.setattr(sandbox_api.config, "LLM_ENABLED", False)
    res = client.post("/api/sandbox/convert", json={
        "name": "nb", "cells": [_cell("c1", "output = 1")], "with_lineage": True})
    assert res.status_code == 503
