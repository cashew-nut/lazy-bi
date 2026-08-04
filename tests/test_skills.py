"""Unit tests for app/skills.py's invoke_skill() dispatch: role gate, rate
limiting, and audit (specs/017-agent-skills-mcp-server/tasks.md T011) —
against synthetic test-only skills so real skills (ask_question,
list_models) are untouched. No MCP transport involved."""
from __future__ import annotations

import pytest

from app import skills
from app.auth import User


def _user(role: str, user_id: int) -> User:
    return User(id=user_id, username=f"{role}-fixture-{user_id}", display_name=role.title(), role=role)


def _echo(user, args):
    return {"who": user.username, "args": args}


@pytest.fixture
def echo_skill(anon_client):
    skill = skills.Skill(
        name="_test_echo", description="test-only echo", min_role="viewer",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"}, handler=_echo,
    )
    skills.register_skill(skill)
    yield skill
    skills.unregister_skill(skill.name)


@pytest.fixture
def admin_skill(anon_client):
    skill = skills.Skill(
        name="_test_admin_only", description="test-only admin skill", min_role="admin",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"}, handler=_echo,
    )
    skills.register_skill(skill)
    yield skill
    skills.unregister_skill(skill.name)


@pytest.fixture
def rate_limited_skill(anon_client):
    skill = skills.Skill(
        name="_test_rate_limited", description="test-only rate-limited skill", min_role="viewer",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"}, handler=_echo, rate_limited=True,
    )
    skills.register_skill(skill)
    yield skill
    skills.unregister_skill(skill.name)


def _last_audit(action_prefix: str) -> dict:
    events = [e for e in registry_events() if e["action"].startswith(action_prefix)]
    assert events, f"no audit event found for action prefix {action_prefix!r}"
    return events[-1]


def registry_events():
    from app.registry import registry
    return registry.auth_store.audit_events()


def test_invoke_skill_success_calls_handler_and_audits(echo_skill):
    user = _user("viewer", 90001)
    result = skills.invoke_skill(echo_skill, user, {"x": 1})
    assert result == {"who": user.username, "args": {"x": 1}}
    event = _last_audit(f"mcp_skill:{echo_skill.name}")
    assert event["action"] == f"mcp_skill:{echo_skill.name}"
    assert event["actor_label"] == user.username


def test_invoke_skill_role_denied_raises_and_audits(admin_skill):
    user = _user("viewer", 90002)
    with pytest.raises(skills.SkillPermissionError):
        skills.invoke_skill(admin_skill, user, {})
    event = _last_audit(f"mcp_skill:{admin_skill.name}:denied")
    assert event["actor_label"] == user.username
    assert "admin" in event["target"]


def test_invoke_skill_admin_role_satisfies_admin_only_skill(admin_skill):
    user = _user("admin", 90003)
    result = skills.invoke_skill(admin_skill, user, {})
    assert result == {"who": user.username, "args": {}}


def test_invoke_skill_rate_limited_blocks_and_audits(rate_limited_skill, monkeypatch):
    monkeypatch.setattr(skills, "rate_limiter", skills._RateLimiter(per_minute=1))
    user = _user("viewer", 90004)

    first = skills.invoke_skill(rate_limited_skill, user, {})
    assert first == {"who": user.username, "args": {}}  # under the limit: handler ran

    second = skills.invoke_skill(rate_limited_skill, user, {})
    assert second == {"error": "rate limit exceeded"}  # over the limit: handler never ran
    event = _last_audit(f"mcp_skill:{rate_limited_skill.name}:rate_limited")
    assert event["actor_label"] == user.username


def test_invoke_skill_rate_limit_is_per_identity(rate_limited_skill, monkeypatch):
    monkeypatch.setattr(skills, "rate_limiter", skills._RateLimiter(per_minute=1))
    alice, bob = _user("viewer", 90005), _user("viewer", 90006)

    assert skills.invoke_skill(rate_limited_skill, alice, {}) == {"who": alice.username, "args": {}}
    # alice is now over her own limit, but bob is a distinct identity
    assert skills.invoke_skill(rate_limited_skill, alice, {}) == {"error": "rate limit exceeded"}
    assert skills.invoke_skill(rate_limited_skill, bob, {}) == {"who": bob.username, "args": {}}


def test_invoke_skill_custom_on_blocked_formatter_is_used(anon_client, monkeypatch):
    def formatter(reason: str) -> dict:
        return {"outcome": "rate_limited", "reason": reason}

    skill = skills.Skill(
        name="_test_custom_blocked", description="", min_role="viewer",
        input_schema={"type": "object", "properties": {}}, output_schema={"type": "object"},
        handler=_echo, rate_limited=True, on_blocked=formatter,
    )
    skills.register_skill(skill)
    monkeypatch.setattr(skills, "rate_limiter", skills._RateLimiter(per_minute=0))
    try:
        user = _user("viewer", 90007)
        result = skills.invoke_skill(skill, user, {})
        assert result == {"outcome": "rate_limited", "reason": "rate limit exceeded"}
    finally:
        skills.unregister_skill(skill.name)
