# Tasks: Agent & Skills Framework with MCP Server

**Input**: Design documents from `/specs/017-agent-skills-mcp-server/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/mcp-server.md, quickstart.md (all present)

**Tests**: Included as first-class tasks — Constitution Principle III ("Every Feature Ships With Tests") is non-negotiable for this project, not optional.

**Organization**: Tasks are grouped by user story (spec.md: US1 ask a question, P1; US2 discover what's available, P2; US3 govern skills declaratively, P3) so each is independently implementable, testable, and deployable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: Maps the task to US1/US2/US3 from spec.md — Setup/Foundational/Polish tasks carry no story label
- Every task names its exact file path

## Path Conventions

Single-project FastAPI backend (existing layout) — `app/`, `agents/`, `tests/` at repository root. No frontend changes in this feature.

---

## Phase 1: Setup

**Purpose**: New dependency and config, no behavior yet.

- [X] T001 Add `fastmcp` to `requirements.txt` (research.md R1)
- [X] T002 [P] Add `CI_MCP_RATE_LIMIT_PER_MIN` (int, sane default e.g. `20`) to `app/config.py`, following the existing `CI_*` convention

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The shared Skill/Agent framework and the mounted, authenticated `/mcp` surface — every user story dispatches through this. No skill-specific logic yet.

**⚠️ CRITICAL**: No user story task can start until this phase is complete.

- [X] T003 Create the `Skill` dataclass (`name`, `description`, `min_role`, `input_schema`, `output_schema`, `rate_limited`, `handler`) and an in-process registry + `register_skill()` in `app/skills.py` (data-model.md)
- [X] T004 [P] Implement a per-identity in-process sliding-window rate limiter in `app/skills.py` (research.md R3 — no shared store, single-worker deployment)
- [X] T005 Implement `invoke_skill()` dispatch in `app/skills.py`: role check (reusing `app.auth.ROLE_ORDER`/`User.has_role`) → rate-limit check for `rate_limited` skills → call `handler(user, args)` → audit via `registry.auth_store.record_audit` for every outcome including role-denied and rate-limited (`action` = `mcp_skill:<name>` / `:denied` / `:rate_limited`) (depends on: T003, T004)
- [X] T006 [P] Create the `Agent` dataclass (`name`, `label`, `description`, `skills: list[str]`) and an `agents/*.yaml` loader with validation (an unknown skill name raises a clear load-time error, not a silent skip) in `app/agents.py` (data-model.md)
- [X] T007 Add `agents: dict[str, Agent]` to `app/registry.py`'s `Registry` and load it via `app/agents.py`'s loader inside `Registry.init()`, mirroring how `registry.models`/`registry.pipelines` already load (depends on: T006)
- [X] T008 Create `app/mcpserver.py`: build the `fastmcp.FastMCP` server instance; a server middleware that resolves the authenticated `User` from the shared ASGI request state (`get_http_request().state.user`) on every `on_call_tool`; dynamic tool registration sourced from the union of skills referenced by `registry.agents`, each tool's handler delegating to `skills.invoke_skill()` (depends on: T005, T007)
- [X] T009 Mount the MCP ASGI app at `/mcp` in `app/main.py`: widen `AuthMiddleware`'s guarded-path predicate from `/api`-only to also cover `/mcp` (no anonymous MCP handshake — same default-deny posture), and compose the MCP app's own lifespan into the existing `lifespan()` context manager rather than letting it run implicitly (research.md R2 — the documented mount-under-FastAPI pitfall) (depends on: T008)
- [X] T010 [P] Smoke-test the real mounted `/mcp` endpoint in `tests/test_mcp_server.py` via an ASGI `TestClient` (not `fastmcp`'s in-memory client — this must exercise the actual mount/lifespan wiring): an MCP `initialize` handshake succeeds with valid credentials, and an unauthenticated request is rejected before any protocol exchange (depends on: T009)
- [X] T011 [P] Unit tests for `invoke_skill()` in `tests/test_skills.py` — success, role-denied, and rate-limited paths, each asserting the corresponding audit entry, against a synthetic test-only skill (depends on: T005)
- [X] T012 [P] Unit tests for the `agents/*.yaml` loader in `tests/test_agents.py` — valid load, and the unknown-skill-reference error path (depends on: T006)

**Checkpoint**: `/mcp` is mounted, authenticated, and dispatches through the shared Skill/Agent framework with zero real skills registered yet — proven by T010-T012 before any user-story-specific code exists.

---

## Phase 3: User Story 1 - Query the BI platform from an external AI tool (Priority: P1) 🎯 MVP

**Goal**: An external MCP client can ask a natural-language business question and get back a grounded, structured answer (or a clarification/decline), exactly like the browser's conversational analytics chat.

**Independent Test**: Connect a real (or `fastmcp`) MCP client with a viewer credential, call `ask_question` with a question answerable from a demo model, and confirm structured, correct data comes back — quickstart.md steps 1-7, 11.

### Implementation for User Story 1

- [ ] T013 [US1] Promote `app/api/chat.py`'s private orchestration helpers — `_start_ask`, `_handle_decision`, `_persist_learned`, `_resolved_query_dict`, `_summarize`, **and** `_handle_translator_error` (easy to miss: it's not called from inside either of the other two, only from the route, around the separate `nlq.resolve()` call) — into public functions on `app/nlq.py` (`start_ask`, `handle_decision`, `persist_learned`, `resolved_query_dict`, `summarize`, `handle_translator_error`), matching `app/nlq.py`'s own module docstring, which already declares itself the intended reuse seam for a second caller (research.md R6). Update `app/api/chat.py`'s `ask()` route to call the promoted `app/nlq.py` functions instead of owning the logic, keeping its existing three-step shape — `start_ask()` → `nlq.resolve()` wrapped in `try/except TranslatorError: return nlq.handle_translator_error(...)` → `handle_decision()` on success — behavior unchanged, rename + move only.
- [ ] T014 [US1] Regression-check `tests/test_chat_api.py` against the T013 promotion — existing chat endpoints behave identically, including the translator-failure path (depends on: T013)
- [ ] T015 [US1] Create `app/skills_analytics.py` with the `ask_question` skill: input `{question: str, conversation_id?: int}`; handler first returns `outcome: "error"` immediately if `not config.LLM_ENABLED` (the skill-side equivalent of the HTTP route's `Depends(_require_enabled)` gate — reimplemented as a plain check here since that gate raises an `HTTPException` and isn't reusable outside a route), then auto-creates a conversation via `registry.conversation_store.create()` when `conversation_id` is omitted (or a typed not-found-equivalent result on an id the caller doesn't own), then reproduces the route's own structure: `nlq.start_ask()` → `nlq.resolve()` wrapped in `try/except TranslatorError: return nlq.handle_translator_error(...)` → `nlq.handle_decision()` on success; `min_role="viewer"`, `rate_limited=True`; output always includes `conversation_id` (contracts/mcp-server.md) (depends on: T003, T013)
- [ ] T016 [US1] Add `agents/analytics.yaml` declaring the analytics agent with `skills: [ask_question]` (data-model.md) (depends on: T006, T015)
- [ ] T017 [US1] Register `app/skills_analytics.py`'s handlers into the `app/skills.py` registry at startup (import during `Registry.init()`) (depends on: T015)
- [ ] T018 [P] [US1] End-to-end `ask_question` tests through the mounted `/mcp` app in `tests/test_mcp_server.py`: an answerable question returns `outcome: "answered"` with real data; an ambiguous question returns `outcome: "clarification"` naming real candidates; an out-of-scope question returns `outcome: "declined"`; a follow-up call reusing the returned `conversation_id` gets follow-up context; **`CI_LLM_API_KEY` unset (or `config.LLM_ENABLED` patched false) returns `outcome: "error"` with no translator constructed**; **a translator that raises `TranslatorError` (monkeypatched, same fake-failure pattern as `tests/test_chat_api.py`) returns `outcome: "error"` instead of propagating**; each call is audited (`mcp_skill:ask_question`) (depends on: T016, T017)
- [ ] T019 [P] [US1] `ask_question` rate-limit test in `tests/test_mcp_server.py`: calls beyond `CI_MCP_RATE_LIMIT_PER_MIN` return `outcome: "rate_limited"` with no LLM call made, and are audited as `mcp_skill:ask_question:rate_limited` (depends on: T016, T017)

**Checkpoint**: User Story 1 is fully functional and independently deployable — an external MCP client can already do everything the feature exists for.

---

## Phase 4: User Story 2 - Discover what's available before asking (Priority: P2)

**Goal**: An external client can list its available skills (filtered to its own role) and the data catalog those skills can query, before invoking anything.

**Independent Test**: Connect at each role tier and confirm `tools/list` and `list_models`'s content differ/stay consistent with that role, without invoking `ask_question` — quickstart.md steps 8-10.

### Implementation for User Story 2

- [ ] T020 [US2] Add the `list_models` skill to `app/skills_analytics.py`: input `{}`; handler wraps `nlq.build_catalog()` and serializes the resulting `ModelCatalogEntry` list to JSON; `min_role="viewer"`, `rate_limited=False` (depends on: T003, T015)
- [ ] T021 [US2] Add `list_models` to `agents/analytics.yaml`'s `skills` list (depends on: T016, T020)
- [ ] T022 [US2] Implement role-filtered `tools/list` in `app/mcpserver.py`: an `on_list_tools` middleware hook drops any tool whose skill's `min_role` the connection's authenticated role doesn't satisfy (research.md R7) (depends on: T008)
- [ ] T023 [P] [US2] `tests/test_mcp_server.py`: register a synthetic higher-role test-only skill/agent fixture and assert a viewer connection's `tools/list` never contains it while a satisfying-role connection's does; assert `list_models`'s output matches `nlq.build_catalog()`'s own catalog for the same registered models (depends on: T021, T022)

**Checkpoint**: User Stories 1 and 2 both independently functional.

---

## Phase 5: User Story 3 - Govern which skills an agent may use, declaratively (Priority: P3)

**Goal**: An admin can change which skills an agent exposes by editing YAML, with no code change, and the MCP surface reflects it after reload.

**Independent Test**: Edit a fixture agent's declared skill list, reload, and confirm the MCP server's exposed tool set changes accordingly — no other story depends on this one.

### Implementation for User Story 3

- [X] T024 [US3] `tests/test_agents.py`: register two synthetic test-only skills (same fixture pattern as T011/T023 — no dependency on the real `ask_question`/`list_models`), declare a fixture `agents/*.yaml` (not the shipped `analytics.yaml`) referencing both, remove one from its declared list, reload via `app/agents.py`'s loader + `Registry`, and assert the MCP server's exposed tool set (`tools/list`) changes accordingly with no code change — independent of US1/US2 landing first (depends on: T007, T009)
- [ ] T025 [US3] Document the Agent/Skill/MCP framework in `README.md`: the `agents/*.yaml` declaration format, the two shipped skills (`ask_question`, `list_models`), how an external client connects to `/mcp` and authenticates, and `CI_MCP_RATE_LIMIT_PER_MIN` — per the constitution's "update README as part of the feature, not a follow-up" (depends on: T016, T021)

**Checkpoint**: All three user stories independently functional — spec.md's full scope delivered.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Verification and consistency pass across the whole feature.

- [ ] T026 [P] Add module docstrings to `app/skills.py`, `app/agents.py`, `app/mcpserver.py`, `app/skills_analytics.py` explaining each module's role and referencing this spec's key decisions (research.md R1-R7), matching this repo's existing module-docstring convention (e.g. `app/nlq.py`, `app/llm.py`)
- [ ] T027 Run the full suite (`.venv/bin/python -m pytest tests/`) and confirm zero regressions across the whole app, not just the new tests
- [ ] T028 Run `quickstart.md` end-to-end against the running app with a real MCP client connection — Constitution Principle IV, adapted per plan.md (no browser surface in this feature; an external MCP client is the golden path instead)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately
- **Foundational (Phase 2)**: depends on Setup — BLOCKS every user story (T003-T012 must all land before any T013+ task)
- **User Story 1 (Phase 3)**: depends on Foundational only — deliverable as a standalone MVP
- **User Story 2 (Phase 4)**: depends on Foundational + `app/skills_analytics.py` existing (T015, from US1) since both skills live in the same module — but is functionally independent of `ask_question` itself; its own tests never depend on US1's tests
- **User Story 3 (Phase 5)**: depends on Foundational only (T007, T009) — uses its own synthetic fixture skills/agent (same pattern as T011/T023), not the real `ask_question`/`list_models`, so it needs neither US1 nor US2 to have landed first
- **Polish (Phase 6)**: depends on all three stories being complete

### Parallel Opportunities

- T002 (Setup) has no dependency on T001 — can run alongside it
- T004, T006 (Foundational) touch different files than T003 and can start once T003 lands — see `[P]`
- T010, T011, T012 (Foundational tests) are mutually independent once their respective implementation tasks land
- T018, T019 (US1 tests) are independent of each other
- Once Foundational is done, US1 and US3 can start in parallel (US3 depends only on Foundational, via its own fixtures) — US2 needs `app/skills_analytics.py` to exist first (T015, from US1) since both real skills live in that one module; plan sequential P1 → P2 delivery unless staffed with care around that one shared file

---

## Parallel Example: Foundational Phase

```bash
# After T003 lands, these can run together:
Task: "Implement per-identity in-process rate limiter in app/skills.py"          # T004
Task: "Create Agent dataclass + agents/*.yaml loader in app/agents.py"           # T006

# After T005, T006/T007, T009 land respectively, these test tasks run together:
Task: "Smoke-test the mounted /mcp endpoint in tests/test_mcp_server.py"         # T010
Task: "Unit tests for invoke_skill() in tests/test_skills.py"                    # T011
Task: "Unit tests for the agents/*.yaml loader in tests/test_agents.py"          # T012
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1: Setup (T001-T002)
2. Phase 2: Foundational (T003-T012) — CRITICAL, blocks everything below
3. Phase 3: User Story 1 (T013-T019)
4. **STOP and VALIDATE**: quickstart.md steps 1-7, 11 against a real MCP client
5. This alone is a demoable MVP: external AI tools can query the platform's data

### Incremental Delivery

1. Setup + Foundational → `/mcp` mounted, authenticated, empty
2. + User Story 1 → external clients can ask questions (MVP)
3. + User Story 2 → external clients can discover what's queryable first
4. + User Story 3 → the skill set becomes admin-tunable without a code change
5. + Polish → full regression pass, README caught up, quickstart fully green

### Explicitly Out of Scope (spec.md Clarifications — do not add tasks for these)

- The sandbox coding agent joining the MCP surface
- Any mutating skill (save a visual/dashboard, trigger a pipeline run, author a model measure)
- A local/stdio MCP transport (remote multi-tenant HTTP only)
