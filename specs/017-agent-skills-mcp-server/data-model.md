# Phase 1 Data Model: Agent & Skills Framework with MCP Server

No new SQLite tables (research.md R4). This document covers the new
in-process types and the existing persisted shapes they read/write.

## Skill (`app/skills.py`, in-process, code + registered at import time)

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Unique registry key; also the MCP tool name (e.g. `ask_question`). |
| `description` | `str` | Shown to MCP clients in `tools/list`. |
| `min_role` | `"viewer"\|"author"\|"admin"` | Reuses `app.auth.ROLE_ORDER`; enforced identically to `require_role()` on existing `/api` routes. |
| `input_schema` | `dict` (JSON Schema) | Passed straight through as the MCP tool's input schema. |
| `output_schema` | `dict` (JSON Schema, documentation only) | Describes the discriminated-union shape each skill returns; not enforced by the protocol, validated by tests. |
| `rate_limited` | `bool` | `True` only for skills that call the LLM backend (MVP: `ask_question`). |
| `handler` | `Callable[[User, dict], dict]` | Does the work; re-validates its own input against live platform state before acting (FR-007) — the dispatch wrapper does not trust `input_schema` validation alone. |

Invariant: a `Skill`'s own `handler` never trusts caller-declared IDs/names
without re-checking them against `registry.models`/live state — the same
rule `nlq.resolve()` already enforces for LLM-declared `propose_query`
input, just generalized to every skill.

## Agent (`app/agents.py`, declared in `agents/*.yaml`)

```yaml
# agents/analytics.yaml
name: analytics
label: Analytics Agent
description: >
  Ask business questions in plain language against the platform's
  declared semantic models and get back grounded, structured answers.
skills:
  - ask_question
  - list_models
```

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Unique; the id an external client sees. |
| `label` | `str` | Display name. |
| `description` | `str` | Shown to MCP clients. |
| `skills` | `list[str]` | Must all exist in the `app/skills.py` registry — validated at load (mirrors `semantic.py`'s YAML validation raising a clear error, not a silent skip, for an unknown reference). |

Loaded once at startup by `registry.init()` into `registry.agents: dict[str,
Agent]`, the same pattern already used for `registry.models` and
`registry.pipelines`. An `Agent` carries **no privilege of its own** — it
is purely a discoverable grouping; every skill call is still gated by that
skill's own `min_role` against the calling identity's actual role,
regardless of which agent's declaration listed it (Key Entities, spec.md).

## MCP Connection

Not a persisted entity — it is the authenticated identity already resolved
by the (widened) `AuthMiddleware` for every request to `/mcp`, exactly the
same `User` object (`app.auth.User`) every `/api` route already receives
as `request.state.user`. A `fastmcp` server middleware reads it via
`get_http_request().state.user` inside `on_call_tool`/`on_list_tools` and
threads it into the skill dispatch call — no separate MCP-specific
identity/session concept is introduced.

## Skill Invocation (audit — reuses `authstore.audit_events`, no new table)

Every skill call, after role/rate-limit checks pass, is recorded via the
existing `AuthStore.record_audit(action, actor_label, actor_user_id,
target)`:

| Column | Value for a skill invocation |
|---|---|
| `action` | `"mcp_skill:<skill_name>"`, e.g. `"mcp_skill:ask_question"`. |
| `actor_user_id` / `actor_label` | The calling identity, exactly as every other audited action already records it. |
| `target` | Skill-specific summary string (e.g. for `ask_question`: `conversation:<id> outcome:<outcome> question:<text>` — the same target shape `chat_ask` already writes today, since `ask_question` calls the same promoted `app/nlq.py` orchestration that produces it). |

A role-check failure or a rate-limit rejection is **not** silently
dropped either — both are audited too (`action` suffixed `:denied` /
`:rate_limited`) so an admin can see attempted-but-blocked access, not just
successful calls.

## `ask_question` output (discriminated union — no new shape)

Identical to what `/api/conversations/{id}/ask` already returns today
(`{"question": Message, "response": Message, "learned": [...]}`), with one
addition: `conversation_id` is always present in the top-level result (the
existing HTTP route gets it from the URL path; an MCP caller may have
omitted it, so the skill echoes back whichever id was used — caller-given
or freshly auto-created — so a follow-up call can pass it back for
multi-turn context). `Message.outcome` is one of: `answered`,
`answered_empty`, `clarification`, `query_shown`, `declined`, `error`,
`rate_limited` (new — see research.md R3; not producible by the existing
HTTP route, only by the new rate-limit gate ahead of the LLM call).

## `list_models` output (no new shape)

The same `ModelCatalogEntry` list `nlq.build_catalog()` already produces
for the LLM's own prompt (name, label, description, dimensions with
type/description/synonyms/sample values, measures with
label/description/synonyms/formula, learned notes), serialized to JSON —
uniform across every role since model access is not itself role-scoped
today (only write actions are).
