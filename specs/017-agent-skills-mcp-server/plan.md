# Implementation Plan: Agent & Skills Framework with MCP Server

**Branch**: `017-agent-skills-mcp-server` | **Date**: 2026-08-04 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/017-agent-skills-mcp-server/spec.md`

## Summary

Introduce a reusable **Skill** abstraction (`app/skills.py`) — a named,
typed, role-gated capability with a handler function — and a reusable
**Agent** abstraction (`app/agents.py`) — a declared YAML bundle of skill
names (`agents/*.yaml`, loaded by `registry.py` exactly like models and
pipelines already are). The MVP re-platforms conversational analytics as
the first agent (`agents/analytics.yaml`) with two skills: `ask_question`
(wraps the existing question → propose/clarify/decline/show-query →
execute/persist/audit flow) and `list_models` (wraps the existing catalog
builder). Both skill handlers are thin wrappers around orchestration
functions promoted out of `app/api/chat.py`'s private helpers into
`app/nlq.py` — which already documents itself as the intended reuse seam
for exactly this kind of second caller — so there is one orchestration
implementation, not two.

A new `app/mcpserver.py` builds a `fastmcp.FastMCP` server, registers one
MCP tool per declared skill, and is mounted as an ASGI sub-application at
`/mcp` inside the existing FastAPI app (`app/main.py`), using the
Streamable HTTP transport so a remote, multi-tenant MCP client (Claude
Desktop, Claude Code, etc.) can connect over the network the same way it
already reaches `/api`. The existing `AuthMiddleware` is widened to also
guard `/mcp` (no anonymous MCP handshake), so every MCP connection
authenticates with the platform's existing session cookie or per-user API
token — no new auth scheme. A FastMCP server-side middleware resolves the
authenticated `User` from the shared request state on every tool call and
list-tools request, enforcing each skill's declared minimum role and
filtering discovery (`tools/list`) to only what that role can see. A
per-identity in-memory rate limiter gates the one LLM-backed skill
(`ask_question`); every skill invocation is audit-logged through the
existing `AuthStore.record_audit`, identical to how `chat_ask` is logged
today. The sandbox coding agent and every mutating capability (save,
trigger, author) are explicitly untouched and unreachable via `/mcp` in
this feature (spec.md Clarifications).

## Technical Context

**Language/Version**: Python 3.10+ (Docker image: `python:3.12-slim`), same as existing.

**Primary Dependencies**: FastAPI/Starlette, Polars, `anthropic` (unchanged,
already used by `app/llm.py`); **new**: `fastmcp` (PyPI `fastmcp`, the
de-facto community MCP server framework — donated FastMCP 1.0 lives inside
the official `mcp` SDK in maintenance mode as `mcp.server.fastmcp.FastMCP`/
`MCPServer`, but the standalone `fastmcp` package is the actively developed
one and is what this plan uses, per research.md R1). Frontend is untouched
— this feature has no browser UI surface.

**Storage**: existing SQLite database (`cash_intel.db`); no new tables. The
existing `conversation_messages`/`conversations` tables (spec 012) persist
`ask_question` turns exactly as they persist chat turns today; the existing
`audit_events` table (spec 011) records every skill invocation. No new
datastore introduced (research.md R4).

**Testing**: pytest + FastAPI `TestClient` (existing pattern), extended
with `fastmcp`'s in-memory client transport for exercising the mounted
`/mcp` app without a real network hop (research.md R5). The LLM call stays
behind the existing `Translator` seam, so tests inject the existing fake
translator — no real network calls in the test suite.

**Target Platform**: Linux server / Docker, single uvicorn worker
(unchanged) — the MCP server is mounted in the same ASGI process, not a
second service; the rate limiter is a plain in-process counter because of
this (research.md R3).

**Project Type**: web service (existing structure) — this feature adds a
second ASGI-mounted surface (`/mcp`) alongside the existing `/api`, no
frontend/mobile split.

**Performance Goals**: SC-001 — an `ask_question` call over MCP has the
same budget as today's chat "ask" (one LLM round trip, typically 1-5s, plus
one sub-second query-engine call); `list_models` and discovery
(`tools/list`) are in-process catalog reads, no LLM call, sub-second.

**Constraints**: no new data-access path — skill handlers never touch
`engine.scan`/S3 directly, only the same `nlq.py` → `engine.run_query` path
`/api/query` and `/api/conversations/.../ask` already use; the rate limiter
must not require a shared store (single-worker constraint, research.md R3);
mounting the MCP ASGI sub-app must compose its lifespan with the app's
existing `lifespan()` in `app/main.py` rather than replacing it
(research.md R2, a documented integration pitfall for this exact library).

**Scale/Scope**: one new skill/agent registry module pair, one new MCP
server module, ~2 skills for the MVP (`ask_question`, `list_models`), a
handful of orchestration functions promoted (not rewritten) from
`app/api/chat.py` into `app/nlq.py`, one new YAML agent declaration, one
new env var (rate limit). No changes to the query engine, semantic layer,
or any existing `/api` route's behavior.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Status | Notes |
|---|-----------|--------|-------|
| I | Semantic layer is the only contract | PASS | `ask_question` executes exclusively through `engine.run_query` against a live `Model`, identical to today's chat path; `list_models` only ever reflects `registry.models`. No skill introduces a raw-column or undeclared-join path. |
| II | Lazy evaluation, pushdown | PASS | No change to `engine.scan`/`run_query`; MCP-originated queries take the same lazy Polars path as the browser query builder and chat. |
| III | Every feature ships with tests | PASS (planned) | New `tests/test_skills.py` (role gating, rate limiting, audit), `tests/test_agents.py` (YAML loading/validation), `tests/test_mcp_server.py` (tool discovery filtered by role, `ask_question`/`list_models` invocation end-to-end via `fastmcp`'s in-memory client), plus regression coverage on the promoted `app/nlq.py` orchestration functions reused by `app/api/chat.py`. |
| IV | Browser-verified before "done" | ADAPTED | This feature has no browser UI — its golden path is an external MCP client, not a page. quickstart.md defines the equivalent bar: connect a real MCP client (or `fastmcp`'s client library) over `/mcp` with real credentials, list tools, call `list_models`, call `ask_question` end-to-end against the demo data, and confirm a role-insufficient identity sees a narrower tool list. This is run and confirmed before the feature is reported done, in place of a browser check. |
| V | Ephemeral vs. persisted state | PASS | `ask_question` persists exactly like chat does today (a conversation + its messages, SQLite) — no new ephemeral state is introduced. An MCP call that omits `conversation_id` auto-creates a persisted conversation (via the existing `ConversationStore.create`), not a throwaway one; its id is returned so a caller can reuse it for follow-ups. Documented in data-model.md. |
| VI | Trusted-config boundary explicit | PASS — not reopened | This feature does not touch the `frame:`/pipeline-`script:`/sandbox eval path at all, and does not widen it. The sandbox coding agent is explicitly excluded from the MCP surface (spec.md FR-010, Clarifications) — extending eval-capable code execution to an external protocol client would need its own future feature that reopens this principle explicitly, which this one does not do. |
| VII | Feature branch, PR merge | PASS | All work on `017-agent-skills-mcp-server` (branch `claude/agentic-architecture-mcp-qi0upi`). |
| — | Technology constraints | PASS | Backend gains one new Python dependency (`fastmcp`) — consistent with existing precedent of backend-only third-party deps (`anthropic`, `deltalake`, `pyiceberg`, `boto3`, `moto`); this is not a frontend dependency, so the frontend "no bundler / vendored not CDN" constraint and its narrow `@finos/perspective` exception are untouched and not implicated. SQLite remains the only persistence store; single uvicorn worker preserved (the MCP mount adds no new local writer, and the rate limiter is in-process by design). |

No violations → Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/017-agent-skills-mcp-server/
├── spec.md              # Feature spec (clarified)
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md         # Phase 1 output
├── quickstart.md         # Phase 1 output
├── contracts/
│   └── mcp-server.md    # Phase 1 output: MCP tool contracts, role matrix
├── checklists/
│   └── requirements.md  # Spec quality checklist (passing)
└── tasks.md              # Phase 2 output (/speckit-tasks — not this command)
```

### Source Code (repository root)

```text
app/
├── skills.py              # NEW: Skill dataclass, skill registry, dispatch
│                           #   (role check -> rate limit -> handler ->
│                           #   audit), per-identity in-process rate limiter.
├── agents.py               # NEW: Agent dataclass, agents/*.yaml loader +
│                           #   validation (every declared skill name must
│                           #   exist in the skills registry).
├── mcpserver.py             # NEW: builds the fastmcp.FastMCP instance,
│                           #   registers one MCP tool per declared skill,
│                           #   role-based tools/list filtering middleware,
│                           #   exposes the ASGI app for mounting.
├── skills_analytics.py      # NEW: concrete skill handlers (ask_question,
│                           #   list_models) registered into app/skills.py's
│                           #   registry at import time.
├── nlq.py                  # + promoted orchestration: start_ask(),
│                           #   handle_decision(), handle_translator_error()
│                           #   (question -> resolve -> execute -> persist
│                           #   -> audit, and the TranslatorError path),
│                           #   moved here from app/api/chat.py's private
│                           #   helpers so both the HTTP route and the new
│                           #   ask_question skill call the same functions —
│                           #   including the not-configured/LLM-failure
│                           #   paths, not just the happy path (research.md R6).
├── config.py                # + CI_MCP_RATE_LIMIT_PER_MIN env var
├── registry.py               # + agents registry (loaded at startup,
│                           #   mirrors registry.models/registry.pipelines)
├── main.py                  # AuthMiddleware path predicate widened to
│                           #   guard /mcp the same as /api; /mcp ASGI app
│                           #   mounted with its lifespan composed into the
│                           #   existing lifespan().
└── api/
    └── chat.py               # unchanged behavior; now calls the promoted
                              #   app/nlq.py functions instead of owning
                              #   the orchestration itself

agents/
└── analytics.yaml            # NEW: the one declared Agent for this
                              #   feature — name, description, skills:
                              #   [ask_question, list_models]

tests/
├── test_skills.py            # NEW
├── test_agents.py            # NEW
└── test_mcp_server.py        # NEW
```

**Structure Decision**: Existing single-service FastAPI layout is kept as
is. This feature adds two new small modules (`app/skills.py`,
`app/agents.py`) following the same shape as `semantic.py`/`pipelines.py`
(a typed model + a YAML loader), one new module (`app/mcpserver.py`) that
plays the same role `app/api/chat.py` plays for the REST surface — an
entry point wired into `app/main.py` — but for the MCP surface instead, and
a `agents/` top-level directory mirroring the existing `models/`,
`dimensions/`, `pipelines/` convention. No new top-level project, no
frontend changes.

## Complexity Tracking

*No Constitution Check violations — table not required.*
