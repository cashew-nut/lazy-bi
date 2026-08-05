"""The MCP server (specs/017-agent-skills-mcp-server/): exposes declared
Agents/Skills to external MCP clients over Streamable HTTP, mounted at
`/mcp` inside the existing FastAPI app (app/main.py) — not a separate
service. Authentication is not this module's concern at all: `/mcp` is
guarded by the same `AuthMiddleware` that guards `/api` (default-deny, no
anonymous handshake), so every request this module ever sees already has
`request.state.user` set by the time it arrives (research.md R1, R2).

Both `tools/list` and `tools/call` are synthesized live from app/skills.py's
registry and `registry.agents` on every single request — not from FastMCP's
own built-in static tool manager, which this module never registers
anything into. That's a deliberate choice, not a simplification left for
later: a skill only counts as *exposed* while some currently-loaded Agent
references it, and re-deriving both the tool list and the dispatch target
from the live registries on every call is what makes an agents/*.yaml edit
+ `registry.reload_all()` take effect immediately, with no server rebuild
(spec 017 User Story 3) — and what lets a skill registered at any point
before a given request (not just before this module's build_mcp() ran) be
listed and called.
"""
from __future__ import annotations

from typing import Sequence

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool import Tool, ToolResult
from mcp import types as mt

from . import skills as skills_mod
from .auth import User
from .registry import registry


def _current_user() -> User:
    """The identity AuthMiddleware already authenticated for this request
    (app/main.py) — never re-derived here. Missing only if this module were
    ever reached without going through that middleware first, which the
    /mcp mount in app/main.py doesn't allow."""
    request = get_http_request()
    user = getattr(request.state, "user", None)
    if user is None:
        raise RuntimeError("no authenticated user on the current MCP request")
    return user


def _agent_skill_names() -> set[str]:
    """The union of skill names referenced by every currently-loaded Agent —
    read live from registry.agents on every call, so an agents/*.yaml edit
    followed by a reload takes effect immediately without rebuilding this
    server (spec 017 User Story 3)."""
    names: set[str] = set()
    for agent in registry.agents.values():
        names.update(agent.skills)
    return names


def _make_tool_fn(skill: skills_mod.Skill):
    # Plain sync (see app/skills.py's SkillHandler note) — FastMCP runs it
    # in a thread pool automatically (FunctionTool's run_in_thread default).
    def _fn(**kwargs):
        user = _current_user()
        return skills_mod.invoke_skill(skill, user, kwargs)

    _fn.__name__ = f"skill_{skill.name}"
    return _fn


def _build_tool(skill: skills_mod.Skill) -> FunctionTool:
    return FunctionTool(
        name=skill.name,
        description=skill.description,
        parameters=skill.input_schema,
        output_schema=skill.output_schema,
        fn=_make_tool_fn(skill),
    )


class AgentExposureMiddleware(Middleware):
    """The entire tools/list and tools/call implementation: both are
    computed fresh from app/skills.py's registry + registry.agents on every
    call (see module docstring) rather than delegating to FastMCP's own
    built-in tool manager (call_next is never invoked here). Enforces, at
    both listing and call time, that a skill is currently referenced by a
    loaded Agent (spec 017 User Story 3) and — at listing time only,
    research.md R7 — that the connection's authenticated role satisfies the
    skill's min_role, so a lower-privilege caller never sees a tool it
    can't use, not merely gets refused on call. skills.invoke_skill() is
    the invocation-time backstop for the same role check (spec.md edge
    case: a client that calls a skill name directly, bypassing discovery)."""

    async def on_list_tools(
        self, context: MiddlewareContext[mt.ListToolsRequest], call_next: CallNext
    ) -> Sequence[Tool]:
        exposed = _agent_skill_names()
        user = _current_user()
        visible = []
        for name in sorted(exposed):
            skill = skills_mod.get_skill(name)
            if skill is not None and user.has_role(skill.min_role):
                visible.append(_build_tool(skill))
        return visible

    async def on_call_tool(
        self, context: MiddlewareContext[mt.CallToolRequestParams], call_next: CallNext
    ) -> ToolResult:
        name = context.message.name
        skill = skills_mod.get_skill(name)
        if skill is None or name not in _agent_skill_names():
            raise NotFoundError(f"unknown tool: {name!r}")
        tool = _build_tool(skill)
        return await tool.run(context.message.arguments or {})


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        name="lazy-bi",
        instructions=(
            "Query the platform's declared semantic models over MCP. "
            "Only models/dimensions/measures declared in the semantic "
            "layer are queryable — never a raw column or arbitrary code."
        ),
    )
    mcp.add_middleware(AgentExposureMiddleware())
    return mcp


def create_asgi_app():
    """The ASGI sub-application to mount at /mcp in app/main.py. Its
    `.lifespan` must be entered explicitly by the parent app's own
    lifespan — Starlette does not do this automatically for a mounted
    sub-app (research.md R2).

    stateless_http=True: every skill this feature exposes tracks its own
    state through its own arguments (ask_question's conversation_id), not
    through MCP session state, so there is nothing an MCP-protocol session
    would buy here — going stateless means no server-side session-affinity
    concern for a remote, multi-tenant deployment (spec.md's clarified
    transport choice), and no Mcp-Session-Id bookkeeping for a client to
    get wrong."""
    mcp = build_mcp()
    return mcp.http_app(path="/", transport="streamable-http", stateless_http=True)
