# Implementation Plan: Conversational Analytics

**Branch**: `claude/roadmap-prioritization-6g39f1` | **Date**: 2026-07-13 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/012-conversational-analytics/spec.md`

## Summary

Add a chat surface where a signed-in user asks natural-language business
questions and gets a grounded answer. A new `app/nlq.py` translation core
turns each question (plus prior-turn context) into a *proposal* — a
structured `propose_query` / `ask_clarification` / `decline` decision — by
calling a third-party LLM (Anthropic Messages API, tool-use) with a catalog
built live from `registry.models`. Every `propose_query` proposal is then
**re-validated server-side against the real semantic model and the caller's
role** and executed through the existing `engine.run_query`, exactly as
`/api/query` does today — the LLM never gets a data-access path of its own,
it only ever fills in the same `QueryRequest` shape a human fills in via the
query builder. Conversations and turns persist per-user in SQLite behind a
new `ConversationStore`, mirroring the existing `VisualStore` pattern. A new
`app/api/chat.py` router exposes conversation CRUD + "ask" under existing
role gates, and a new vanilla-ES-module `chat.js` view is added to the SPA.
This translation core (question + prior context → propose/clarify/decline)
is built as a standalone, model-agnostic function so the future
prompt-to-dashboard (3.2) and dashboard-analyst (3.3) roadmap items can call
it directly instead of re-deriving it.

## Technical Context

**Language/Version**: Python 3.10+ (Docker image: python:3.12-slim), same as existing.

**Primary Dependencies**: FastAPI/Starlette, Polars (unchanged); **new**:
`anthropic` (Messages API + tool-use, see research.md R1) for NL-to-query
translation. Frontend remains vanilla ES modules, no build step.

**Storage**: existing SQLite database (`cash_intel.db`) — new tables
`conversations`, `conversation_messages` (mirrors `VisualStore`'s
`self._conn()` / schema-on-init pattern). No new datastore introduced.

**Testing**: pytest + FastAPI TestClient (existing pattern). The LLM call is
behind a narrow `Translator` seam (research.md R2) so tests inject a fake
translator that returns scripted propose/clarify/decline decisions —
no real network calls in the test suite, matching how `AuthStore` is
already swappable.

**Target Platform**: Linux server / Docker, single uvicorn worker (unchanged
— this feature adds one outbound HTTPS call per turn, no new local
concurrency source).

**Project Type**: web service + static SPA (existing structure).

**Performance Goals**: SC-001 — under 30s from question to grounded answer
for an unambiguous question; the query-engine portion of that budget is
unchanged from `/api/query` today (sub-second), so the budget is
effectively "one LLM round trip" (typically 1-5s) plus one query-engine
call.

**Constraints**: single SQLite writer (unchanged); no new data-access path —
`nlq.py` never touches `engine.scan`/S3 directly, only produces a
`QueryRequest`-shaped proposal that is validated and executed exactly like
today's `/api/query`; the LLM call is the only new network egress this
feature introduces, and what it sends must be documented (FR-015, see
research.md R7).

**Scale/Scope**: internal-tool scale — one new store module, one new router,
one new translation module, ~2-3 new frontend modules; no changes to the
existing query engine or semantic layer parsing.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Status | Notes |
|---|-----------|--------|-------|
| I | Semantic layer is the only contract | PASS | The LLM never queries data directly. It produces a proposal that is re-validated against the live `Model` (real dimensions/measures) and executed only through `engine.run_query`, identically to the existing `/api/query` path. A proposal referencing anything not declared in the model is rejected before execution, not "trusted because the LLM said so." |
| II | Lazy evaluation, pushdown | PASS | No change to `engine.scan`/`run_query`; conversational queries take the same lazy Polars path as the query builder. |
| III | Every feature ships with tests | PASS (planned) | New `tests/test_nlq.py` (translator contract, using a fake LLM client), `tests/test_chat_api.py` (conversation CRUD + ask flow, role scoping), extending the audit-log assertions from spec 011. |
| IV | Browser-verified before done | PASS (planned) | quickstart.md defines the browser golden path: ask → grounded answer → follow-up → ambiguous question → clarification → out-of-scope question → decline → reload → conversation history persists. |
| V | Ephemeral vs persisted is deliberate | PASS | Persisted: conversations + turns (FR-013), in SQLite, per-user. Ephemeral: in-progress (not-yet-sent) chat input text, like other unsaved form state elsewhere in the app. Documented in data-model.md. |
| VI | Trusted-config boundary explicit | PASS — no widening | This feature does not touch the `frame:`/eval path or model YAML mutation at all; it only ever *reads* the model to build the LLM's catalog and to validate proposals, and *runs* the existing read-only query path. It introduces a new **external** trust boundary (a third-party LLM sees schema + question text + optionally result values) — that is new, but it is a data-egress boundary, not the eval-capable-config boundary principle VI governs, so no amendment is needed; it is called out explicitly in research.md R7 and documented per FR-015. |
| VII | Feature branch, PR merge | PASS | All work on `claude/roadmap-prioritization-6g39f1`. |
| — | Technology constraints | PASS | One router per resource (`app/api/chat.py`); SQLite remains the persistence store (not a data source); vanilla ES modules, no bundler; single worker preserved (LLM calls are outbound HTTP, not a second local writer). |

No violations → Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/012-conversational-analytics/
├── spec.md              # Feature spec (clarified)
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── chat-api.md      # Phase 1 output: endpoints, role matrix, translator contract
├── checklists/
│   └── requirements.md  # Spec quality checklist (passing)
└── tasks.md             # Phase 2 output (/speckit-tasks — not this command)
```

### Source Code (repository root)

```text
app/
├── nlq.py                # NEW: translation core — question + catalog +
│                          #   prior-turn context -> {propose_query |
│                          #   ask_clarification | decline}; server-side
│                          #   re-validation of any propose_query against
│                          #   the live Model before it's ever executed.
│                          #   Model-agnostic entry point future 3.2/3.3
│                          #   features call directly (no HTTP hop needed).
├── llm.py                 # NEW: thin Translator seam — the one place that
│                          #   talks to the Anthropic Messages API (tool
│                          #   use). Swappable for tests / future providers.
├── conversationstore.py   # NEW: ConversationStore — conversations +
│                          #   messages tables on the same SQLite file,
│                          #   same self._conn()/schema-on-init pattern as
│                          #   VisualStore.
├── config.py              # + CI_LLM_API_KEY, CI_LLM_MODEL env vars
├── registry.py             # + conversation_store handle
└── api/
    ├── __init__.py         # + chat router
    └── chat.py              # NEW: conversation CRUD + POST .../ask,
                              #   role-gated (viewer+, per FR-005)

app/static/
├── index.html              # + chat nav entry + chat view container
├── js/
│   ├── chat.js              # NEW: conversation list, message thread,
│   │                        #   ask/clarify/decline rendering, grounding
│   │                        #   table display, explicit model-scope picker
│   └── main.js              # + chat mode wired into existing mode-nav
└── style.css                # chat view styling

tests/
├── test_nlq.py              # NEW: translator contract via fake LLM client
│                              #   (propose/clarify/decline, re-validation
│                              #   rejects out-of-schema proposals)
└── test_chat_api.py          # NEW: conversation CRUD, ask flow, role
                               #   scoping, persistence-across-reload
```

**Structure Decision**: stay inside the existing single-app layout — one new
store module (`conversationstore.py`, same shape as `store.py`'s
`VisualStore`), one new router under `app/api/` (per-resource-router rule),
the NL-to-query translation logic split into a swappable LLM-client seam
(`llm.py`) and a model-agnostic decision core (`nlq.py`) so it has exactly
one reason to change and is directly reusable by 3.2/3.3 later, and the
frontend grows one new vanilla ES module plus a `main.js` wiring change. No
new packages, services, or build steps.

## Complexity Tracking

No constitution violations to justify.
