---
description: "Task list for Safe Measure Compilation"
---

# Tasks: Safe Measure Compilation

**Input**: Design documents from `specs/008-safe-measure-compilation/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: INCLUDED — the project constitution (Principle III) mandates pytest coverage alongside every feature, so test tasks are first-class here, not optional. This is also a security feature, so the red-team suite is not negotiable.

**Organization**: Tasks are grouped by user story (US1–US4 from spec.md) so each story is an independently implementable, independently testable increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4; Setup/Foundational/Polish carry no story label
- Paths are repo-relative and concrete (single FastAPI app under `app/`, `tests/` at root, model config under `models/`)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Land new module skeletons and config so later tasks slot in without scaffolding churn.

- [X] T001 [P] Create `app/measure_dsl.py` with `MeasureCompileError(ValueError)`, the module docstring stating "never eval/exec/compile", and the size/depth guard constants (`MAX_MEASURE_LEN=2000`, `MAX_NODES=200`, `MAX_DEPTH=30`) per [contracts/compile_measure.md](./contracts/compile_measure.md) — no visitor logic yet.
- [X] T002 [P] Create `app/auth.py` with a `require_measure_author` function stub (signature only, `NotImplementedError` body) so route wiring can reference it ahead of T016.
- [X] T003 [P] Add `API_KEY = os.environ.get("CI_API_KEY", "")` to `app/config.py`, per [research.md](./research.md) R3 (fail closed when unset).

**Checkpoint**: App still boots, all existing tests green (`pytest -q`) — pure scaffolding, no behavior change yet.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The one shared security choke point (inline measures can never carry a `frame`) and the provenance store, both referenced by multiple user stories below. MUST complete before Phase 3+.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T004 In `app/engine.py`'s `run_query`, reject any inline measure dict containing a `frame` or `frame_emits` key immediately — before any compilation is attempted — raising `QueryError` with a message stating that frame-based measures require an authenticated model-measure save and are never available inline. This is the single change that closes the current worst case (an unauthenticated `POST /query` running arbitrary Python via inline `frame`) and the boundary both US1 and US3 build on.
- [X] T005 In `tests/test_engine.py`, rewrite the three existing inline-frame tests (`test_inline_framed_measure`, `test_frame_that_drops_dimensions_rejected`, `test_emitted_dimension_missing_from_frame_rejected`) to assert `QueryError` rejection instead of success, per T004's new rule. (Depends on T004.)
- [X] T006 [P] Add a `measure_provenance` table (`SCHEMA` string, `CREATE TABLE IF NOT EXISTS`) and `record_measure_provenance(model, measure, action, expr, frame, frame_emits, author) -> dict` / `measure_history(model, measure) -> list[dict]` methods to `app/store.py`'s `VisualStore`, per [data-model.md](./data-model.md) (append-only, version = 1 + previous max for that `(model, measure)` pair).
- [X] T007 [P] In `tests/test_store.py`, add unit tests for `record_measure_provenance`/`measure_history`: version increments across repeated creates/updates/deletes for the same `(model, measure)`, delete rows carry `expr=NULL`, and history returns newest-first. (Different file from T006, but logically depends on it landing first — sequence, don't parallelize with T006 itself.)

**Checkpoint**: Full suite green again (the T005 rewrite keeps it that way); provenance store ready; no inline measure can ever reach the `frame` mechanism regardless of what else is built.

---

## Phase 3: User Story 1 - Inline measures can no longer run arbitrary code (Priority: P1) 🎯 MVP

**Goal**: Every inline (query-time) measure compiles through a new AST-allowlisting compiler that never calls `eval`/`exec`/`compile`; the full red-team suite is rejected with zero execution.

**Independent Test**: Send each red-team payload as an inline measure via `/query` and confirm rejection with no execution; send a valid DSL expression and confirm correct computed results — independent of any auth/model-measure work.

### Tests for US1

- [X] T008 [US1] Create `tests/test_measure_dsl.py` with the correctness suite: plain aggregate (`sum`, `mean`, etc.), ratio of aggregates, filtered aggregate via `where(...)`, `if_(...)`, `coalesce(...)`, `cast(...)`, `count_distinct(...)` — each compiled against a small fixture `pl.Schema`/`pl.DataFrame` and asserted equal to a hand-computed or directly-constructed-`pl.Expr` expected value.
- [X] T009 [US1] In `tests/test_measure_dsl.py`, add the red-team suite: `__import__('os').system('id')`, `().__class__.__bases__[0].__subclasses__()`, `open('/etc/passwd').read()`, `getattr(col, '__globals__')`, `col.__class__` (attribute walk), `scan_parquet('s3://evil/x')`, `read_csv('/etc/passwd')`, `map_elements(lambda x: x)`, `apply(...)`, `[x for x in range(10)]` (comprehension), a lambda, a subscript, an f-string, `foobar(revenue)` (unknown function), `sum(does_not_exist)` (unknown column), and an oversized/deeply-nested expression exceeding the T001 limits — every case asserted to raise `MeasureCompileError` and never execute (e.g. via a sentinel global that must remain unset).

### Implementation for US1

- [X] T010 [US1] Implement the core AST node-allowlist visitor in `app/measure_dsl.py`: handle `Expression, Constant, Name, BinOp (Add/Sub/Mult/Div/Mod/Pow), UnaryOp (UAdd/USub/Not), Compare (single comparison only), BoolOp` per [contracts/compile_measure.md](./contracts/compile_measure.md)'s node table, building `pl.Expr`; every other node type raises `MeasureCompileError` in a catch-all `generic_visit` override.
- [X] T011 [US1] Implement the function-call allowlist in `app/measure_dsl.py`: the builder table (`sum, mean, min, max, count, count_distinct, median, std, var, first, last, col, where, if_, coalesce, cast`) restricted to bare-`ast.Name` callees only (reject `Attribute`/nested-`Call` callees outright); wire `Call` handling into the visitor. (Depends on T010.)
- [X] T012 [US1] Implement `compile_measure(text, schema, *, alias) -> pl.Expr` in `app/measure_dsl.py`: length guard, `ast.parse(text, mode="eval")` with `SyntaxError` converted to `MeasureCompileError`, one `ast.walk` pass for the node-count guard, depth tracking during the visit for the depth guard, column resolution of every `Name`/`col("...")` against `schema` (raise on unknown column), and `.alias(alias)` on the final `pl.Expr`. (Depends on T010, T011.)
- [X] T013 [US1] In `app/engine.py`'s `run_query`, route every inline measure's `expr` through `compile_measure(text, schema=lf.collect_schema(), alias=name)` instead of `compile_expr`; remove the `compile_expr` import/call from the inline-measure code path entirely. (Depends on T004, T012.)

**Checkpoint**: US1 fully functional and testable independently — inline measures can never `eval`, the red-team suite is 100% rejected, correctness suite is exact.

---

## Phase 4: User Story 2 - Trusted developers author saved model measures with accountability (Priority: P1)

**Goal**: Creating/updating/deleting a model measure requires an authoring credential; every save is validated through the safe compiler and stamped with author + version; reading a saved measure never requires the credential.

**Independent Test**: Call the model-measure endpoints without credentials (rejected), with credentials (saved + provenance stamped), and with an invalid expression (refused, nothing persisted) — exercised entirely through the API.

### Tests for US2

- [ ] T014 [P] [US2] In `tests/test_api.py`, add auth-rejection tests: `POST/PUT/DELETE` on model-measure routes without `X-API-Key` → 401; with a wrong key → 401; with a correct key but missing/empty `X-Author` → 400.
- [ ] T015 [P] [US2] In `tests/test_api.py`, add authenticated-success tests: create → 201, YAML updated, `GET .../history` shows one row (`version=1`, matching author); update → version increments to 2; delete → measure removed from YAML and from `to_public()`, a `delete` provenance row is recorded; an invalid expression on create/update → 400 and neither the YAML nor a provenance row changes.
- [ ] T016 [P] [US2] In `tests/test_semantic.py`, assert `Measure.expr()` for a non-framed measure now goes through `compile_measure` (e.g. monkeypatch `builtins.eval` to raise, and confirm a normal non-framed measure still compiles successfully — proving `eval` is never reached on that path).

### Implementation for US2

- [ ] T017 [US2] Implement `require_measure_author` in `app/auth.py`: compare the `X-API-Key` header against `config.API_KEY` via `secrets.compare_digest` (401 if `config.API_KEY` is empty/unset or mismatched), require a non-empty `X-Author` header (400 if empty given a valid key), return the author string for the route to use. (Depends on T002, T003.)
- [ ] T018 [US2] In `app/semantic.py`, change `Measure.expr()` to accept a `schema` parameter and call `measure_dsl.compile_measure(self.expr_source, schema, alias=self.name)` when `frame_source is None`; keep `compile_expr` only for the `frame_source is not None` branch. Update every call site (`app/engine.py`, `app/api/models.py`) to pass the resolved `collect_schema()` through. (Depends on T012.)
- [ ] T019 [US2] Add `replace_measure_yaml(text, measure_name, new_entry)` and `remove_measure_yaml(text, measure_name)` helpers to `app/semantic.py`, alongside the existing `append_measure_yaml`, reusing its comment-preserving block-boundary logic.
- [ ] T020 [US2] In `app/api/models.py`, add `Depends(require_measure_author)` to `POST /models/{name}/measures`; validate `expr` via `compile_measure` (schema from `engine.scan(model).collect_schema()`) when no `frame` is present; on success call `registry.store.record_measure_provenance(..., action="create", author=...)`. (Depends on T017, T018.)
- [ ] T021 [US2] In `app/api/models.py`, add `PUT /models/{name}/measures/{measure_name}` (auth-gated, 404 if the measure doesn't exist, re-validates the same way as create, rewrites via `replace_measure_yaml`, records `action="update"`) and `DELETE /models/{name}/measures/{measure_name}` (auth-gated, 404 if missing, removes via `remove_measure_yaml`, records `action="delete"` with `expr=NULL`). (Depends on T019, T020.)
- [ ] T022 [US2] Add `GET /models/{name}/measures/{measure_name}/history` in `app/api/models.py` (no auth) returning `registry.store.measure_history(model, measure)` newest-first. (Depends on T006.)
- [ ] T023 [US2] Rewrite the 33 non-framed `expr:` entries across `models/taxi.yaml`, `models/sales.yaml`, `models/subscriptions.yaml`, `models/logistics.yaml`, `models/marketing.yaml`, and the non-framed measures in `models/clinical_ops_recruitment.yaml` from method-chain `pl` syntax to the new DSL grammar, per [research.md](./research.md) R1's mapping table. (Depends on T018 — the model-load path must already route through `compile_measure` for these to validate at load time.)

**Checkpoint**: US2 fully functional and testable independently — auth-gated CRUD + provenance works end to end; every rewritten measure produces identical values to before the rewrite (spot-checked against `tests/test_engine.py`'s existing benchmark-style assertions).

---

## Phase 5: User Story 3 - An existing framed measure keeps working under the new trust boundary (Priority: P2)

**Goal**: The one real production measure needing multi-step frame logic (`months_to_75`) keeps working, saved through the authenticated path; the identical construct is always rejected inline, regardless of credentials.

**Independent Test**: Re-save `months_to_75` through the authenticated endpoint and confirm correct results; submit the same `frame`/`frame_emits` construct as an inline measure and confirm rejection.

### Tests for US3

- [ ] T024 [P] [US3] In `tests/test_api.py`, add tests for the authenticated `frame`/`frame_emits` path: `POST`/`PUT` with a `frame` body succeeds (calls `validate_frame` for the load-time syntax check) and is recorded with `frame`/`frame_emits` in its provenance row; the identical body submitted without credentials → 401 (same gate as any other mutation, not a separate check).
- [ ] T025 [P] [US3] In `tests/test_engine.py`, confirm `months_to_75` (grand-total, grouped-by-dimension, filtered, and timeline-bucketed variants — extending the existing framed-measure fixtures already in this file) still computes correct results end-to-end through the model-measure path, unaffected by US1/US2's changes to the non-framed path.

### Implementation for US3

- [ ] T026 [US3] In `app/api/models.py`'s `MeasureIn` model and the create/update handlers (T020/T021), accept optional `frame`/`frame_emits` fields; when `frame` is present, call `semantic.validate_frame` instead of `compile_measure` for the load-time check, and include `frame`/`frame_emits` in the YAML entry written by `append_measure_yaml`/`replace_measure_yaml` (both already support this shape via the `Measure` dataclass fields). (Depends on T019, T020, T021.)
- [ ] T027 [US3] Re-save `months_to_75` in `models/clinical_ops_recruitment.yaml` through the authenticated `PUT` endpoint (document the one-time curl/script invocation in [quickstart.md](./quickstart.md) step 3) to establish its first provenance record; confirm query results are identical before and after. (Depends on T026.)
- [ ] T028 [US3] Confirm via test (extending T025) that `app/engine.py`'s existing model-measure framed-measure branch (`meas.frame_source`, `meas.frame_emits`, `meas.expr()`) is untouched by T004/T013/T018 — it still routes through `compile_frame`/`compile_expr`, never `compile_measure`. (Depends on T025.)

**Checkpoint**: US3 fully functional and testable independently — `months_to_75` works via the authenticated path; inline `frame` stays rejected unconditionally (guaranteed since Foundational T004, re-verified here).

---

## Phase 6: User Story 4 - A measure the safe DSL cannot express fails with an actionable error (Priority: P3)

**Goal**: Unsupported-but-benign constructs (not security payloads) produce a clear, specific error rather than a stack trace or silent wrong answer.

**Independent Test**: Submit a measure using an unlisted function name, and one exceeding the size/nesting limits; confirm each error names the specific problem and resembles neither a Python traceback nor a generic failure.

### Tests for US4

- [ ] T029 [US4] In `tests/test_measure_dsl.py`, assert that `MeasureCompileError` messages/attributes distinguish a disallowed/security-relevant construct (e.g. attribute access, a banned node type) from a merely-unsupported one (unknown function name, unknown column, oversized/deep input) — e.g. via a `kind` attribute on the exception, not just free-text message matching.

### Implementation for US4

- [ ] T030 [US4] In `app/measure_dsl.py`, give `MeasureCompileError` a `kind: Literal["disallowed", "unknown_function", "unknown_column", "limit_exceeded"]` attribute (or equivalent), and set it correctly at every raise site in the visitor/guards per [contracts/compile_measure.md](./contracts/compile_measure.md). (Depends on T010, T011, T012.)
- [ ] T031 [US4] Confirm `app/api/query.py` and `app/api/models.py`'s existing `except semantic.ModelError` blocks also catch `MeasureCompileError` (add an explicit `except` clause if the two aren't already compatible) so the specific message reaches the HTTP response detail unwrapped. (Depends on T030.)

**Checkpoint**: US4 fully functional and testable independently — error responses are specific and categorized, never a raw traceback.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, the constitutional amendment, and full-suite/browser verification required before "done".

- [ ] T032 [P] Update `README.md`: document the measure DSL grammar, a few example measures, the `X-API-Key`/`X-Author` auth requirement for model-measure authoring, and the framed-measure carve-out policy (Constitution Development Workflow mandate — README stays current with every shipped feature).
- [ ] T033 Amend `.specify/memory/constitution.md` Principle VI per [research.md](./research.md) R5: record the three-way trust-boundary split (inline measures fully allowlisted; model measures' scalar expressions equally allowlisted; the single `frame` construct remains eval-level but is now access-controlled) as the explicit "re-opening" event the principle itself calls for.
- [ ] T034 Run the full suite (`pytest -q`) and fix any regressions; run the static check from [quickstart.md](./quickstart.md) step 6 (`grep` confirming `app/measure_dsl.py` contains no `eval(`/`exec(`/`compile(` calls, and that `app/semantic.py`'s remaining calls are reachable only from the authenticated frame carve-out).
- [ ] T035 Execute [quickstart.md](./quickstart.md) steps 1-5 end to end (compiler suite, full regression, framed carve-out, auth-gated authoring, browser spot-check of a rewritten measure in Studio) and record results per Constitution IV.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately.
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories (T004's inline-frame guard and T006's provenance table are both referenced by multiple stories below).
- **User Stories (Phase 3+)**: All depend on Foundational completion.
  - US1 and US2 are both P1 and have no dependency on each other's routes/auth — they touch disjoint files (`engine.py`+`measure_dsl.py` vs. `auth.py`+`api/models.py`+`store.py`) except for the shared `measure_dsl.compile_measure` function itself, which US2 (T018) depends on US1 (T012) to have implemented. Build US1 first.
  - US3 depends on US2's CRUD/auth scaffolding existing (T026 extends the same `MeasureIn`/handlers T020/T021 built) — build after US2.
  - US4 depends on US1's compiler existing (it refines `MeasureCompileError`) — can be built any time after US1, independent of US2/US3.
- **Polish (Phase 7)**: Depends on all four stories being complete.

### User Story Dependencies

- **US1 (P1)**: Foundational only. No dependency on US2/US3/US4.
- **US2 (P1)**: Foundational + US1's `compile_measure` (T012) for validate-on-save of non-framed measures. Otherwise independent.
- **US3 (P2)**: Foundational + US2's auth/CRUD scaffolding (extends the same endpoints). Independent of US1/US4 beyond that.
- **US4 (P3)**: Foundational + US1's compiler existing (refines its errors). Independent of US2/US3.

### Within Each User Story

- Tests are written alongside or just before their corresponding implementation tasks (constitution mandates coverage, not strict TDD ordering).
- Compiler internals (node allowlist → function allowlist → entry point) before wiring into `engine.py`/`api/models.py`.
- Auth dependency before the routes that use it.
- YAML rewrite (US2 T023) only after the routing change (T018) that makes the new syntax load correctly.

### Parallel Opportunities

- All Setup tasks (T001-T003) in parallel — different files.
- T006 (provenance table) can be built in parallel with T004/T005 (inline-frame guard) — different files; T007 follows T006.
- Within US1: T008/T009 (both in the same new test file) are sequential; T010→T011→T012→T013 are sequential (each depends on the last).
- Within US2: T014/T015/T016 (different files) in parallel; T017 (auth) can be built in parallel with T018 (semantic.py routing) — different files — before T020/T021 need both.
- US4 (T029-T031) can proceed in parallel with US2/US3 once US1 is done — different files, no shared state.

---

## Parallel Execution Examples

```bash
# Setup, all in parallel (different new/edited files):
Task: "Create app/measure_dsl.py skeleton"
Task: "Create app/auth.py skeleton"
Task: "Add config.API_KEY setting"

# US2 tests, in parallel (different files/assertions, same test file for T014/T015 can be
# split into separate test functions written together, T016 is a different file):
Task: "Auth-rejection tests in tests/test_api.py"
Task: "Measure.expr() routing test in tests/test_semantic.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (closes the worst current vulnerability — inline `frame` execution — immediately)
3. Complete Phase 3: User Story 1 (compiler + inline wiring)
4. **STOP and VALIDATE**: run the red-team suite, confirm zero execution
5. This alone is a shippable security fix even before Tier 1 auth/provenance exists

### Incremental Delivery

1. Setup + Foundational → the current inline-`frame` RCE is closed immediately, regardless of what follows.
2. Add US1 → inline measures are fully allowlisted (MVP, biggest security win).
3. Add US2 → model-measure authoring gets auth + provenance; the 34 real measures move to the new grammar.
4. Add US3 → the one production measure needing frame logic keeps working via the authenticated carve-out.
5. Add US4 → error-message polish.
6. Each story adds value without breaking the previous one; Polish (Phase 7) closes out docs/constitution/full verification.
