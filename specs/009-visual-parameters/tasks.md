---

description: "Task list for implementing spec 009: Visual Parameters for Measures"

---

# Tasks: Visual Parameters for Measures

**Input**: Design documents from `/specs/009-visual-parameters/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included — Constitution Principle III ("Every Feature Ships With Tests") makes pytest coverage mandatory for this codebase, not optional. Tests are added to the existing `tests/test_measure_dsl.py` / `tests/test_engine.py` / `tests/test_api.py` files (this project has no `tests/contract/`/`tests/integration/` split — see `plan.md`'s Project Structure), not written first per strict TDD, but shipped alongside each story's implementation per Principle III.

**Organization**: Tasks are grouped by user story (from `spec.md`, P1/P1/P2/P2/P3) to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on another incomplete task in the same phase)
- **[Story]**: Which user story this task belongs to (US1-US5)
- Every task includes its exact file path

## Path Conventions

Single project, existing repository layout (see `plan.md`'s Project Structure) — `app/` for the FastAPI backend, `app/static/js/` for the vanilla-JS frontend, `tests/` (flat, one file per module) for pytest. No new top-level directories.

---

## Phase 1: Setup

**Purpose**: Establish a clean baseline before any change.

- [X] T001 Run the full existing suite (`pytest -v`) to confirm a clean, green baseline before touching any code

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The DSL/engine/query substrate every user story depends on — `param()` support in the safe measure compiler, and the engine-level validation/resolution that turns a query's declared parameters + selections into the `dict[str, int]` the compiler consumes. Nothing UI- or persistence-facing yet.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T002 Add `"unknown_parameter"` to the `ErrorKind` literal in `app/measure_dsl.py` (contracts/compile_measure_param.md "New ErrorKind")
- [X] T003 Add `referenced_parameter_names(text) -> set[str]` structural-only helper to `app/measure_dsl.py` — AST walk for `param(...)` call sites anywhere in the expression, parses only, never evaluates (mirrors existing `referenced_names()`/`is_window_expr()`; contracts/compile_measure_param.md "New structural-only helper")
- [X] T004 Add `parameter_values: Optional[dict[str, int]] = None` to `_Compiler.__init__` (stored as `self.parameter_values`) and to `compile_measure()`'s keyword arguments in `app/measure_dsl.py`, threading it through to the `_Compiler` construction (contracts/compile_measure_param.md "Signature change")
- [X] T005 Implement `param('name')` recognition inside `_fn_lag`'s parsing of its second (`periods`) argument in `app/measure_dsl.py`: literal-int case unchanged; new case detects an `ast.Call` node named `param` with exactly one string-literal argument, looks the name up in `compiler.parameter_values`, raises `MeasureCompileError(kind="unknown_parameter")` if absent, and applies the existing positive-integer check to the resolved value. Any other shape (wrong `param()` arity/arg type) raises `kind="disallowed"`; `param(...)` appearing anywhere `_fn_lag` doesn't parse falls through unchanged to the existing generic `unknown function 'param'` rejection. Full behavior table: contracts/compile_measure_param.md
- [X] T006 Add pytest cases to `tests/test_measure_dsl.py` covering every row of contracts/compile_measure_param.md's `lag()` behavior table (resolves correctly, unknown parameter, wrong param() arity/type, param() outside lag()'s 2nd arg, resolved value < 1) — quickstart.md §1
- [X] T007 In `app/engine.py::run_query`, before any measure compiles: validate `query.get("parameters")` declarations (non-empty unique `name`, non-empty `values` list of ints, `default` a member of `values`) and `query.get("parameter_values")` (every key names a declared parameter, every value is a member of that parameter's `values`); raise `QueryError` on any violation; build `resolved: dict[str, int]` = each declared parameter's `parameter_values` override or its `default` (data-model.md "Query-time parameter selection")
- [X] T008 In `app/engine.py`, pass `resolved` (from T007) as `parameter_values=` into the existing window-measure `measure_dsl.compile_measure(...)` call site (the one that already receives `partition_by=partition_cols, order_by=order_dim`)
- [X] T009 [P] Add `parameters: list[dict] = []` and `parameter_values: dict[str, int] = {}` fields to `QueryRequest` in `app/api/query.py` (contracts/parameters-api.md "Changed: POST /api/query")
- [X] T010 Add pytest cases to `tests/test_engine.py` and `tests/test_api.py` for query-time parameter resolution and validation (default applied when no override, override applied, out-of-list value rejected with no scan executed, undeclared-name value rejected) — quickstart.md §2

**Checkpoint**: `param()` compiles correctly end-to-end through a raw `run_query()`/`/api/query` call. No visual/dashboard persistence or UI yet — verified by pytest only.

---

## Phase 3: User Story 1 - Declare a parameter and write a parameter-aware measure (Priority: P1) 🎯 MVP

**Goal**: A dashboard developer can declare a parameter on a visual, write a `lag(..., param('name'))` measure that saves to the visual, and is blocked from promoting that measure to the shared model library.

**Independent Test**: On a single visual, declare a parameter and save a `lag(..., param('name'))` measure referencing it; confirm it saves and the query it produces uses the declared default (spec.md User Story 1).

### Implementation for User Story 1

- [X] T011 [P] [US1] Add save-time validation to `app/api/visuals.py`: reject a visual save whose `spec.query.parameters` has duplicate names or a `default` not in its own `values`, and reject any `inline_measures` entry whose `expr` references (via `measure_dsl.referenced_parameter_names`) a parameter name absent from that same visual's `spec.query.parameters` (contracts/parameters-api.md "Unchanged, but newly load-bearing")
- [X] T012 [US1] Add `parameters: list[dict] = []` to `MeasureCheckIn` in `app/api/models.py`; in `check_measure()`, resolve each declared parameter to its `default` and pass as `parameter_values` into `compile_measure` (contracts/parameters-api.md "Changed: POST /api/measures/check")
- [X] T013 [US1] Add a `referenced_parameter_names` guard at the very top of `_validate_measure_body()` in `app/api/models.py`: if non-empty, reject with a 400 explaining parameterized measures can only be saved to a visual (FR-007; contracts/parameters-api.md "Changed: POST/PUT /api/models/{name}/measures")
- [X] T014 [US1] Add pytest cases to `tests/test_api.py`: visual save validation from T011 (dup names, bad default, unknown param ref), `/api/measures/check` with `parameters` from T012, and the model-measure-save rejection from T013 — quickstart.md §3
- [ ] T015 [P] [US1] Add `state.parameters` (declarations, mirrors `state.inlineMeasures`) and `state.parameterValues` (current picks) to `app/static/js/state.js`
- [ ] T016 [US1] Add parameter declaration UI to the builder in `app/static/js/builder.js` (add/edit/remove: name, values list, default) backed by `state.parameters`; round-trip `parameters` through `buildQuery()`, `currentSpec()`, and `loadVisual()` the same way `inline_measures` already round-trips (contracts/parameters-api.md "Frontend state additions")
- [ ] T017 [US1] Add a `param('name')` insert control to the Measure Lab in `app/static/js/measurelab.js`, listing `state.parameters`; disable the "save to model" button (with an explanatory tooltip) whenever the draft expression's referenced parameters are non-empty
- [ ] T018 [US1] Browser walkthrough per quickstart.md §4.1: declare a parameter, save a parameterized measure to the visual, confirm "save to model" is blocked, refresh the page and confirm both the parameter and the measure persist. Zero console errors.

**Checkpoint**: User Story 1 fully functional and independently testable — this is the MVP.

---

## Phase 4: User Story 2 - Toggle a parameter while viewing a visual (Priority: P1)

**Goal**: A viewer sees a control listing a parameter's declared values on a visual, and changing it re-runs the query and updates the display.

**Independent Test**: Open a visual with a parameter-referencing measure standalone, change the parameter control, confirm displayed values change to match (spec.md User Story 2).

### Implementation for User Story 2

- [ ] T019 [P] [US2] Add a parameter control (dropdown) to the visual view in `app/static/js/builder.js`, rendered whenever `state.parameters` is non-empty, initialized to each parameter's default and updating `state.parameterValues` + re-running the query (via `buildQuery()`'s now-included `parameter_values`) on change
- [ ] T020 [P] [US2] Add a pytest case to `tests/test_engine.py` explicitly asserting an out-of-declared-list `parameter_values` entry is rejected before any scan/query executes (spy/mock the scan and assert it's never called) — quickstart.md §2
- [ ] T021 [US2] Browser walkthrough per quickstart.md §4.1's toggle behavior: confirm changing the control re-runs the query and the displayed values change to match the newly selected value. Zero console errors.

**Checkpoint**: User Stories 1 and 2 both work independently.

---

## Phase 5: User Story 3 - Save a parameter selection to a dashboard view (Priority: P2)

**Goal**: A dashboard developer can save the current parameter selection into a named dashboard view and have it restored on reload.

**Independent Test**: On a dashboard with one parameterized visual, pick a non-default value, save a named view, reload, switch to that view, confirm the saved value (not the default) is shown (spec.md User Story 3).

### Implementation for User Story 3

- [ ] T022 [P] [US3] Confirm/adjust `app/api/dashboards.py` accepts and round-trips a `parameters: {name: int}` map on each view (verify `_dash_to_dict`'s existing passthrough in `app/store.py` already carries it — no store.py change expected per data-model.md; add explicit handling only if the API layer strips unknown view keys)
- [ ] T023 [US3] In `app/static/js/dashboard.js`, extend `tileQuery()` to merge the active view's saved `parameters` into that tile's `parameter_values` (falling back to the visual's own declared default for any parameter the view has no saved value for — the "view saved before parameter existed" edge case from spec.md)
- [ ] T024 [US3] In `app/static/js/dashboard.js`, add a parameter control to the dashboard's filter/view bar (parallel to `renderDashFilters()`) that writes the selection into `activeView().parameters[name]` and triggers `saveDash()` + `renderDashboard()`, matching the existing `dashFiltersChanged()` debounce/save/rerun pattern
- [ ] T025 [P] [US3] Add pytest cases to `tests/test_api.py` for saving/loading a dashboard view carrying `parameters`, including the fallback-to-default case when a view predates a visual's parameter
- [ ] T026 [US3] Browser walkthrough per quickstart.md §4.2's save/reload portion (single parameterized visual + saved view, not yet the two-visual shared-control part). Zero console errors.

**Checkpoint**: User Stories 1-3 independently functional.

---

## Phase 6: User Story 4 - Two visuals share one parameter control (Priority: P2)

**Goal**: Two visuals on one dashboard declaring an identically-defined same-named parameter get one shared control instead of two.

**Independent Test**: Two visuals each declaring an identical `period_list` parameter on one dashboard show one control; changing it re-runs both visuals' queries (spec.md User Story 4).

### Implementation for User Story 4

- [ ] T027 [US4] Implement `dashParamUnion()` in `app/static/js/dashboard.js` (parallel to the existing `dashDimUnion()`): scan every tile's visual's `spec.query.parameters`, group by `name`, and for each group of 2+ determine whether every declaration is identical (`values` as a set + `default`) per FR-014
- [ ] T028 [US4] Implement `renderDashParams()` in `app/static/js/dashboard.js` (parallel to `renderDashFilters()`): render one control per identical-definition group from T027, writing the single selection into `activeView().parameters[name]` and applying it to every visual in that group via T023's `tileQuery()` merge; single-visual (non-shared) parameters from US3 keep their own independent control
- [ ] T029 [P] [US4] Add pytest coverage to `tests/test_api.py` for the server-observable half of shared push-down: a saved view's single `parameters[name]` entry driving both tiles' resolved query values identically (the pure UI collapse-to-one-control behavior is verified only in the browser walkthrough, not pytest, per Constitution Principle IV)
- [ ] T030 [US4] Browser walkthrough per quickstart.md §4.2 in full: two visuals with an identically-defined parameter show one shared control; changing it updates both tiles; saving and reloading the view restores the shared value to both. Zero console errors.

**Checkpoint**: User Stories 1-4 independently functional.

---

## Phase 7: User Story 5 - Dashboard blocks conflicting parameter definitions (Priority: P3)

**Goal**: Adding or saving a dashboard with two visuals whose same-named parameter definitions differ is rejected with a clear, specific error, never silently resolved.

**Independent Test**: Attempt to add a visual with `period_list = [1,2,3]` default `1` to a dashboard that already has one with `period_list = [1,2,3,4]` default `1`; confirm the action is blocked with an error naming `period_list` and both visuals (spec.md User Story 5).

### Implementation for User Story 5

- [ ] T031 [P] [US5] Add conflict validation to `app/api/dashboards.py`'s create/update handlers (FR-014/FR-015/FR-016): walk `items` → each visual's `spec.query.parameters`, group by name, and if any group's declarations aren't identical, reject the save with a 400 naming the conflicting parameter and both visuals (contracts/parameters-api.md "Changed: POST/PUT /api/dashboards[/{id}]")
- [ ] T032 [P] [US5] Add the same conflict check to `app/static/js/dashboard.js`'s "add tile to dashboard" handler, reusing `dashParamUnion()` (T027) to block the add client-side with the same error the server would give, before the API call is even made
- [ ] T033 [P] [US5] Add pytest cases to `tests/test_api.py` for: differing `values` conflict, differing `default`-only conflict, and the "rename one visual's parameter so names no longer match → both now allowed, independent controls" resolution case from spec.md's edge cases
- [ ] T034 [US5] Browser walkthrough per quickstart.md §4.3: conflicting visuals blocked from coexisting with a clear message; renaming one parameter resolves the conflict and both visuals add successfully with independent controls; confirm no bad dashboard state is ever persisted. Zero console errors.

**Checkpoint**: All five user stories independently functional — feature complete.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Final documentation, regression, and sign-off — required by Constitution Principles III/IV before this feature is reported done.

- [ ] T035 [P] Update `README.md` to document visual parameters (declaration, `param()` in `lag()`, viewer toggling, dashboard view save/share/conflict behavior) — per constitution "Update README as part of the feature, not as a follow-up"
- [ ] T036 Run the full regression suite (`pytest -v`) and confirm every pre-existing test (including non-parameter `lag()`/`running_total()` cases and all dashboard/view tests) still passes unchanged — quickstart.md §5
- [ ] T037 Full end-to-end browser pass of quickstart.md §4 (all four sub-steps in one continuous session) as final sign-off, distinct from the per-story spot checks in T018/T021/T026/T030/T034
- [ ] T038 [P] Re-review the Constitution Check table in plan.md against what was actually built, confirming no drift (in particular Principle VI: no eval-based construct was introduced; Principle II: no new full-table materialization)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup. **BLOCKS all user stories** — nothing in Phases 3-7 can start until `param()` compiles and resolves correctly through `run_query()`.
- **User Story 1 (Phase 3)**: Depends on Foundational only. This is the MVP — it alone delivers real value (a developer-authored, viewer-toggleable measure), per spec.md's "Why this priority".
- **User Story 2 (Phase 4)**: Depends on Foundational + US1's T016 (the builder's parameter declaration UI/state must exist for a viewer control to have something to render).
- **User Story 3 (Phase 5)**: Depends on Foundational + US1 (a visual must be able to declare/save a parameter before a dashboard view can save a selection of it). Does not depend on US2's specific UI, only on the same underlying `state.parameters`/query shape.
- **User Story 4 (Phase 6)**: Depends on US3 (T023's `tileQuery()` parameter merge — sharing extends single-visual dashboard parameters to multiple visuals).
- **User Story 5 (Phase 7)**: Depends on US4's T027 (`dashParamUnion()` — conflict detection is the negative case of the same grouping logic used for sharing).
- **Polish (Phase 8)**: Depends on all desired user stories being complete.

### Within Each User Story

- Same-file edits are sequenced (not parallel), even when logically independent, to avoid conflicting concurrent edits.
- Backend validation/API tasks generally precede the frontend tasks that call them.
- Each story's browser walkthrough task is last, after every implementation task in that story.

### Parallel Opportunities

- Phase 2: T009 (`api/query.py`) is independent of the `measure_dsl.py`/`engine.py` chain (T002-T008) and can run in parallel with it.
- Phase 3 (US1): T011 (`api/visuals.py`) and T015 (`state.js`) are independent of each other and of the `api/models.py` chain (T012-T013) — three parallel tracks feeding into T016-T018.
- Phase 4 (US2): T019 (`builder.js`) and T020 (`tests/test_engine.py`) are independent.
- Phase 5 (US3): T022 (`api/dashboards.py`) is independent of the `dashboard.js` chain (T023-T024).
- Phase 7 (US5): T031 (`api/dashboards.py`), T032 (`dashboard.js`), and T033 (`tests/test_api.py`) are all independent of each other.

---

## Parallel Example: Phase 2 (Foundational)

```bash
# These two tracks can proceed at the same time:
Track A (DSL + engine): T002 -> T003 -> T004 -> T005 -> T006 -> T007 -> T008
Track B (API shape):    T009

# T010 (tests) waits for both tracks to finish (T008 and T009).
```

## Parallel Example: Phase 3 (User Story 1)

```bash
# Three independent tracks:
Track A (visual save):   T011
Track B (model-save gate): T012 -> T013
Track C (frontend state):  T015

# Then, once their prerequisites land:
T014 (tests)   depends on T011, T012, T013
T016 (builder)  depends on T015
T017 (measure lab) depends on T015, T013
T018 (browser walkthrough) depends on everything above
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup.
2. Complete Phase 2: Foundational (critical — blocks every story).
3. Complete Phase 3: User Story 1.
4. **STOP and VALIDATE**: run T014's pytest cases and T018's browser walkthrough independently.
5. This alone is demonstrable/shippable value: a developer can author a parameterized measure and have it persist correctly, even before any viewer-facing toggle or dashboard sharing exists.

### Incremental Delivery

1. Setup + Foundational → substrate ready (pytest-verified only, nothing user-facing yet).
2. US1 → developer authoring works end-to-end → demo-able MVP.
3. US2 → viewers can actually toggle the parameter → the feature's core promise is delivered.
4. US3 → dashboards remember a selection → removes the "reset every time" friction.
5. US4 → two visuals stop requiring the same toggle to be set twice.
6. US5 → the dashboard can no longer be put into a silently-wrong shared state.
7. Each story lands as a complete, independently-verifiable increment without breaking the previous ones.

### Suggested Sequencing for a Single Implementer

Given the dependency chain above (US2 needs US1, US3 needs US1, US4 needs US3, US5 needs US4), the phases are best done in the order they're numbered (2 → 3 → 4 → 5 → 6 → 7 → 8) rather than attempted in parallel, despite US2/US3 nominally being independent of each other — US1 is a hard prerequisite for both, and US4/US5 form a strict chain through US3.

---

## Notes

- [P] tasks touch different files and have no dependency on another incomplete task in the same phase.
- Same-file tasks are intentionally left unmarked and sequenced, even where logically independent, to avoid edit conflicts.
- Every implementation task cites the plan.md/research.md/data-model.md/contracts/ section it's grounded in — consult those for the "why", not just the "what".
- Commit after each task or logical group, per repository convention.
- Stop at any checkpoint to validate a story independently before continuing.
