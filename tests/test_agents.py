"""Unit tests for app/agents.py's agents/*.yaml loader (specs/017-agent-
skills-mcp-server/tasks.md T012, T024). Uses synthetic test-only skills and
a temp directory of fixture YAML — never the shipped agents/analytics.yaml.
"""
from __future__ import annotations

import textwrap

import pytest

from app import agents as agents_mod
from app import skills


def _echo(user, args):
    return {"ok": True}


@pytest.fixture
def two_fixture_skills(anon_client):
    a = skills.Skill(
        name="_agentfix_a", description="", min_role="viewer",
        input_schema={"type": "object", "properties": {}}, output_schema={"type": "object"},
        handler=_echo,
    )
    b = skills.Skill(
        name="_agentfix_b", description="", min_role="viewer",
        input_schema={"type": "object", "properties": {}}, output_schema={"type": "object"},
        handler=_echo,
    )
    skills.register_skill(a)
    skills.register_skill(b)
    yield a, b
    skills.unregister_skill(a.name)
    skills.unregister_skill(b.name)


def test_load_agents_empty_directory_yields_no_agents(tmp_path):
    assert agents_mod.load_agents(tmp_path / "does-not-exist") == {}


def test_load_agents_parses_name_label_description_skills(tmp_path, two_fixture_skills):
    a, b = two_fixture_skills
    (tmp_path / "fixture.yaml").write_text(textwrap.dedent(f"""\
        name: fixture_agent
        label: Fixture Agent
        description: a test-only agent
        skills:
          - {a.name}
          - {b.name}
    """))
    loaded = agents_mod.load_agents(tmp_path)
    assert set(loaded) == {"fixture_agent"}
    agent = loaded["fixture_agent"]
    assert agent.label == "Fixture Agent"
    assert agent.description == "a test-only agent"
    assert agent.skills == [a.name, b.name]


def test_load_agents_unknown_skill_reference_raises(tmp_path, two_fixture_skills):
    (tmp_path / "bad.yaml").write_text(textwrap.dedent("""\
        name: bad_agent
        skills:
          - this_skill_does_not_exist
    """))
    with pytest.raises(agents_mod.AgentError, match="this_skill_does_not_exist"):
        agents_mod.load_agents(tmp_path)


def test_load_agents_duplicate_name_raises(tmp_path, two_fixture_skills):
    a, _ = two_fixture_skills
    (tmp_path / "one.yaml").write_text(f"name: dup\nskills: [{a.name}]\n")
    (tmp_path / "two.yaml").write_text(f"name: dup\nskills: [{a.name}]\n")
    with pytest.raises(agents_mod.AgentError, match="duplicate agent"):
        agents_mod.load_agents(tmp_path)


def test_load_agents_missing_name_raises(tmp_path):
    (tmp_path / "noname.yaml").write_text("skills: []\n")
    with pytest.raises(agents_mod.AgentError):
        agents_mod.load_agents(tmp_path)


def test_load_agents_empty_file_skipped_quietly(tmp_path, two_fixture_skills):
    a, _ = two_fixture_skills
    (tmp_path / "empty.yaml").write_text("")
    (tmp_path / "real.yaml").write_text(f"name: real_agent\nskills: [{a.name}]\n")
    loaded = agents_mod.load_agents(tmp_path)
    assert set(loaded) == {"real_agent"}


def test_load_agents_defaults_label_to_name_and_description_to_empty(tmp_path, two_fixture_skills):
    a, _ = two_fixture_skills
    (tmp_path / "minimal.yaml").write_text(f"name: minimal\nskills: [{a.name}]\n")
    agent = agents_mod.load_agents(tmp_path)["minimal"]
    assert agent.label == "minimal"
    assert agent.description == ""


def test_reconfiguring_an_agents_skill_list_and_reloading_changes_mcp_exposure(
    tmp_path, two_fixture_skills, viewer_client, monkeypatch
):
    """T024 (US3): editing an agent's declared skill list and reloading
    changes what the mounted /mcp app lists and can call — no code change,
    using synthetic fixture skills so this needs neither US1's ask_question
    nor US2's list_models to exist first."""
    from .test_mcp_server import mcp_call, mcp_result

    a, b = two_fixture_skills
    fixture_yaml = tmp_path / "fixture.yaml"
    fixture_yaml.write_text(f"name: reconfig_agent\nskills: [{a.name}, {b.name}]\n")

    from app.registry import registry
    monkeypatch.setitem(registry.agents, "reconfig_agent",
                         agents_mod.load_agents(tmp_path)["reconfig_agent"])

    names_before = {t["name"] for t in mcp_result(mcp_call(viewer_client, "tools/list"))["result"]["tools"]}
    assert {a.name, b.name} <= names_before

    # edit the YAML to drop `b`, then reload — same mechanism registry.
    # reload_all() already uses for models/pipelines
    fixture_yaml.write_text(f"name: reconfig_agent\nskills: [{a.name}]\n")
    monkeypatch.setitem(registry.agents, "reconfig_agent",
                         agents_mod.load_agents(tmp_path)["reconfig_agent"])

    names_after = {t["name"] for t in mcp_result(mcp_call(viewer_client, "tools/list"))["result"]["tools"]}
    assert a.name in names_after
    assert b.name not in names_after

    call_b = mcp_result(mcp_call(viewer_client, "tools/call", {"name": b.name, "arguments": {}}))["result"]
    assert call_b["isError"] is True
