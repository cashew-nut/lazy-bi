"""The Skill abstraction (specs/017-agent-skills-mcp-server/): a named,
typed, role-gated capability with a handler, generalizing the ad hoc
tool-calling shape `app/llm.py`'s `_TOOLS` already used for conversational
analytics into something any caller — today the MCP server in
`app/mcpserver.py` — can dispatch through uniformly.

`invoke_skill()` is the one dispatch path every skill call goes through
(role check -> rate limit -> handler -> audit), so a future skill gets
those guarantees for free rather than having to reimplement them, exactly
as `nlq.resolve()` re-validating a proposal is the one path every
conversational-analytics answer goes through today.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from . import config
from .auth import User

# handler(user, args) -> a JSON-serializable dict; re-validates its own
# input against live platform state before acting (never trusts args just
# because they matched input_schema) — the same rule nlq.resolve() already
# enforces for LLM-declared propose_query input, generalized to every skill.
# Plain sync, not async: every business-logic module this calls into
# (engine.py, nlq.py) is already synchronous — no async/await pattern
# exists anywhere else in this codebase — and FastMCP runs a sync tool
# function in a thread pool automatically (run_in_thread=True by default),
# so there is nothing to gain from introducing async here.
SkillHandler = Callable[[User, dict], dict]

# Only consulted when a rate-limited skill is blocked (see invoke_skill).
# Lets a skill shape its own "blocked" result (e.g. ask_question's
# outcome-discriminated envelope) instead of a generic one.
BlockedFormatter = Callable[[str], dict]


def _default_blocked(reason: str) -> dict:
    return {"error": reason}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    min_role: str  # "viewer" | "author" | "admin" — app.auth.ROLE_ORDER
    input_schema: dict
    output_schema: dict
    handler: SkillHandler
    rate_limited: bool = False
    on_blocked: Optional[BlockedFormatter] = None


class SkillPermissionError(Exception):
    """Raised by invoke_skill() when the caller's role doesn't satisfy the
    skill's min_role. A well-behaved MCP client never hits this — role-based
    tools/list filtering (app/mcpserver.py) keeps a caller from seeing a
    skill it can't use — this is the invocation-time backstop for a client
    that calls a skill name directly anyway (spec.md edge case)."""


_SKILLS: dict[str, Skill] = {}


def register_skill(skill: Skill) -> None:
    if skill.name in _SKILLS:
        raise ValueError(f"skill already registered: {skill.name!r}")
    _SKILLS[skill.name] = skill


def get_skill(name: str) -> Optional[Skill]:
    return _SKILLS.get(name)


def all_skills() -> list[Skill]:
    return list(_SKILLS.values())


def unregister_skill(name: str) -> None:
    """Test-only: removes one registered skill (a synthetic fixture a test
    registered itself) without disturbing the real ones — application
    startup never calls this."""
    _SKILLS.pop(name, None)


class _RateLimiter:
    """Per-identity sliding window over the last 60s — in-process, no shared
    store (research.md R3: single uvicorn worker by design, so there is no
    multi-process case to coordinate across)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[int, deque[float]] = {}

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        window_start = now - 60.0
        hits = self._hits.setdefault(user_id, deque())
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        """Test-only: clears all counters between tests."""
        self._hits.clear()


rate_limiter = _RateLimiter(config.MCP_RATE_LIMIT_PER_MIN)


def _audit(action: str, user: User, target: str) -> None:
    # Local import: app/registry.py loads app/agents.py which validates
    # against this module's registry, so a module-level import here would
    # cycle (registry -> agents -> skills -> registry). By the time this
    # function actually runs (a real skill call, well after startup), the
    # cycle is moot.
    from .registry import registry
    store = registry.auth_store
    if store is not None:
        store.record_audit(action, user.username, actor_user_id=user.id, target=target)


def invoke_skill(skill: Skill, user: User, args: dict) -> dict:
    """Role check -> rate limit (only for rate_limited skills) -> handler ->
    audit. Every outcome is audited, including a denial or a rate-limit
    block — not just successful calls (FR-008, data-model.md "Skill
    Invocation") — so an admin can see attempted-but-blocked access too.

    A skill's own handler may already write a richer, domain-specific audit
    entry of its own (e.g. ask_question reuses nlq.py's existing "chat_ask"
    audit) — the generic "mcp_skill:<name>" entry written here is
    additional, not a replacement, so FR-008 holds uniformly even for a
    future skill whose handler doesn't self-audit.
    """
    if not user.has_role(skill.min_role):
        _audit(f"mcp_skill:{skill.name}:denied", user, f"requires role {skill.min_role}")
        raise SkillPermissionError(f"'{skill.name}' requires the {skill.min_role} role")

    if skill.rate_limited and not rate_limiter.allow(user.id):
        _audit(f"mcp_skill:{skill.name}:rate_limited", user,
               f"limit {config.MCP_RATE_LIMIT_PER_MIN}/min")
        formatter = skill.on_blocked or _default_blocked
        return formatter("rate limit exceeded")

    result = skill.handler(user, args)
    _audit(f"mcp_skill:{skill.name}", user, f"args={args!r}"[:300])
    return result
