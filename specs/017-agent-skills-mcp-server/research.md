# Phase 0 Research: Agent & Skills Framework with MCP Server

## R1: Which Python MCP library

**Decision**: `fastmcp` (PyPI package `fastmcp`, standalone, currently at
v3.x), not the `mcp.server.fastmcp.FastMCP` class bundled inside the
official `mcp` SDK.

**Rationale**: FastMCP 1.0 was folded into the official `mcp` SDK in 2024
(`mcp.server.fastmcp.FastMCP`, recently renamed `MCPServer` in the SDK's
v2.0 beta to reduce confusion with the standalone project) but that bundled
copy is in maintenance mode — critical fixes only, no new features. The
standalone `fastmcp` package, maintained under PrefectHQ, is the actively
developed, de-facto community standard (reported >4M daily downloads as of
March 2026), supports both stdio and Streamable HTTP transports, and — most
relevant to this feature — ships a documented Middleware system
(`on_call_tool`, `on_list_tools` hooks) and HTTP-context dependency helpers
(`get_http_request()`, `get_http_headers()`) purpose-built for mounting
under an existing ASGI app and doing custom (non-OAuth) auth. This feature
needs exactly that: reuse this platform's own session/token auth (FR-004),
not MCP's OAuth-flavored auth extension, which is what the official SDK's
built-in auth story is oriented around.

**Alternatives considered**:
- `mcp.server.fastmcp.FastMCP` (official, bundled): rejected — maintenance
  mode, and its request-context/middleware story for a mounted-under-another-app
  deployment with custom auth is less developed than `fastmcp`'s.
- Hand-rolling against the low-level `mcp.server.Server`: rejected — far
  more boilerplate (manual JSON-RPC method wiring, manual Streamable HTTP
  session-manager plumbing) for no benefit here; this feature is a business
  capability on top of MCP, not a protocol implementation exercise.

**Sources**: [FastMCP 2.0 vs MCP Python SDK Server (python-sdk#1068)](https://github.com/modelcontextprotocol/python-sdk/issues/1068), [FastMCP vs FastAPI-MCP vs Python SDK — MCP.Directory](https://mcp.directory/blog/fastmcp-vs-fastapi-mcp-vs-python-sdk-2026), [FastMCP 3.0 announcement](https://jlowin.dev/blog/fastmcp-3)

## R2: Mounting under the existing FastAPI app — the lifespan pitfall

**Decision**: Build the MCP ASGI app via `fastmcp`'s `http_app(path="/",
transport="streamable-http")`, mount it in `app/main.py` with
`app.mount("/mcp", mcp_app)`, and **explicitly compose its lifespan into
the existing `lifespan()` context manager** rather than leaving Starlette
to run sub-app lifespans implicitly.

**Rationale**: A known integration failure (`python-sdk#1367`) mounting a
Streamable HTTP MCP app under an existing FastAPI app produces "Task group
is not initialized. Make sure to use run()." — the Streamable HTTP session
manager needs its own async context entered for the life of the process,
and that does not happen automatically just because the sub-app is
mounted; it must be entered as part of the parent app's lifespan (e.g. via
`contextlib.AsyncExitStack`, entering both the app's existing startup
sequence and the mcp app's own lifespan context). `app/main.py`'s
`lifespan()` already does several ordered startup steps (emulator, seed,
`registry.init()`, pipeline worker) — the MCP app's lifespan is entered
alongside those, and torn down alongside `pipeline_jobs.stop_worker()` /
`emulator.stop()` at shutdown.

**Alternatives considered**: SSE transport (the older, two-endpoint MCP
HTTP transport) — rejected; Streamable HTTP is the current recommended
remote transport and is what a modern MCP client (Claude Desktop, Claude
Code) expects by default. Running the MCP server as a fully separate ASGI
process/port — rejected; it would need its own auth re-implementation and
contradicts the "single deployment" clarification (spec.md).

**Sources**: [python-sdk#1367 — mounting under FastAPI](https://github.com/modelcontextprotocol/python-sdk/issues/1367), [FastMCP HTTP Deployment docs](https://gofastmcp.com/deployment/http)

## R3: Rate limiting implementation

**Decision**: A plain in-process, per-identity sliding-window counter
(module-level `dict[int, deque[float]]` in `app/skills.py`), gating only
skills flagged `rate_limited=True` (in the MVP: `ask_question`, the one
skill that calls the LLM). Limit configurable via `CI_MCP_RATE_LIMIT_PER_MIN`
(new env var, `config.py`, following the existing `CI_*` naming
convention), with a generous default that does not affect normal
conversational use.

**Rationale**: The platform's own packaging constraint is a single uvicorn
worker by design (in-process S3 emulator + SQLite both want a single
writer) — there is no multi-process/multi-node case to coordinate a rate
limiter across today, so an in-process counter is not a scaling shortcut,
it is the correctly-scoped implementation for this deployment model. A
distributed limiter (Redis, etc.) would be a new infrastructure dependency
this feature does not need and the platform does not otherwise have.

**Alternatives considered**: A token-bucket keyed by IP — rejected, the
platform's existing security model is identity-based (session/token), not
IP-based, and multiple legitimate users can share an egress IP. No rate
limiting — rejected per the clarification in spec.md: an external,
protocol-driven MCP caller is a materially new surface for driving
repeated LLM calls, unlike the browser chat UI's human-paced typing.

## R4: Persistence — no new tables

**Decision**: `ask_question` persists through the existing
`ConversationStore` (spec 012) exactly like the browser chat "ask" action;
skill-invocation audit trail reuses the existing `AuthStore.audit_events`
table (spec 011) with a new `action` label per skill (e.g.
`mcp_skill:ask_question`). No new SQLite tables.

**Rationale**: `audit_events(actor_user_id, actor_label, action, target,
created_at)` already has exactly the shape FR-008 needs — actor + action +
target, queryable after the fact. `ConversationStore` already has exactly
the shape a multi-turn `ask_question` needs. Adding parallel tables for the
same concepts under new names would be exactly the kind of premature
abstraction the project avoids elsewhere (README/constitution note the
project reuses `VisualStore`'s schema-on-init pattern rather than inventing
a new one each feature).

**Alternatives considered**: A dedicated `skill_invocations` table with
richer structure (full input/output snapshots) — rejected for the MVP;
`audit_events.target` (a free-text string, same as every other audited
action today) is sufficient for "traceable to the identity that made it"
(SC-004) at the same fidelity the rest of the platform already audits at.
Can be revisited if a future feature needs queryable/structured invocation
history, not needed to satisfy this spec.

## R5: Testing the MCP surface without a real network hop

**Decision**: Use `fastmcp`'s in-memory client transport (a `Client`
constructed directly against the server object, no HTTP) for
`tests/test_mcp_server.py`'s tool-discovery and tool-call assertions, the
same way `tests/test_chat_api.py` already uses FastAPI's `TestClient`
in-process rather than a real server.

**Rationale**: Matches the existing test posture (~6s full suite, no real
network calls, no real LLM calls — the existing fake `Translator` already
makes `ask_question`'s underlying LLM call deterministic in tests). An
in-memory MCP client still exercises the real tool registration, schema
validation, and middleware (role filtering, rate limiting) — only the wire
transport is swapped, exactly the same tradeoff the existing `Translator`
seam already makes for the Anthropic API.

## R6: What "re-expressing the translator as Skills" means concretely

**Decision**: The four internal tool names the LLM itself chooses between
inside one turn (`propose_query` / `ask_clarification` / `decline` /
`show_last_query`, defined in `app/llm.py`'s `_TOOLS`) are **not** turned
into four separately MCP-invokable skills — an external MCP caller does not
pick which of those four happens; the platform's own LLM call does, exactly
as today. What becomes a Skill in the new framework is the **outer**
capability an external caller actually invokes: "ask a business question
and get back whichever of those four outcomes applies" — one skill,
`ask_question`, whose typed output is a discriminated union over exactly
those outcomes (mirroring `nlq.Decision`'s existing shape:
`ProposeQuery|AskClarification|Decline|ShowQuery`, executed into a real
result for the `ProposeQuery` case). This reading satisfies FR-006 (the
translator's tool-calling pattern is what's generalized into the
reusable, typed, role-gated `Skill` shape) without inventing a
client-facing concept ("call propose_query yourself") that doesn't match
how an external agent would actually use this.

**Rationale**: `app/nlq.py`'s own module docstring already states its
design intent — "Model-agnostic entry point future features ... can call
directly" — this feature is exactly that promised second caller. The
orchestration that today lives as `app/api/chat.py`'s private helpers
(`_start_ask`, `_handle_decision`, `_persist_learned`, `_resolved_query_dict`,
`_summarize`) is transport-agnostic already (plain functions taking a
`User` and plain args, no `Request`/`Depends` coupling) — promoting them
(dropping the leading underscore, moving into `app/nlq.py`) is a rename +
move, not a rewrite, and both the existing HTTP route and the new
`ask_question` skill call the same functions afterward.

**Alternatives considered**: Exposing `propose_query`/`ask_clarification`/
`decline`/`show_last_query` as four separate MCP tools — rejected, this
would require an external MCP client to somehow decide which of the four
to call itself (that's the platform's own LLM's job, not the external
caller's), and would leak an internal implementation seam across the
protocol boundary for no benefit.

## R7: Discovery — role-filtered `tools/list`, not a bespoke skill

**Decision**: "List the Agents/Skills available to me" (User Story 2, part
1) is served by MCP's own built-in `tools/list` protocol method, filtered
per-connection by a `fastmcp` server middleware's `on_list_tools` hook that
drops any tool whose skill requires a role higher than the authenticated
caller's. "List what's queryable" (User Story 2, part 2 — the data
catalog, not the tool list) is the separate `list_models` skill, a real
callable tool, since that is genuinely feature-specific data MCP's
protocol-level discovery does not carry.

**Rationale**: `fastmcp`'s middleware system documents exactly this pattern
(resolve identity from the request in a middleware hook, filter the tool
list in `on_list_tools`, and re-check in `on_call_tool` so a client can
never invoke a tool it wasn't shown) — reusing a protocol-native mechanism
instead of building a parallel bespoke "list my skills" tool that would
just re-describe what `tools/list` already returns.

**Sources**: [FastMCP Middleware docs](https://gofastmcp.com/servers/middleware), [How to Secure Your FastMCP Server With Permission Management — Cerbos](https://www.cerbos.dev/blog/how-to-secure-your-fast-mcp-server-with-permission-management)
