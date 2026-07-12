---

description: "Task list for implementing spec 010: Generalize Visual Parameters to More Types and DSL Positions"

---

# Tasks: Generalize Visual Parameters to More Types and DSL Positions

**Input**: Design documents from `/specs/010-parameter-type-generalization/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included — Constitution Principle III makes pytest coverage mandatory for this codebase. Tests are added to the existing `tests/test_measure_dsl.py` / `tests/test_engine.py` / `tests/test_api.py` files (no `tests/contract/`/`tests/integration/` split in this project), shipped alongside each story's implementation.

**Organization**: Tasks are grouped by user story (from `spec.md`: P1/P1/P2/P2/P3) to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on another incomplete task in the same phase)
- **[Story]**: Which user story this task belongs to (US1-US5)
- Every task includes its exact file path

## Path Conventions

Single project, existing repository layout (see `plan.md`'s Project Structure) — `app/` for the FastAPI backend, `app/static/js/` for the vanilla-JS frontend, `tests/` (flat, one file per module) for pytest. No new top-level directories; every file touched already has spec-009 parameter-handling code to extend.

---

## Phase 1: Setup

**Purpose**: Establish a clean baseline before any change.

- [X] T001 Run the full existing suite (`pytest -v`) to confirm a clean, green baseline (including every spec-009 parameter test) before touching any code

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Make `param()` a general DSL construct instead of a `lag()`-only special case, and make the engine's parameter resolution type-aware. Nothing API- or UI-facing yet.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T002 In `app/measure_dsl.py`, extract shared helpers from the existing `_resolve_periods_arg`: `_param_name_from_args(args: list) -> str` (validates `param()`'s single string-literal argument, raises `kind="disallowed"` on a bad shape) and `_lookup_param(compiler, name: str)` (raises `kind="unknown_parameter"` if `compiler.parameter_values` is `None` or lacks `name`, else returns the raw resolved value) — research.md §1/§3
- [X] T003 In `app/measure_dsl.py`, add `_fn_param(compiler, args, depth) -> pl.Expr`: resolves via `_param_name_from_args`/`_lookup_param` (T002), validates the resolved value with `_is_allowed_constant` (raising `kind="disallowed"` if not, as defense-in-depth per contracts/compile_measure_param_types.md's resolution table), and returns `pl.lit(value)`
- [X] T004 In `app/measure_dsl.py`, register `"param": _fn_param` in both `_FUNCTIONS` and `_WINDOW_FUNCTIONS` — this is what makes `param()` legal anywhere a literal already is (comparisons, `if_()`, `coalesce()`, `where()`, `cast()`'s value argument), per contracts/compile_measure_param_types.md's grammar delta. `cast()`'s type-name argument needs no change — it's already string-literal-only via `_string_literal_arg` (research.md §2)
- [X] T005 In `app/measure_dsl.py`, rewrite `_resolve_periods_arg` to use `_param_name_from_args`/`_lookup_param` (T002) instead of its own inline `param()`-detection, and change its type check on a `param()`-resolved value to `isinstance(value, int) and not isinstance(value, bool)` explicitly — rejecting a `float` (even a numerically-whole one like `2.0`) or `str`, per contracts/compile_measure_param_types.md's `lag()` delta table
- [X] T006 Add pytest cases to `tests/test_measure_dsl.py` covering contracts/compile_measure_param_types.md's grammar delta and `lag()` delta table: `param()` resolving correctly inside a comparison, `if_()`'s predicate/branches, `coalesce()`'s arguments, `where()`'s predicate, and `cast()`'s value argument, across `int`/`float`/`string` resolved values; `param()` still rejected as `cast()`'s type-name argument; `lag()`'s periods argument rejects a `float`-resolved `param()` even when whole, and rejects a `string`-resolved one — quickstart.md §1
- [X] T007 [P] In `app/engine.py`, add public `PARAM_TYPES = {"int", "float", "string"}`, `param_type_ok(value, type_name) -> bool`, and `coerce_param_value(value, type_name)` per data-model.md's "Type membership and coercion" table — `param_type_ok` must accept a JSON-integer-shaped value as a legitimate `"float"` (research.md §5), and `coerce_param_value` must canonicalize a `"float"`-typed value to a genuine Python `float`
- [X] T008 In `app/engine.py`, rewrite `resolve_parameter_values()` to use T007's helpers: read `type_name = p.get("type") or "int"` per declared parameter (FR-004 backward compat), reject an unrecognized `type_name`, reject any `values`/`default` entry that fails `param_type_ok` for its declared type, and store/compare/return everything through `coerce_param_value` so the returned `resolved` dict always holds canonically-typed Python values — data-model.md "Query-time parameter selection"
- [X] T009 In `app/engine.py::run_query`, add `parameter_values=resolved_params` to the plain (non-window) inline-measure `compile_measure(...)` call site (`add_plain(nm, measure_dsl.compile_measure(text, schema, alias=nm))`) — under spec 009 this call site never needed it since `param()` could only appear inside `lag()`, which always forced window mode; now `param()` can appear in aggregate-mode measures too (research.md §9)
- [X] T010 Add pytest cases to `tests/test_engine.py` for `resolve_parameter_values()`'s type validation/coercion: a `float`-typed parameter accepts a JSON-integer-shaped value and resolves to a genuine Python `float`; an `int`-typed parameter rejects a JSON float value; a `string`-typed parameter round-trips correctly; an absent `type` field behaves identically to `"type": "int"` — quickstart.md §2

**Checkpoint**: `param()` compiles and resolves correctly, across every declared type and every legal DSL position, through a raw `run_query()`/`compile_measure()` call. No API validation or UI yet — verified by pytest only.

---

## Phase 3: User Story 1 - Declare a typed parameter and use it in a comparison or conditional (Priority: P1) 🎯 MVP

**Goal**: A dashboard developer can declare an `int`/`float`/`string`-typed parameter, reference it via `param('name')` anywhere a literal is legal (not only `lag()`), save the measure, and have it resolve correctly using the declared default.

**Independent Test**: On a single visual, declare a `float` parameter and save a measure using `param('name')` inside a comparison (not inside `lag()`); confirm it saves, compiles, and a query using the default returns the expected result (spec.md User Story 1).

### Implementation for User Story 1

- [X] T011 [P] [US1] Update `_validate_visual_spec` in `app/api/visuals.py` to be type-aware, reusing `engine.PARAM_TYPES`/`param_type_ok`/`coerce_param_value` (T007): reject an unrecognized `type`, reject a `values`/`default` entry that doesn't match the declared (or implicit `int`) type — contracts/parameters-api-types.md "Changed: POST/PUT /api/visuals"
- [X] T012 [P] [US1] Add a pytest case to `tests/test_api.py` confirming `/api/measures/check` (`check_measure` in `app/api/models.py`) already resolves `param()` correctly for a `float`/`string`-typed parameter used in a non-`lag()` position — no code change expected here (research.md §9: this endpoint already passes `parameter_values` unconditionally), this task is verification-only
- [X] T013 [P] [US1] In `app/static/js/builder.js`'s `renderParameters()`, add a `type` `<select>` (`int`/`float`/`string`) per declared parameter row; make the `values`-list text input's parsing dispatch on the row's type (`parseInt`/`parseFloat`/trimmed-and-deduped string split); clear `values`/`default` when a row's type changes rather than reinterpreting stale data; make `addParameter()` default a new row to `type: "int"` — contracts/parameters-api-types.md "Frontend contract additions", research.md §7
- [X] T014 [US1] In `app/static/js/builder.js`'s `renderParameters()`, update the `default`-value `<select>`'s change handler to parse `.value` according to the row's declared type (`parseInt`/`parseFloat`/pass-through string) instead of always `parseInt` (depends on T013)
- [X] T015 [P] [US1] In `app/static/js/completion.js`'s `dslItems` (the `kind: "param"` branch), include the parameter's type in the completion hint: `` `${p.type || "int"} · values: ${p.values.join(", ")} (default ${p.default})` `` — contracts/parameters-api-types.md "measurelab.js" row
- [X] T016 [US1] Add pytest cases to `tests/test_api.py`: saving a visual with a valid `float`- or `string`-typed parameter succeeds; saving one with a type/value mismatch (wrong-typed `values` entry, wrong-typed `default`) is rejected with a clear error; an unrecognized `type` string is rejected (depends on T011)
- [X] T017 [US1] Browser walkthrough per quickstart.md §5.1-§5.3: declare a `float` parameter, author `if_(revenue > param('threshold'), revenue, 0)`, confirm it resolves/saves and "save to model" stays blocked; declare a `string` parameter and use it in a comparison/`coalesce()`; switch a parameter's type and confirm `values`/`default` clear. Zero console errors.

**Checkpoint**: User Story 1 fully functional and independently testable — this is the MVP.

---

## Phase 4: User Story 2 - Existing int/lag() parameters keep working unchanged (Priority: P1)

**Goal**: A visual saved under spec 009 (untyped parameter, used only inside `lag()`) continues to load, query, and display identically after this feature ships, with no developer action required.

**Independent Test**: Re-run every existing spec-009 automated test and manual scenario against the post-change system; all must pass unchanged, and a pre-existing saved visual with an untyped parameter must open and query identically (spec.md User Story 2).

### Implementation for User Story 2

- [X] T018 Run `pytest tests/test_measure_dsl.py tests/test_engine.py tests/test_api.py -k "lag or param" -v` and confirm every spec-009 parameter/`lag()` test still passes unmodified — quickstart.md §4.2 (verification-only, no code change expected; if anything fails, that's a regression to fix before continuing)
- [X] T019 [P] Add a pytest case to `tests/test_api.py` explicitly saving a visual with a parameter that has **no** `type` field at all and confirming it is treated as `int` end-to-end: declaration validation accepts it, query resolution works, and it participates correctly in dashboard sharing/conflict checks (T024-T026) — data-model.md "absent `type` field ... always `int`"
- [X] T020 [US2] Browser walkthrough per quickstart.md §4.1: open a visual saved before this feature (untyped parameter, referenced only inside `lag()`) and confirm it loads, displays as an implicitly-`int` parameter, its toggle control works, and its measure resolves — zero code or data changes required, zero console errors

**Checkpoint**: User Stories 1 and 2 both verified — the new capability doesn't regress the one it extends.

---

## Phase 5: User Story 3 - Mismatched parameter type is caught with a clear error (Priority: P2)

**Goal**: A `string`- or `float`-typed parameter used as `lag()`'s periods argument is rejected clearly, at both live-check and save time, never silently coerced.

**Independent Test**: Declare a `string` parameter, write `lag(revenue, param('string_param'))`, and confirm the save/compile is rejected with an error naming the type mismatch (spec.md User Story 3).

### Implementation for User Story 3

- [X] T021 [US3] Add a pytest case to `tests/test_api.py`: `/api/measures/check` with a `string`-typed parameter referenced as `lag()`'s periods argument returns `ok: false` with an error naming the type mismatch, not a generic parse failure — quickstart.md §1
- [X] T022 [US3] Add a pytest case to `tests/test_api.py`: saving a visual whose inline measure uses a `float`-typed parameter as `lag()`'s periods argument is rejected at visual-save time (via `_validate_visual_spec`/`_validate_measure_body`'s existing compile-time checks), not only at query time — mirrors spec 009's "fail closed at save time" posture (depends on T011)
- [X] T023 [US3] Browser walkthrough: in the Measure Lab, declare a `string` parameter, write `lag(revenue, param('name'))`, and confirm the live-check status shows a clear rejection message naming the type mismatch (not a generic/obscure error). Zero console errors otherwise.

**Checkpoint**: User Stories 1-3 verified.

---

## Phase 6: User Story 4 - Dashboard sharing/conflict detection accounts for type (Priority: P2)

**Goal**: Two visuals declaring a same-named parameter with different types are treated as conflicting (never silently merged); same type + values + default still share one control.

**Independent Test**: Declare a same-named parameter on two visuals with different types, attempt to add both to one dashboard, and confirm the conflict is blocked with an error naming the parameter (spec.md User Story 4).

### Implementation for User Story 4

- [X] T024 [P] [US4] Update `_same_param_def` in `app/api/dashboards.py` to compare `type` first (defaulting absent `type` to `"int"` on both sides), short-circuiting to "not identical" on a mismatch before comparing `values`/`default` (via `engine.coerce_param_value`) — contracts/parameters-api-types.md "Changed: POST/PUT /api/dashboards"
- [X] T025 [P] [US4] Update `sameParamDef` in `app/static/js/dashboard.js` to compare `type` first, with a type-aware sort (numeric comparator for `int`/`float`, default/lexicographic for `string`) before the values-set comparison — contracts/parameters-api-types.md "dashboard.js" row
- [X] T026 [US4] Add pytest cases to `tests/test_api.py`: two visuals with a same-named parameter but different `type` (e.g. `int` vs `string`, even with similar-looking values) are blocked from coexisting on one dashboard with an error naming the parameter; two visuals with the same `type`, `values`, and `default` (using `float` and `string` types, not just `int`) are allowed together (depends on T024)
- [X] T027 [US4] Browser walkthrough per quickstart.md §5.4: two visuals with a same-named parameter, one `int` one `string` with similar-looking values (e.g. `[1,2,3]` vs `["1","2","3"]`), blocked from coexisting on one dashboard with a clear message. Zero console errors.

**Checkpoint**: User Stories 1-4 verified.

---

## Phase 7: User Story 5 - Viewers get a type-appropriate control (Priority: P3)

**Goal**: A `string`-typed parameter's viewer control shows its declared text options; a `float`-typed parameter's control allows decimal values; both toggle-and-rerun exactly as `int` parameters already do.

**Independent Test**: Open a visual with a declared `string` parameter and confirm the control shows its declared text values (not attempting numeric parsing/sorting), and that selecting one re-runs the query using that value (spec.md User Story 5).

### Implementation for User Story 5

- [X] T028 [P] [US5] Verify `renderParamToggleBar()` in `app/static/js/builder.js` renders `string`/`float` parameter values correctly (values are already correctly typed from T013/T014's parsing, so this should already work structurally) — fix if a display/comparison bug is found (e.g. a stray numeric assumption), otherwise no code change
- [X] T029 [P] [US5] Verify `renderDashParams()` in `app/static/js/dashboard.js` (the dashboard-level shared parameter control) renders `string`/`float` parameter values correctly — fix if found, otherwise no code change
- [X] T030 [US5] Browser walkthrough per quickstart.md §5.2: a `string` parameter's viewer control shows its declared text options (not numeric parsing/sorting); a `float` parameter's control allows its declared decimal values; both toggle-and-rerun correctly on a standalone visual and on a dashboard. Zero console errors.

**Checkpoint**: All five user stories verified — feature complete.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Final documentation, regression, and sign-off — required by Constitution Principles III/IV before this feature is reported done.

- [X] T031 [P] Update `README.md` to document parameter types (`int`/`float`/`string`) and the generalized `param()` DSL positions (comparisons, `if_()`, `coalesce()`, `where()`, `cast()`'s value argument), alongside the existing spec-009 parameter documentation
- [X] T032 Run the full regression suite (`pytest -v`) and confirm every pre-existing test (including all of spec 009's parameter tests) still passes unchanged — quickstart.md §6
- [X] T033 Full end-to-end browser pass of quickstart.md §5 (all four sub-steps in one continuous session) as final sign-off, distinct from the per-story spot checks in T017/T020/T023/T027/T030
- [X] T034 [P] Re-review the Constitution Check table in plan.md against what was actually built, confirming no drift (in particular Principle VI: `param()`'s wider reach is still zero new eval-based surface; and that the JSON/JS numeric-type accommodation in `coerce_param_value` didn't introduce any silent cross-type coercion beyond what research.md §5 documents)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup. **BLOCKS all user stories** — nothing in Phases 3-7 can start until `param()` is a general DSL construct and `resolve_parameter_values()` is type-aware.
- **User Story 1 (Phase 3)**: Depends on Foundational only. This is the MVP — it alone delivers the entire new capability (a typed parameter usable outside `lag()`), per spec.md's "Why this priority".
- **User Story 2 (Phase 4)**: Depends on Foundational + US1 (needs the full new code path to exist before it can be verified as non-regressive against it). Largely a verification phase, not new implementation.
- **User Story 3 (Phase 5)**: Depends on Foundational + US1 (T011's visual-save validation must exist for T022's save-time rejection test).
- **User Story 4 (Phase 6)**: Depends on Foundational only (dashboard sharing/conflict logic is independent of US1's UI work, though it reuses T007's engine helpers).
- **User Story 5 (Phase 7)**: Depends on US1 (T013/T014's type-aware values/default parsing is what US5's controls render).
- **Polish (Phase 8)**: Depends on all desired user stories being complete.

### Within Each User Story

- Same-file edits are sequenced (not parallel), even when logically independent, to avoid conflicting concurrent edits.
- Backend validation/API tasks generally precede the frontend tasks that call them.
- Each story's browser walkthrough task is last, after every implementation task in that story.

### Parallel Opportunities

- Phase 2: T007 (`engine.py`'s new type helpers) is independent of the `measure_dsl.py` chain (T002-T006) and can run in parallel with it.
- Phase 3 (US1): T011 (`api/visuals.py`), T012 (verification-only), T013 (`builder.js`), and T015 (`completion.js`) are all independent of each other.
- Phase 4 (US2): T019 (`tests/test_api.py`) is independent of T018/T020 (both verification-only, different concerns).
- Phase 6 (US4): T024 (`api/dashboards.py`) and T025 (`dashboard.js`) are independent of each other.
- Phase 7 (US5): T028 (`builder.js`) and T029 (`dashboard.js`) are independent of each other.

---

## Parallel Example: Phase 2 (Foundational)

```bash
# Two independent tracks:
Track A (DSL generalization):     T002 -> T003 -> T004 -> T005 -> T006
Track B (engine type awareness):  T007 -> T008 -> T009

# T010 (tests) waits for both tracks to finish (T006 and T009).
```

## Parallel Example: Phase 3 (User Story 1)

```bash
# Four independent tracks:
Track A (visual validation):   T011
Track B (check_measure verify): T012
Track C (builder.js UI):        T013 -> T014
Track D (completion hint):      T015

# Then:
T016 (tests) depends on T011
T017 (browser walkthrough) depends on everything above
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup.
2. Complete Phase 2: Foundational (critical — blocks every story).
3. Complete Phase 3: User Story 1.
4. **STOP and VALIDATE**: run T016's pytest cases and T017's browser walkthrough independently.
5. This alone is demonstrable/shippable value: a developer can declare a typed parameter and use it anywhere a literal is legal in a measure expression — the entire point of this feature.

### Incremental Delivery

1. Setup + Foundational → substrate ready (pytest-verified only).
2. US1 → the new capability works end-to-end → demo-able MVP.
3. US2 → confirms nothing shipped under spec 009 broke.
4. US3 → confirms the new failure mode (type mismatch) is caught clearly, not silently.
5. US4 → dashboards stop being able to silently merge differently-typed same-named parameters.
6. US5 → viewer-facing polish for the new types, matching what `int` already had.
7. Each story lands as a complete, independently-verifiable increment without breaking the previous ones.

### Suggested Sequencing for a Single Implementer

Given the dependency chain (US2/US3/US5 all lean on US1's work existing first; US4 only needs Foundational), the phases are best done in numeric order (2 → 3 → 4 → 5 → 6 → 7 → 8) — US4 could technically be pulled forward directly after Foundational, but sequencing it after US1-US3 keeps the "prove the core capability, then prove it doesn't regress, then prove it fails safely, then prove dashboards respect it" narrative intact for review purposes.

---

## Notes

- [P] tasks touch different files and have no dependency on another incomplete task in the same phase.
- Same-file tasks are intentionally left unmarked and sequenced, even where logically independent, to avoid edit conflicts.
- Every implementation task cites the plan.md/research.md/data-model.md/contracts/ section it's grounded in.
- Commit after each task or logical group, per repository convention.
- Stop at any checkpoint to validate a story independently before continuing.
