# API Contract: Conversational Analytics

All endpoints are JSON under `/api`, authenticated (session cookie or
bearer token — same carriers as every other route, see spec
011's `contracts/auth-api.md`). No new public/allowlisted routes. Every
route here requires at least the **viewer** role — asking a question is a
read-only action, same tier as `POST /api/query` (FR-005). Errors use the
existing `{"detail": "..."}` shape; validation failures reuse
`semantic.ModelError` → 400, unknown model → 404, exactly like
`/api/query`.

If `CI_LLM_API_KEY` is not configured, every route below returns
**503** `{"detail": "conversational analytics is not configured"}` —
the feature is off, not silently degraded (research.md R7).

## Endpoints (`app/api/chat.py`)

### GET /api/conversations — viewer+
List the caller's own conversations, most-recently-updated first.
- 200: `[ConversationSummary]` — `{id, title, model_scope, updated_at}`.
  Never another user's conversations (FR-013).

### POST /api/conversations — viewer+
Create an empty conversation.
Request: `{"model_scope"?: [string]}` (default `[]` = auto-infer, FR-014).
- 201: `Conversation` — `{id, title: "", model_scope, created_at, updated_at, messages: []}`.
- 400 if `model_scope` names a model that doesn't exist.

### GET /api/conversations/{id} — viewer+, owner only
- 200: `Conversation` with full `messages: [Message]` in order.
- 404 if the conversation doesn't exist **or** isn't owned by the caller
  (existence is not leaked across users).

### PATCH /api/conversations/{id} — viewer+, owner only
Request (any subset): `{"title"?, "model_scope"?}`.
- 200: updated `Conversation` (messages unchanged).

### DELETE /api/conversations/{id} — viewer+, owner only
- 204. Cascades to its messages.

### POST /api/conversations/{id}/ask — viewer+, owner only
The core action: ask a question in this conversation.
Request: `{"question": string}`.
- 200: `Message` (the new `assistant`/`clarification` row, plus the `user`
  row that was created for the question — returned as
  `{"question": Message, "response": Message}`).
- 400: the question produced a proposal that failed re-validation against
  the current model (e.g. model changed since a follow-up's prior context
  was set) — surfaced as `outcome: "error"` in the persisted `response`
  message, not a bare HTTP 400, so the failure is visible in conversation
  history (FR-011).
- 503: LLM call itself failed/timed out — same `outcome: "error"` shape.
- Every call appends exactly one `user` message and exactly one
  `assistant`/`clarification` message. Audit: `chat_ask` (question text,
  resolved model/dimensions/measures if any, outcome) — FR-012.

## Internal contract: `app/nlq.py` ↔ `app/llm.py` (not HTTP — in-process)

This is the seam future features (3.2 prompt-to-dashboard, 3.3
dashboard-analyst) call directly instead of going through `/api/chat.py`.

```
nlq.resolve(
    question: str,
    catalog: list[ModelCatalogEntry],       # research.md R4
    prior_context: list[PriorTurn],         # research.md R5, may be []
    user: User,
) -> Decision
```

`Decision` is exactly one of:

- `ProposeQuery(model, dimensions, measures, filters, sort, limit)` — the
  same shape as `app/api/query.py`'s `QueryRequest`. `nlq.resolve` has
  **already** re-validated this against the live `Model` (existence of
  model/dimensions/measures, per-role access) before returning it — a
  caller of `nlq.resolve` never needs to re-check semantic validity itself,
  only execute it via `engine.run_query` and handle engine-level failures
  (FR-011).
- `AskClarification(question_text, candidates: list[str])` — candidates are
  real model/dimension/measure names, never invented.
- `Decline(reason_text)`.

`app/llm.py`'s `Translator` protocol is the only thing that talks to the
Anthropic API:

```
Translator.translate(
    question: str,
    catalog: list[ModelCatalogEntry],
    prior_context: list[PriorTurn],
) -> RawToolCall   # propose_query | ask_clarification | decline, unvalidated
```

`nlq.resolve` calls `Translator.translate` then performs the re-validation
step (research.md R2) before constructing a `Decision`. Tests substitute a
`FakeTranslator` implementing the same protocol with scripted responses —
`nlq.resolve`'s re-validation logic is exercised without any network call.
