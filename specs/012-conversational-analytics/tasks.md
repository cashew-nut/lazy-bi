# Tasks: Conversational Analytics

**Input**: Design documents from `/specs/012-conversational-analytics/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/chat-api.md, quickstart.md

**Tests**: Included — Principle III ("Every Feature Ships With Tests") is non-negotiable in this project's constitution.

**Organization**: Tasks are grouped by user story (spec.md priorities) so each story is independently implementable and testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Maps to spec.md user stories (US1-US4)

## Path Conventions

Single project (existing `app/` + `tests/` layout, per plan.md's Project Structure section) — no new top-level directories.

---

## Phase 1: Setup

**Purpose**: Dependencies and config for the feature; nothing user-facing yet.

- [ ] T001 Add `anthropic` to `requirements.txt` (research.md R1)
- [ ] T002 [P] Add `CI_LLM_API_KEY` and `CI_LLM_MODEL` (default e.g. `claude-sonnet-5`) to `app/config.py`, following the existing `CI_S3_ENDPOINT`-presence-toggles-behavior pattern (research.md R7)
- [ ] T003 [P] Add `conversational-analytics` env vars to `docker-compose.yml` (pass-through, no default secret) and document them in the root `README.md`'s env-var table

**Checkpoint**: dependency installs, config loads with and without `CI_LLM_API_KEY` set.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The seam every user story is built on — persistence, the translator protocol, and the re-validation core. No user-visible behavior yet, but nothing in Phase 3+ can start without this.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [ ] T004 Create `ConversationStore` in `app/conversationstore.py` — `conversations` + `conversation_messages` tables (schema-on-init `SCHEMA` string, `sqlite3.Row` factory, `self._conn()` pattern copied from `app/store.py`'s `VisualStore`); methods: `list_for_user`, `create`, `get` (owner-scoped, returns `None` if not owned), `update` (title/model_scope), `delete` (owner-scoped, cascades messages), `add_message` (data-model.md `Conversation`/`Message` fields)
- [ ] T005 [P] Register `conversation_store` on `app/registry.py`'s `Registry`, wired up in `app/main.py` lifespan next to the existing `VisualStore`/`AuthStore` wiring
- [ ] T006 [P] Define the `Translator` protocol and `RawToolCall` result types in `app/llm.py`, plus a real `AnthropicTranslator` implementation: builds the tool-use request (three tools: `propose_query`, `ask_clarification`, `decline` — shapes per contracts/chat-api.md) from `(question, catalog, prior_context)` and returns the raw (unvalidated) tool call. Raises a typed `TranslatorError` on API failure/timeout (contracts/chat-api.md's 503 case)
- [ ] T007 Build the model catalog helper in `app/nlq.py`: `build_catalog(models: dict[str, Model], scope: list[str]) -> list[ModelCatalogEntry]`, reusing `semantic.model_to_spec` per model (research.md R4), filtered to `scope` when non-empty
- [ ] T008 Implement `nlq.resolve(question, catalog, prior_context, user) -> Decision` in `app/nlq.py`: calls `Translator.translate`, then re-validates any `propose_query` result against the live `Model` (existence of model/dimensions/measures — reuse `get_model`/`semantic.ModelError` exactly as `app/api/query.py` does) before returning a `ProposeQuery`/`AskClarification`/`Decline` `Decision` (contracts/chat-api.md's internal contract; research.md R2)
- [ ] T009 [P] Write `FakeTranslator` (implements the `Translator` protocol with scripted responses, no network) in `tests/test_nlq.py`'s fixtures, for reuse by both `test_nlq.py` and `test_chat_api.py`

**Checkpoint**: `ConversationStore` CRUD and `nlq.resolve`'s re-validation logic are unit-testable in isolation with zero network calls. Nothing is wired to HTTP yet.

---

## Phase 3: User Story 1 - Ask a plain-language question and get a grounded answer (Priority: P1) 🎯 MVP

**Goal**: A signed-in user asks an unambiguous question and gets a natural-language answer plus the exact grounding table/number, scoped to their own role.

**Independent Test**: Sign in as viewer, `POST /api/conversations`, `POST /api/conversations/{id}/ask` with an unambiguous question, confirm the response's `resolved_query` + `result` match a hand-built equivalent call to `POST /api/query`.

### Tests for User Story 1

- [ ] T010 [P] [US1] `tests/test_nlq.py`: `nlq.resolve` returns `ProposeQuery` for an unambiguous `FakeTranslator` response, and the returned `Decision` matches the resolved model/dimensions/measures
- [ ] T011 [P] [US1] `tests/test_nlq.py`: `nlq.resolve` rejects (converts to `Decline`, does not raise) a `FakeTranslator` `propose_query` that names a dimension/measure not present in the live `Model` — proves re-validation isn't just trusting the LLM (research.md R2)
- [ ] T012 [P] [US1] `tests/test_chat_api.py`: `POST /api/conversations` → `POST .../ask` (viewer_client, unambiguous question via `FakeTranslator` override) → 200, `response.outcome == "answered"`, `result` present and matches a direct `POST /api/query` call with the same resolved shape
- [ ] T013 [P] [US1] `tests/test_chat_api.py`: two different role clients (`viewer_client`, `admin_client`) asking the same question each get results identical to what their role already gets from `/api/query` — no elevation (FR-005, spec's Acceptance Scenario 3)
- [ ] T014 [P] [US1] `tests/test_chat_api.py`: with `CI_LLM_API_KEY` unset, every `/api/conversations*` route returns 503 (contracts/chat-api.md)

### Implementation for User Story 1

- [ ] T015 [US1] Create `app/api/chat.py`: `GET/POST /api/conversations`, `GET/PATCH/DELETE /api/conversations/{id}` — all `require_role("viewer")` + owner-scoped (404 on not-owned, per contracts/chat-api.md), all gated by `CI_LLM_API_KEY` presence (503 otherwise)
- [ ] T016 [US1] `POST /api/conversations/{id}/ask` in `app/api/chat.py`: persists the `user` message, calls `nlq.build_catalog` + `nlq.resolve`, on `ProposeQuery` executes via the existing `engine.run_query`, persists the `assistant` message (`outcome: answered` or `answered_empty` per FR-010, using `resolved_query`+`result`), generates `answer_text` (a second, smaller LLM call or templated summary — implementer's choice, documented in code), returns `{question, response}` per contracts/chat-api.md
- [ ] T017 [US1] Wire `chat` router into `app/api/__init__.py`
- [ ] T018 [US1] Audit logging: `chat_ask` event (question text, resolved model/dimensions/measures, outcome, user) via the existing audit-log mechanism used by `measure_provenance`/spec 011 (FR-012)
- [ ] T019 [US1] `app/static/js/chat.js`: conversation list + new-conversation button, message thread rendering (question, answer text, grounding result table using existing table-render helpers from `builder.js`/`lib.js`), ask input box
- [ ] T020 [US1] Wire a **Chat** entry into `app/static/js/main.js`'s mode-nav and `app/static/index.html`'s nav/view containers, hidden when `GET /api/conversations` first call 503s (config-off path, research.md R7)

**Checkpoint**: User Story 1 fully functional — an unambiguous question round-trips through chat with visible grounding data, independently of clarification/decline/follow-up handling.

---

## Phase 4: User Story 4 - Assistant declines what the semantic layer can't answer (Priority: P1)

**Goal**: Out-of-scope questions (raw columns, undeclared joins, code/SQL injection attempts) are refused honestly, never executed.

**Independent Test**: Ask for a metric no model declares, and separately a prompt-injection attempt ("run raw SQL"), confirm both produce `outcome: "declined"` with no query executed and no data-access-layer call made.

**Note**: Sequenced right after US1 (both P1) since the safety boundary should ship before any richer conversational behavior (follow-ups/clarification) that only makes sense once basic answer/decline both work.

### Tests for User Story 4

- [ ] T021 [P] [US4] `tests/test_nlq.py`: `FakeTranslator` returns `decline`; `nlq.resolve` passes it through as `Decline` untouched, no query executed
- [ ] T022 [P] [US4] `tests/test_nlq.py`: `FakeTranslator` returns a `propose_query` referencing a model the requesting `User`'s role can't reach; `nlq.resolve` converts it to `Decline`, mirroring the direct-query 403/404 behavior (spec Acceptance Scenario US4.3)
- [ ] T023 [P] [US4] `tests/test_chat_api.py`: `ask` with a scripted `FakeTranslator` decline → 200, `response.outcome == "declined"`, `resolved_query`/`result` both null, and assert (via a spy on `engine.run_query`) that the query engine was never called for that turn

### Implementation for User Story 4

- [ ] T024 [US4] Ensure `nlq.resolve`'s re-validation path (already built in T008) covers every decline trigger from spec's Edge Cases (undeclared cross-model join, role-inaccessible model, malformed tool call) — extend `nlq.py`, not `llm.py`, since these are all post-LLM safety checks
- [ ] T025 [US4] `app/api/chat.py`/`chat.js`: render `outcome: "declined"` distinctly from `outcome: "answered_empty"` in both the API response shape and the chat UI (FR-010's "must not look the same" requirement)

**Checkpoint**: The trust boundary (Principle I/VI) is provably enforced — US1 and US4 together are a safe, shippable MVP even without follow-ups or clarification.

---

## Phase 5: User Story 3 - Assistant asks a clarifying question instead of guessing wrong (Priority: P2)

**Goal**: Genuinely ambiguous questions produce a real clarifying question naming actual candidates, not a guess.

**Independent Test**: Ask a question matching entities in more than one model/measure, confirm a clarifying question is returned (`outcome: "clarification"`, no query executed) naming real candidates, then confirm answering it proceeds correctly without re-asking.

### Tests for User Story 3

- [ ] T026 [P] [US3] `tests/test_nlq.py`: `FakeTranslator` returns `ask_clarification` with candidate names; `nlq.resolve` validates the candidates are real model/dimension/measure names (drops/flags any that aren't) and returns `AskClarification`
- [ ] T027 [P] [US3] `tests/test_chat_api.py`: `ask` → clarification response (`outcome: "clarification"`) → a second `ask` call in the same conversation answering it → `nlq.resolve` receives the clarification as `prior_context` and the second call's `FakeTranslator` response is used as final `propose_query`, producing `outcome: "answered"`

### Implementation for User Story 3

- [ ] T028 [US3] `app/api/chat.py`: `ask` persists `role: "clarification"` messages distinctly from `role: "assistant"` (data-model.md), and includes the most recent clarification exchange in `prior_context` built for the next `ask` call in that conversation
- [ ] T029 [US3] `chat.js`: render a clarification turn distinctly (e.g. highlighted candidate chips) from a normal answer

**Checkpoint**: Ambiguous questions are handled safely; combined with US1/US4 this covers 3 of 4 user stories end-to-end.

---

## Phase 6: User Story 2 - Ask a follow-up question that refines the previous one (Priority: P2)

**Goal**: Short follow-ups reuse prior-turn context (model/dimensions/measures/filters) and are re-validated fresh each time, per research.md R5/FR-009.

**Independent Test**: Ask an initial question, then a follow-up that only makes sense given the first; confirm the second answer reuses the first's model and correctly applies the follow-up's adjustment. Then ask an unrelated follow-up and confirm it's treated as a fresh question.

### Tests for User Story 2

- [ ] T030 [P] [US2] `tests/test_nlq.py`: `nlq.resolve` given `prior_context` (a prior resolved query) and a `FakeTranslator` response that only overrides one field (e.g. dimension) — confirm the resulting `Decision` merges correctly and is still independently re-validated (not exempted from T008's checks)
- [ ] T031 [P] [US2] `tests/test_nlq.py`: `prior_context` referencing a model/dimension that no longer exists (simulating a model changed since the prior turn) → `nlq.resolve` does not blindly reuse it, produces `Decline` or fresh `AskClarification` rather than executing against stale context (FR-009, spec Edge Case)
- [ ] T032 [P] [US2] `tests/test_chat_api.py`: two sequential `ask` calls in one conversation — second is a short follow-up — confirm `prior_context` passed to the translator includes the first turn's resolved query, and the final `resolved_query` reflects the merge

### Implementation for User Story 2

- [ ] T033 [US2] `app/api/chat.py`: build `prior_context` for each `ask` call from the conversation's recent messages (resolved query structs only, per research.md R5 — never raw result rows) and pass it into `nlq.resolve`
- [ ] T034 [US2] `chat.js`: conversation thread visually reads as continuous turns (no need to re-select a model/scope between turns)

**Checkpoint**: All 4 user stories now work together as a coherent conversation loop.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Rounds out FR-014/FR-015 details and the persistence/role edge cases from spec.md not already covered by a single story's tests, plus the browser-verified pass required by Principle IV.

- [ ] T035 [P] Explicit model-scope selection: `POST /api/conversations` `model_scope` validation (400 on unknown model name) + `chat.js` model-picker UI (FR-014, research.md R6)
- [ ] T036 [P] `tests/test_chat_api.py`: conversation list/get/delete are strictly owner-scoped — a second user's `GET /api/conversations/{id}` for the first user's conversation returns 404, and `GET /api/conversations` never includes another user's rows (FR-013)
- [ ] T037 [P] README: add the "Conversational analytics" data-egress subsection (research.md R7) documenting exactly what is sent to the third-party LLM provider and how to disable the feature entirely
- [ ] T038 Run `quickstart.md` end-to-end in a real browser (all 18 steps) — zero console errors, persistence across reload, role scoping, config-off path all verified (Principle IV)
- [ ] T039 Full `pytest tests/` run green, including the new `test_nlq.py`/`test_chat_api.py` suites alongside the existing suites

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: depends on Setup — blocks every user story
- **US1 (Phase 3)** and **US4 (Phase 4)**: both P1, depend only on Foundational; US4 is sequenced second only because "answer" needs to exist before "decline" is worth demoing, but their code touches largely disjoint paths in `nlq.resolve` and could be built in parallel by two people
- **US3 (Phase 5)**: depends on Foundational; independent of US1/US4 in code, sequenced after them because clarification is only useful once the answer/decline loop exists to clarify *into*
- **US2 (Phase 6)**: depends on Foundational and benefits from US3 existing (a clarification exchange is itself part of `prior_context`), so sequenced last
- **Polish (Phase 7)**: depends on all four stories being complete

### Parallel Opportunities

- T001-T003 (Setup) in parallel
- T005, T006 (Foundational) in parallel once T004 lands; T007-T009 depend on T006
- Within each story's test tasks, all `[P]`-marked tasks touch only `tests/test_nlq.py` or `tests/test_chat_api.py` fixtures/cases and can be written in parallel, then run together
- US1 and US4 implementation can proceed in parallel after Phase 2 (different reviewers/sessions), since T024 only extends `nlq.py` functions T008 already created without touching US1's `chat.py` additions — coordinate on `app/api/chat.py` if working simultaneously to avoid overlapping edits

---

## Implementation Strategy

### MVP First

1. Phase 1 (Setup) → Phase 2 (Foundational) → Phase 3 (US1). **Stop and validate**: an unambiguous question round-trips with grounding data.
2. Add Phase 4 (US4) immediately after — the safety boundary is what makes US1 shippable rather than a toy. **US1 + US4 together are the recommended minimum ship** (both P1 in spec.md).

### Incremental Delivery

3. Add Phase 5 (US3, clarification) → validate ambiguous-question handling.
4. Add Phase 6 (US2, follow-ups) → validate multi-turn conversation.
5. Phase 7 (Polish) → full quickstart.md pass, README data-egress doc, full test suite green.

### Notes

- No task touches `app/engine.py`, `app/semantic.py`, or existing `/api/query.py` — this feature is additive only, per Constitution Principle I/VI (plan.md's Constitution Check).
- Commit after each task or logical group; stop at any Checkpoint to validate a story independently before continuing.
