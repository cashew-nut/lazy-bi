# Contract: MCP Server (`/mcp`)

Streamable HTTP MCP endpoint, mounted alongside the existing REST API.
Authenticated identically to `/api` (session cookie or `Authorization:
Bearer` per-user token — same carriers, see spec 011's
`contracts/auth-api.md`) — **no anonymous MCP handshake**: `AuthMiddleware`
guards `/mcp` exactly like `/api`, so even `initialize` requires valid
credentials. A connection with no/invalid credentials gets the same 401 an
unauthenticated `/api` call gets today, before any MCP protocol exchange
happens.

## Discovery: `tools/list`

Standard MCP protocol method. Returns only the tools whose skill's
`min_role` the connection's authenticated identity satisfies — a viewer
never sees an author/admin-only tool in the list (not just "sees it and
gets refused on call"). For this feature's MVP, both declared skills are
viewer-tier, so every authenticated identity sees the same two tools:
`ask_question`, `list_models`.

A tool's declared MCP schema is exactly the skill's `input_schema`
(data-model.md); its description is the skill's `description`.

## Tool: `ask_question` — viewer+

Ask a business question in plain language; get back a grounded, structured
answer or a clarification/decline, exactly like the browser's
conversational-analytics chat.

**Input**:
```json
{
  "question": "string, required",
  "conversation_id": "integer, optional — omit to start a new conversation"
}
```

**Behavior**:
1. Rate-limit check (research.md R3) — a caller over `CI_MCP_RATE_LIMIT_PER_MIN`
   gets `outcome: "rate_limited"` immediately, no LLM call made.
2. LLM-configured check — if `config.LLM_ENABLED` is false (no
   `CI_LLM_API_KEY`), returns `outcome: "error"` immediately, same message
   class as the existing chat 503 ("conversational analytics is not
   configured"), no translator ever constructed. This is the skill-side
   equivalent of the HTTP route's `Depends(_require_enabled)` gate — not
   reusable as-is (that gate raises an `HTTPException`), so it's
   reimplemented as a plain check here rather than promoted.
3. If `conversation_id` given, must belong to the calling identity (404
   equivalent — a typed error result — otherwise, exactly like
   `/api/conversations/{id}` today never leaking another user's
   conversation existence). If omitted, a new conversation is created
   for the caller.
4. The question is resolved via the existing `app/nlq.py` translation core
   (unchanged: builds the live catalog, calls the configured LLM
   translator, re-validates any `propose_query` against the live semantic
   model before executing it), reproducing the HTTP route's own three-step
   structure — `start_ask()` → `nlq.resolve()` wrapped in
   `try/except TranslatorError: return handle_translator_error(...)` → 
   `handle_decision()` on success — so a real LLM-call failure (bad key,
   network error, timeout) also resolves to `outcome: "error"` rather than
   an unhandled exception, exactly as `POST /api/conversations/{id}/ask`
   already does today (same promoted orchestration functions, see plan.md).
   The outcome is persisted + audited identically either way.

**Output** (data-model.md's `ask_question output`):
```json
{
  "conversation_id": 42,  // null only if blocked before any conversation was touched (not configured, rate limited)
  "question": { "role": "user", "question_text": "..." },
  "response": {
    "role": "assistant",
    "outcome": "answered | answered_empty | clarification | query_shown | declined | error | rate_limited",
    "answer_text": "string",
    "resolved_query": { "...": "present for answered*/query_shown" },
    "result": { "...": "present for answered*, same shape as POST /api/query" }
  },
  "learned": [ "...same shape chat's self-learning loop already produces" ]
}
```

**Errors**: LLM backend not configured (`CI_LLM_API_KEY` unset) →
`outcome: "error"`, same message class as the existing chat 503 today,
translated into a typed result rather than a bare protocol error, so a
calling agent can read *why* rather than just seeing a failed call.

## Tool: `list_models` — viewer+

List the models, dimensions, and measures the caller may query — the same
catalog the LLM itself is grounded on for `ask_question`.

**Input**: `{}` (no arguments).

**Output**: array of model catalog entries (data-model.md's `list_models
output`) — name, label, description, dimensions (name, type, description,
synonyms, sample values where declared), measures (name, label,
description, synonyms, formula where non-framed), learned notes.

## Role matrix

| Tool | min role | Rate-limited (LLM-backed) |
|---|---|---|
| `ask_question` | viewer | yes |
| `list_models` | viewer | no |

No author/admin-only skill exists in this feature (spec.md FR-011: read/
query-only scope) — the role-gating machinery is exercised end-to-end by
tests (`tests/test_skills.py`) against a synthetic higher-role skill so the
mechanism itself is proven even though the MVP's two real skills don't need
it, per plan.md's Constitution Check (Principle III).

## Audit

Every call to either tool — success, decline, clarification, role denial,
or rate-limit denial — is recorded via `AuthStore.record_audit` (see
data-model.md, "Skill Invocation"). No browsing API for this feature (same
posture as the existing `audit_events` table today — append-only, no
`/api` route exposes it).
