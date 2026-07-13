# Data Model: Conversational Analytics

## Persisted entities (SQLite, via `app/conversationstore.py`)

### Conversation

| Field | Type | Notes |
|---|---|---|
| `id` | integer, PK | autoincrement |
| `user_id` | integer, FK → `users.id` | owner; every read/write scoped to this (FR-013) |
| `title` | text | derived from the first question, editable later (parity with dashboard naming) |
| `model_scope` | JSON list of strings | model names this conversation is pinned to; `[]` = auto-infer across every model the user can access (FR-014, research.md R6) |
| `created_at` | text (ISO 8601 UTC) | |
| `updated_at` | text (ISO 8601 UTC) | bumped on every new message |

**Validation rules**: `model_scope` entries, if present, must name models
that currently exist in `registry.models` at write time; a stale scope
entry (model deleted since) is dropped silently on next read rather than
erroring the whole conversation.

**Lifecycle**: created on the first question a user asks (no separate
"new conversation" step required, though the UI also exposes one, R6);
deleted only by its owner (or admin) — deletion cascades to its messages.

### Message (a single turn's user-question OR assistant-response record)

| Field | Type | Notes |
|---|---|---|
| `id` | integer, PK | autoincrement |
| `conversation_id` | integer, FK → `conversations.id` | |
| `role` | text enum: `user` \| `assistant` \| `clarification` | `clarification` = assistant asked a disambiguating question instead of answering (User Story 3) |
| `question_text` | text, nullable | the raw text this turn is responding to (null for a pure follow-up answer that only carries `answer_text`, if ever split — v1 always pairs one `user` row with one `assistant`/`clarification` row) |
| `resolved_query` | JSON, nullable | the validated `{model, dimensions, measures, filters, sort, limit}` actually executed; null for `clarification` and `decline` outcomes (FR-002, FR-004) |
| `result` | JSON, nullable | the exact table/number returned by `engine.run_query` for this turn, capped at `MAX_ROWS`; null unless `resolved_query` is set (FR-004, FR-010) |
| `outcome` | text enum: `answered` \| `answered_empty` \| `clarification` \| `declined` \| `error` | distinguishes "ran, no matching rows" from "could not be answered" (FR-010) and from execution failure (FR-011) |
| `answer_text` | text | natural-language text shown to the user for this turn — the answer, the clarifying question, the decline explanation, or the error message |
| `created_at` | text (ISO 8601 UTC) | |

**Validation rules**: `resolved_query` MUST be set if and only if
`outcome` is `answered` or `answered_empty`. `resolved_query`, when present,
must reference a model/dimensions/measures that exist in the current
`Model` at write time (server-side re-validation already guarantees this
before persistence — see research.md R2).

**State transitions**: none post-write — a message row is an immutable
record of what happened on that turn (matches audit-log style already used
for `measure_provenance`, FR-012). Corrections happen by asking a new
follow-up question, not by editing history.

## Ephemeral (not persisted) state

- The chat input box's in-progress, not-yet-sent text — cleared/lost on
  reload like any other unsaved form field elsewhere in the app
  (Principle V).
- The "assistant is thinking" pending-request UI state for a turn that
  hasn't returned yet.

## Non-persisted, request-scoped shapes (not stored, just documented for contracts)

### Catalog (built fresh per `ask` call — research.md R4)

For each in-scope model: model name + description, and for each declared
dimension/measure: name, type, description. Never raw source
columns/paths/credentials — only what `semantic.model_to_spec` already
exposes to the existing `/api/models` endpoint.

### Translator decision (the LLM tool-use result — research.md R1/R2)

One of:
- `propose_query`: `{model, dimensions, measures, filters, sort, limit}` —
  same shape as the existing `QueryRequest` (`app/api/query.py`).
- `ask_clarification`: `{question_text, candidates: [...]}` — candidates
  name the real model/dimension/measure options under consideration.
- `decline`: `{reason_text}`.

This is an internal contract between `app/llm.py` and `app/nlq.py`, not a
persisted entity — see `contracts/chat-api.md` for the full shape and the
server-side re-validation step every `propose_query` goes through before it
can ever become a `resolved_query`.
