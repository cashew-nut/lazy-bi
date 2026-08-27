# Agents & MCP

**Source:** `app/skills.py` (153 lines) · `app/agents.py` (76 lines) ·
`app/skills_analytics.py` (191 lines) · `app/mcpserver.py` (145 lines)

This generalizes conversational analytics' tool-calling pattern into two
reusable concepts — **Skill** and **Agent** — and exposes them to *external*
MCP clients (Claude Desktop, Claude Code, or any other MCP-capable host)
over a server mounted at `/mcp`, not just the browser's own Chat surface.

## Skill (`app/skills.py`)

```python
@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    min_role: str            # "viewer" | "author" | "admin"
    input_schema: dict        # JSON Schema
    output_schema: dict
    handler: Callable[[User, dict], dict]
    rate_limited: bool = False
    on_blocked: Optional[Callable[[str, dict], dict]] = None
```

A named, typed, role-gated capability with a handler. **Every skill call
goes through one dispatch path**, `invoke_skill(skill, user, args)`:

```
role check (user.has_role(skill.min_role)?)
    │  fail → audit "mcp_skill:<name>:denied" → SkillPermissionError
    ▼
rate limit (only if skill.rate_limited)
    │  fail → audit "mcp_skill:<name>:rate_limited" → skill.on_blocked(reason, args)
    ▼
skill.handler(user, args)
    ▼
audit "mcp_skill:<name>"  (always — including on the two block paths above,
                            so an admin can see attempted-but-blocked access,
                            not only successful calls)
```

A handler re-validates its own input against **live platform state** before
acting — it never trusts `args` just because they matched `input_schema`.
This is the same discipline `nlq.resolve()` already enforces for an LLM's
`propose_query` call, generalized to every skill (see
[Conversational Analytics](conversational-analytics.md)).

Rate limiting (`_RateLimiter`) is a per-identity sliding 60-second window,
**in-process only** — there's no shared store to coordinate across,
because the deployment model is a single uvicorn worker by design (see
[Architecture → Process model](architecture.md#process-model)).

`register_skill()` populates a module-level registry at import time;
`get_skill(name)`/`all_skills()` read it.

## Agent (`app/agents.py`, `agents/*.yaml`)

```yaml
# agents/analytics.yaml
name: analytics
description: Ask business questions and discover the semantic model catalog.
skills:
  - ask_question
  - list_models
```

```python
@dataclass
class Agent:
    name: str
    label: str
    description: str
    skills: list[str]
```

A named, described **bundle of skill names**, declared in YAML the same way
a model is declared under `models/*.yaml`. **An agent carries no privilege
of its own** — it's purely a discoverable grouping. Every skill call is
still gated by that skill's own `min_role` against the caller's *real*
role, regardless of which agent's declaration listed it; a skill only
counts as *exposed* over MCP while some currently-loaded agent references
it. `load_agents()` validates every referenced skill name against
`app/skills.py`'s registry at load time — an agent naming an unregistered
skill is a load-time error, mirroring how `app/semantic.py` validates model
YAML.

Agents reload on every `registry.reload_all()` — editing an agent's
`skills:` list and reloading changes what's exposed **immediately**, with
no code change or restart. (The skill *registry* itself, by contrast, is
populated exactly once at process start — skills are code, not reloadable
declarative state.)

## The shipped analytics agent (`app/skills_analytics.py`)

```yaml
# agents/analytics.yaml
```

Two `viewer`-tier skills, both registered as an import side effect
(`app/main.py` imports this module before `Registry.init()` or the MCP
server ever read the registry):

| Skill | Rate-limited | What it does |
|---|---|---|
| `ask_question` | yes | Wraps the **exact same** question → resolve → execute → persist → audit path the browser's Chat surface uses (`app/nlq.py`'s `start_ask`/`resolve`/`handle_decision`, promoted out of `app/api/chat.py` specifically so this skill and the HTTP route share one implementation). A call and a browser chat turn against the same `conversation_id` share one persisted history. |
| `list_models` | no | The same models/dimensions/measures catalog `ask_question` is grounded on (`nlq.build_catalog`) — discovery before asking. |

`ask_question` deliberately does **not** support a conversation's
per-model `llm_model` override the browser offers — always using the
single default translator is an intentionally narrower MVP scope, not an
oversight.

## The MCP server (`app/mcpserver.py`)

Mounted at `/mcp` (Streamable HTTP, **stateless**) inside the same FastAPI
process as the REST API — not a separate service, and its lifespan is
entered explicitly from `app/main.py`'s own lifespan (Starlette doesn't do
this automatically for a mounted ASGI sub-app).

**Authentication is not this module's concern at all** — `/mcp` is guarded
by the identical `AuthMiddleware` that guards `/api` (default-deny, no
anonymous handshake, not even for the MCP `initialize` call). By the time a
request reaches this module, `request.state.user` is already set — see
[Auth & Security](auth-and-security.md). A non-browser MCP client
authenticates with a per-user `Authorization: Bearer cipat_…` personal
access token, the same mechanism a script already uses against the REST
API.

**Both `tools/list` and `tools/call` are synthesized live** from
`app/skills.py`'s registry and `registry.agents` **on every single
request** — `AgentExposureMiddleware` never delegates to FastMCP's own
built-in static tool manager (`call_next` is never invoked). That's a
deliberate choice: a skill only counts as exposed while some
currently-loaded agent references it, and re-deriving both the tool list
and the dispatch target from the live registries on every call is what
makes an `agents/*.yaml` edit + reload take effect with no server rebuild.

```python
async def on_list_tools(self, context, call_next):
    exposed = _agent_skill_names()          # union of every loaded agent's skills
    user = _current_user()
    return [_build_tool(skill) for name in sorted(exposed)
            if (skill := skills_mod.get_skill(name)) and user.has_role(skill.min_role)]
```

**Role filtering happens at both listing and call time** — `tools/list`
only ever returns the skills this connection's authenticated role can
actually invoke (a `viewer` never sees an `admin`-only tool at all, not
merely gets refused calling one); `skills.invoke_skill()`'s own role check
is the invocation-time backstop for a client that calls a skill name
directly, bypassing discovery.

**Rate limiting**: `ask_question` calls the same LLM backend
conversational analytics does, so it's gated by the same in-process,
per-identity limiter — `CI_MCP_RATE_LIMIT_PER_MIN` (default 20). A caller
past the limit gets `outcome: "rate_limited"` immediately, with no LLM call
made.

## Scope, deliberately narrow

This feature is **read/query-only**: `ask_question` and `list_models`
never save, trigger, or author anything, and the sandbox coding agent
(admin-gated, unsandboxed real bucket I/O reach — see
[Sandbox](sandbox.md)) does **not** join the MCP surface at all. Extending
either would be a new feature that has to explicitly reopen the
constitution's trusted-config boundary principle — see
[Auth & Security → The trust boundary](auth-and-security.md#the-trust-boundary-principle-vi)
— not a side effect of adding an MCP tool.
