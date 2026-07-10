# Tasks: Common Dimensional Models

**Input**: [spec.md](spec.md), [plan.md](plan.md), [data-model.md](data-model.md), [contracts/api-changes.md](contracts/api-changes.md), [quickstart.md](quickstart.md)

**Tests**: Included and non-optional — constitution Principle III ("every
feature ships with tests") overrides the generic template's "tests are
optional" default for this project.

**Organization**: Grouped by user story (spec.md priorities): US1 = define a
bundle, US2 = import a bundle (whole, default), US3 = subset import. US1+US2
together are the MVP — a bundle nobody imports delivers nothing, and FR-006's
default-whole-bundle behavior is part of the MVP contract, not an extra.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files/regions, no ordering dependency)
- **[Story]**: US1 / US2 / US3, or unlabeled for setup/foundational/cross-cutting/polish

---

## Phase 1: Setup

- [ ] T001 Create the `dimensions/` directory at the repo root (parallel to `models/`)
- [ ] T002 Add `DIMENSIONS_DIR = Path(os.environ.get("CI_DIMENSIONS_DIR", PROJECT_ROOT / "dimensions"))` to `app/config.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story below can be implemented until this phase is
complete — every story depends on the bundle/import data structures and
registry wiring existing first.

- [ ] T003 [P] Add `Dataset` and `DatasetJoin` dataclasses to `app/semantic.py` (see [data-model.md](data-model.md))
- [ ] T004 [P] Add `Import` dataclass to `app/semantic.py`
- [ ] T005 Add `DimensionBundle` dataclass to `app/semantic.py` (depends on T003)
- [ ] T006 Add `imports: list[Import] = field(default_factory=list)` to the `Model` dataclass in `app/semantic.py` (depends on T004) — every existing model YAML must remain valid unchanged
- [ ] T007 Implement `_parse_dataset_join`, `_parse_dataset`, `_parse_bundle` parsing helpers in `app/semantic.py`, reusing `_parse_source`/`_as_list`/the existing `on:`-vs-`left_on`/`right_on` shorthand and the YAML-1.1 bare-`on:` quirk handling already used by `Join` (depends on T003, T005)
- [ ] T008 Implement `load_dimension_bundles(dimensions_dir) -> dict[str, DimensionBundle]` in `app/semantic.py`: parse every `dimensions/*.yaml`, reject cyclical `DatasetJoin` graphs and cross-dataset dimension-name collisions within one bundle, error messages naming the bundle + dataset at fault (depends on T007)
- [ ] T009 Implement `_parse_import` inside `_parse_model` in `app/semantic.py` — structural parse only (bundle/anchor/subset recorded, not yet validated against loaded bundles, so a model file stays parseable in isolation) (depends on T004, T006)
- [ ] T010 Implement `resolve_imports(model: Model, bundles: dict[str, DimensionBundle]) -> Model` in `app/semantic.py`: for each `Import`, validate bundle/anchor/subset exist, BFS the bundle's dataset graph from `anchor_dataset` (pruned to the subset if given), collect reachable datasets' dimensions, apply collision rules (native-wins over imported; imported-vs-imported across two bundles is a load-time error), merge survivors into `model.dimensions`, attach internal `ImportBinding`s (depends on T008, T009)
- [ ] T011 Extend `Registry` in `app/registry.py`: add `dimension_bundles: dict[str, semantic.DimensionBundle]`; add `reload_all()` that calls `load_dimension_bundles()` then `load_models()` then `resolve_imports()` per model; keep `reload_models()` as a thin wrapper around `reload_all()` so existing callers in `app/api/models.py` don't need to change (depends on T008, T010)
- [ ] T012 Wire `Registry.init()` (and therefore `app/main.py` startup) through `reload_all()` (depends on T011)

**Checkpoint**: bundles load, validate, and resolve into importing models' `dimensions` dict. Foundation ready for US1/US2/US3.

---

## Phase 3: User Story 1 - Define a common dimensional model once (Priority: P1)

**Goal**: A developer can declare a bundle with multiple datasets and joins between them, independent of any fact model, and it loads/validates correctly.

**Independent Test**: Load a 2-dataset bundle with a join between them; confirm both datasets' attributes resolve when the bundle is inspected on its own (no fact model needed).

### Tests for User Story 1 (write first, confirm they fail before Phase 2 lands)

- [ ] T013 [P] [US1] Test: a bundle with two datasets and a join between them parses, and the joined-to dataset's columns are reachable through the joined-from dataset — in `tests/test_semantic.py`
- [ ] T014 [P] [US1] Test: a bundle whose `DatasetJoin`s form a cycle is rejected at load time, naming the bundle — in `tests/test_semantic.py`
- [ ] T015 [P] [US1] Test: two datasets in the same bundle declaring a dimension with the same name is rejected at load time, naming both datasets and the dimension — in `tests/test_semantic.py`
- [ ] T016 [P] [US1] Test: a bundle YAML has no `measures:` concept — a bundle file containing one is either ignored or rejected (pick one and assert it), confirming FR-004 structurally — in `tests/test_semantic.py`

### Implementation for User Story 1

Substantially delivered by Phase 2 (T003-T010, which implement bundle
definition and validation directly). Remaining story-specific work:

- [ ] T017 [US1] Confirm/adjust error message formatting in `_parse_bundle`/`load_dimension_bundles` so every rejection case in T014-T016 names the specific bundle and dataset, matching the existing `ModelError` message convention (depends on Phase 2)

**Checkpoint**: User Story 1 is independently functional — a bundle can be authored and validated with no importer.

---

## Phase 4: User Story 2 - Import a bundle into a fact model, whole by default (Priority: P1)

**Goal**: A fact model imports a bundle by name with an anchor; every dataset in the bundle becomes queryable on the fact model by default, including datasets reachable only through the bundle's own internal joins.

**Independent Test**: Import a 2-dataset bundle (B joins to A) into a fact model anchored only on B; query a dimension that lives on A and confirm it resolves correctly.

### Tests for User Story 2 (write first)

- [ ] T018 [P] [US2] Test: a fact model importing a 2-dataset bundle, anchored on one dataset, exposes the other (transitively-joined) dataset's dimensions too — in `tests/test_semantic.py`
- [ ] T019 [P] [US2] Test: `engine.run_query` against a fact model with an import returns correct values when grouping and filtering by an imported dimension, including one that came in transitively — in `tests/test_engine.py`
- [ ] T020 [P] [US2] Test: every existing filter op (`eq/ne/gt/gte/lt/lte/in/not_in/contains`) works identically against an imported dimension as against a native one — in `tests/test_engine.py`
- [ ] T021 [P] [US2] Test: a fact row with no matching bundle row returns nulls for imported attributes (default `how: left`), and is dropped only when the import declares `how: inner` — in `tests/test_engine.py`
- [ ] T022 [P] [US2] Test: a fact model importing two distinct bundles resolves dimensions from both; a same-named dimension collision across the two bundles is a load-time error naming both bundles — in `tests/test_semantic.py`
- [ ] T023 [P] [US2] Test: an imported dimension's name colliding with a natively-declared dimension on the same fact model — the native one wins (no error) — in `tests/test_semantic.py`
- [ ] T024 [P] [US2] Test: all joins added by an import remain lazy — no `.collect()` before the query's final `.collect()` (assert via the existing lazy-scan test pattern, e.g. inspecting the `LazyFrame` plan or row-group-read behavior the way existing pushdown tests already do) — in `tests/test_engine.py`

### Implementation for User Story 2

- [ ] T025 [US2] Extend `scan(model)` in `app/engine.py`: after applying `model.joins` as today, for each `ImportBinding` build the bundle's combined lazy frame (BFS-ordered `DatasetJoin`s over `included_datasets`, starting from `anchor_dataset`) and join it into the running frame via the `Import`'s `left_on`/`right_on`/`how` (depends on Phase 2 T010)
- [ ] T026 [US2] Add the `imports` summary field to `Model.to_public()` in `app/semantic.py`, per [contracts/api-changes.md](contracts/api-changes.md) (depends on Phase 2 T010)
- [ ] T027 [US2] In `app/api/models.py`, call `semantic.resolve_imports(parsed, registry.dimension_bundles)` after `semantic.parse_model_text()` inside `validate_model`, `create_model`, and `put_model_yaml`, so the editor/validate/create/put paths reflect imported dimensions too, not only the startup-load path (depends on T025)

**Checkpoint**: US1 + US2 together are the MVP — bundles can be defined and imported, whole by default, and queried correctly end-to-end.

---

## Phase 5: User Story 3 - Import only a subset of a bundle's datasets (Priority: P2)

**Goal**: A fact model can restrict an import to named datasets within a bundle.

**Independent Test**: Import a 3-dataset bundle with an explicit 2-dataset subset; confirm the third dataset's dimensions are absent from the importing fact model.

### Tests for User Story 3 (write first)

- [ ] T028 [P] [US3] Test: `datasets: [...]` on an import limits merged dimensions to only the named datasets — in `tests/test_semantic.py`
- [ ] T029 [P] [US3] Test: a subset naming a dataset not present in the bundle fails validation, naming the unknown dataset — in `tests/test_semantic.py`
- [ ] T030 [P] [US3] Test: omitting `datasets:` behaves identically to naming every dataset in the bundle explicitly — in `tests/test_semantic.py`

### Implementation for User Story 3

- [ ] T031 [US3] Parse the optional `datasets: list[str]` field on `Import` in `app/semantic.py` (depends on Phase 2 T009)
- [ ] T032 [US3] Apply the subset as a filter on `resolve_imports()`'s BFS-reachable set in `app/semantic.py` (depends on Phase 2 T010, T031)

**Checkpoint**: All three spec user stories independently functional.

---

## Phase 6: Cross-Cutting Fix - Data Explorer Attribution

Not a spec user story on its own, but a correctness requirement surfaced
during planning: without this, bundle-sourced files would be wrongly
flagged `unmapped`, contradicting [004](../004-studio-portal-data-explorer/spec.md)'s
own FR-012.

- [ ] T033 [P] Extend the matcher-building in `app/api/explorer.py` to also cover each model's resolved `ImportBinding`s' dataset sources, tagged `import: {bundle}.{dataset}` — per [contracts/api-changes.md](contracts/api-changes.md) (depends on Phase 2 T010)
- [ ] T034 [P] Test: a bundle dataset's source file is attributed to every fact model that imports it (directly or transitively), not `unmapped` — in `tests/test_api.py` (or wherever explorer coverage currently lives)

---

## Phase 7: New API Surface - Bundle Read/Write Endpoints

- [ ] T035 [P] Create `app/api/dimensions.py`: `GET /dimensions`, `POST /dimensions/reload`, `GET /dimensions/{name}/yaml`, `PUT /dimensions/{name}/yaml` — per [contracts/api-changes.md](contracts/api-changes.md) (depends on Phase 2 T011)
- [ ] T036 Register the new router in `app/api/__init__.py` (depends on T035)
- [ ] T037 [P] Test: `/api/dimensions` list/reload/get-yaml/put-yaml round-trip, including that `PUT` re-resolves every importing model — in `tests/test_api.py`

---

## Phase 8: Demo Data & Quickstart Validation

Builds the concrete worked example from [quickstart.md](quickstart.md), used
as the end-to-end proof for spec SC-001/SC-002/SC-003.

- [ ] T038 Extend `app/seed.py` to seed `s3://cash-intel/ref/regions.csv` and `s3://cash-intel/ref/territories.csv`, reusing the existing `REGIONS`/`REGION_COORDS` constants (do not modify `marketing`'s existing seeding)
- [ ] T039 Add `dimensions/geography.yaml` exactly as specified in [quickstart.md](quickstart.md)
- [ ] T040 Update `models/sales.yaml`: remove the native `region` dimension, add the `dimension_imports` block anchored on `regions`
- [ ] T041 Update `models/logistics.yaml`: same change as T040
- [ ] T042 Run [quickstart.md](quickstart.md) end-to-end: pytest subset, the `curl` checks (imports summary, transitive `territory_name`, explorer attribution), and the browser verification steps (geo chart on `sales`, cross-filter across `sales`/`logistics`/`marketing` by region)

**Checkpoint**: the feature is proven against real, running data — not just unit tests.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [ ] T043 [P] Add a "Common dimensional models" section to `README.md` documenting the `dimensions/` directory, bundle YAML shape, and `dimension_imports` — per constitution's Development Workflow ("update README.md as part of the feature, not as a follow-up")
- [ ] T044 Run the full existing test suite (`.venv/bin/python -m pytest tests/`) and confirm zero regressions
- [ ] T045 Full browser regression sweep per constitution Principle IV — not just the new geography demo, but existing builder/dashboard/portal/explorer surfaces — zero console errors before calling the feature done

---

## Phase 10: Authoring UI (User Story 4 — added after first use)

Spec User Story 4 / FR-014..FR-017. The backend mechanism is complete and
tested by Phase 9; this phase makes it usable from the running app, which
first-use showed to be a hard requirement, not a nicety.

- [ ] T046 Backend: `engine.scan_source(source)` public helper returning a single source's schema (feeds per-dataset column introspection)
- [ ] T047 Backend: `POST /api/dimensions/validate` — parse bundle text; on success, introspect each dataset's own source columns; return per-dataset summary + columns (mirrors `/api/models/validate`)
- [ ] T048 Backend: `POST /api/dimensions` (create bundle file, 409 on name/file collision) and `DELETE /api/dimensions/{name}` — the delete refused with 409 naming importing models when any model imports the bundle (FR-017)
- [ ] T049 [P] Backend tests: validate endpoint (ok + error + per-dataset columns), create round-trip, delete-guard refusing when imported and succeeding when not — in `tests/test_api.py`
- [ ] T050 Frontend: generalize `app/static/js/editor.js` to `editor.kind` ("model" | "bundle") — per-kind template, validate/save/delete endpoints, status/labels; new-bundle template with a worked geography-style example
- [ ] T051 Frontend: sidebar "Common Dimensions" section in `index.html` (bundle list, click-to-edit, "+ new common model"); render + wire in a new `app/static/js/dimlab.js` and `main.js`
- [ ] T052 Frontend: "Common Dimensions" import panel in the model editor side — list available bundles with their datasets; a click inserts a `dimension_imports` block anchored on the chosen dataset, `on:` pre-filled with the anchor dataset's first dimension name (correct for the common case), then re-validates
- [ ] T053 Confirm bundles never appear in the builder model `<select>` (FR-016) — it reads `/api/models`; add/confirm coverage
- [ ] T054 Browser-verify US4 end-to-end: create a bundle in-app, import it into a fact model via the panel, watch live validation resolve the gained dimensions; attempt to delete an imported bundle and see it refused; zero console errors
- [ ] T055 [P] Update `README.md` to mention the in-app authoring/import path alongside the hand-authored-YAML description

---

## Dependencies & Execution Order

- **Setup (Phase 1)** → no dependencies.
- **Foundational (Phase 2)** → depends on Setup. **Blocks every user story.**
- **US1 (Phase 3)**, **US2 (Phase 4)**, **US3 (Phase 5)** → all depend only on Phase 2, not on each other structurally, but US2's tests assume US1's bundle-definition machinery exists (it does, from Phase 2), and US3 extends US2's import mechanism (subset is a filter on top of the whole-bundle default) — implement in priority order (US1 → US2 → US3) rather than in parallel, since US2 is the MVP-completing story and US3 is a refinement of it.
- **Phase 6 (Explorer fix)** and **Phase 7 (bundle API)** → depend on Phase 2 (T010/T011 respectively); independent of each other and of US3; can run in parallel with US3 or right after US2.
- **Phase 8 (Demo data)** → depends on US1 + US2 being complete (needs real imports to exist); benefits from US3 existing too if the demo is later extended to a subset example, but the quickstart as written only needs whole-bundle import.
- **Phase 9 (Polish)** → last; depends on everything above.

### Suggested Order

T001-T012 (Setup + Foundational) → T013-T017 (US1 tests+fixups) → T018-T027
(US2 tests+implementation, **MVP complete here**) → T028-T032 (US3) →
T033-T034 (explorer fix) → T035-T037 (bundle API) → T038-T042 (demo +
quickstart) → T043-T045 (polish).

### Parallel Opportunities

- T003/T004 (independent dataclasses) in parallel.
- All test tasks within a phase marked [P] target the same test file but
  independent test functions — safe to draft in parallel, sequence the
  actual edits to avoid clobbering each other in one file.
- Phase 6 and Phase 7 can proceed in parallel once Phase 2 lands — they
  touch different files (`explorer.py` vs. new `dimensions.py`).

## Implementation Strategy

**MVP = Setup + Foundational + US1 + US2** (T001-T027). At that checkpoint,
a bundle can be defined, imported whole by default, and queried correctly —
the core value proposition from the original ask is delivered and
independently verifiable, even before subset-import, the explorer fix, or
the demo data exist. Recommended: stop and validate there (run the US1/US2
tests, manually exercise one import end-to-end) before continuing to US3
and the polish phases.
