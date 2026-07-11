---
description: "Task list for Modelling Workspace — Delightful Model Creation & Editing"
---

# Tasks: Modelling Workspace — Delightful Model Creation & Editing

**Input**: Design documents from `specs/007-modelling-workspace/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: INCLUDED — the project constitution (Principle III) mandates pytest coverage alongside every feature, so test tasks are first-class here, not optional.

**Organization**: Tasks are grouped by user story (US1–US5 from spec.md) so each story is an independently implementable, independently testable increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US5; Setup/Foundational/Polish carry no story label
- Paths are repo-relative and concrete (single-app layout: `app/` backend + `app/static/` frontend, `tests/` at root)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Land the new module skeletons and router mount so later tasks slot into place without scaffolding churn.

- [X] T001 Create `app/api/datasets.py` with an empty `APIRouter(tags=["datasets"])` and register it in `app/main.py` alongside the existing routers (models, dimensions, explorer, query, dashboards, visuals).
- [X] T002 [P] Create `app/static/js/modelling.js` module skeleton (exports `loadModelling()`), and import it in `app/static/js/main.js` (no behavior yet).
- [X] T003 [P] Create `app/static/js/completion.js` empty module placeholder (target for the extracted completion engine in US4) so US4 has a stable import path.

**Checkpoint**: App still boots, all existing tests green (`pytest -q`), no console errors — pure scaffolding.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The DATA→MODELLING rename plumbing and shared editor state that every subsequent UI task references. MUST complete before Phase 3+.

**⚠️ Blocks all user stories.**

- [X] T004 Rename the nav in `app/static/index.html`: change the `data-mode="data"` button label `DATA` → `MODELLING` (keep `data-mode` value consistent with the state mapping chosen in T005).
- [X] T005 In `app/static/js/state.js`: rename the `view` value `"explorer"` → `"modelling"` throughout `showView` (the view list and the `mode` mapping), and add ephemeral editor state fields `editor.dirty` and `editor.columns` and a `datasets` cache slot (per [data-model.md](./data-model.md) — all session-only, never persisted).
- [X] T006 In `app/static/js/main.js`: rewire the `#mode-nav` handler so the MODELLING mode calls `loadModelling()` (replacing the direct `loadExplorer()` call), keeping STUDIO and PORTAL unchanged.

**Checkpoint**: MODELLING nav opens (even if the workspace body is still just the old explorer), STUDIO/PORTAL unaffected, tests green.

---

## Phase 3: User Story 1 — Model authoring lives in a dedicated Modelling workspace (Priority: P1) 🎯 MVP

**Goal**: Move the three authoring controls out of Studio into a Modelling workspace that also shows datasets↔models overview, the model list, and the common-model list, each with edit/new entry points. Studio becomes visual-building only.

**Independent Test**: Studio sidebar shows no authoring controls; MODELLING shows datasets overview + model list + common-model list + create/edit entry points; building a visual in Studio still works.

### Tests for US1

- [X] T007 [P] [US1] In `tests/test_static.py`, add smoke assertions: `index.html` nav contains `MODELLING` and not `DATA`; the Studio `<aside>` no longer contains `id="edit-model"`, `id="new-model"`, or `id="new-bundle"`.

### Implementation for US1

- [X] T008 [US1] In `app/static/index.html`: remove the three authoring buttons (`#edit-model`, `#new-model`, `#new-bundle`) and the "Common Dimensions" sidebar section from the Studio `<aside>`; add a `#modelling-view` container in `<main>` (or restructure `#explorer-view` into it) to host the workspace.
- [X] T009 [US1] Build `loadModelling()` in `app/static/js/modelling.js`: render (a) the datasets↔models overview by absorbing `explorer.js`'s `loadExplorer()` rendering, (b) a model list from `GET /api/models` with per-item "edit" (→ `openEditor("model", name)`) and a "+ new model" action (→ `openEditor("model", null)`), and (c) a common-model list from `GET /api/dimensions` with "edit" (→ `openEditor("bundle", name)`) and "+ new common model" (→ `openEditor("bundle", null)`).
- [X] T010 [US1] Fold `app/static/js/explorer.js` into the Modelling workspace: either import its table renderer from `modelling.js` or move the code in and delete the separate module; ensure the "open in builder" affordance still switches to Studio and selects the model.
- [X] T011 [US1] In `app/static/js/main.js`: remove the Studio-sidebar authoring event wiring (`#edit-model`/`#new-model`/`#new-bundle` listeners) and the `#new-bundle` sidebar hook; relocate any still-needed wiring into `modelling.js`. Keep `#model-select` in Studio.
- [X] T012 [P] [US1] In `app/static/style.css`: add Modelling-workspace layout styling (dataset overview + model/common-model lists + entry-point buttons), following the existing explorer/sidebar visual language.

**Checkpoint**: US1 fully testable on its own — MVP delivered. All model create/edit/delete reachable only from Modelling; Studio de-cluttered; visual-building regression passes.

---

## Phase 4: User Story 2 — Pick a source from available datasets (Priority: P2)

**Goal**: Author picks a grouped, drillable dataset from the bucket; the editor's `source:` block is filled in and the source's real columns become insertable.

**Independent Test**: Start a new model, pick a dataset from the browsable list, confirm `source` path+format populate and real columns appear — without typing a path.

### Tests for US2

- [X] T013 [P] [US2] In `tests/test_datasets.py` (new): unit-test pure helpers `group_objects` and `infer_format` in `app/semantic.py` — prefix grouping, parquet/csv extension inference, delta-root (`_delta_log/`) detection, mixed-extension `format_ambiguous`, and empty input.
- [X] T014 [P] [US2] In `tests/test_api.py`: add TestClient tests for `GET /api/datasets` against the `seeded` moto bucket — known seed prefixes appear with correct formats, a model's source location lists that model under `models`, response matches [contracts/datasets.md](./contracts/datasets.md), and empty-bucket → `datasets: []`.

### Implementation for US2

- [X] T015 [US2] Add pure helpers to `app/semantic.py`: `infer_format(keys) -> (format|None, ambiguous)` and `group_objects(objects) -> list[dataset dict]` (prefix grouping + glob path suggestion + delta-root handling), per [contracts/datasets.md](./contracts/datasets.md). No bucket/network access in these helpers.
- [X] T016 [US2] Implement `GET /api/datasets` in `app/api/datasets.py`: reuse `s3.client()` + `list_objects_v2` (as in `app/api/explorer.py`) and the Explorer model-matcher logic, group via `group_objects`, and return `{bucket, endpoint, datasets:[...]}`; fail loudly (`502`/surfaced note) when the bucket is unreachable.
- [X] T017 [US2] Add a `source:`-block text patcher to `app/static/js/editor.js`: given the current YAML, replace an existing top-level `source:` `path`/`format` or insert a `source:` block after the header; fail safe with a note when no parseable header exists (edge case: guided vs hand-edited YAML must never corrupt the doc).
- [X] T018 [US2] Add the dataset-picker affordance to `app/static/js/editor.js` (shown for `kind === "model"`): fetch `GET /api/datasets`, render grouped datasets (path/format/object-count/model-reads badge) with drill-down to individual objects, and on selection apply the source patch (T017) then re-validate; handle the empty-bucket state.
- [X] T019 [P] [US2] In `app/static/style.css`: style the dataset picker (grouped rows, drill-down, warning for `format_ambiguous`).

**Checkpoint**: US2 testable independently — a model can be sourced entirely by picking, with live columns; US1 unaffected.

---

## Phase 5: User Story 3 — Guided common-model import (Priority: P2)

**Goal**: Elevate the `dimension_imports` affordance to a first-class, guided pick-and-wire flow (common model → dataset → matching key column), with an empty state when no common models exist.

**Independent Test**: With a common model present, import a dataset on a chosen key from the editor and confirm imported dimensions become usable — without hand-writing the import block.

### Tests for US3

- [X] T020 [P] [US3] In `tests/test_api.py`: add a round-trip test — create/patch a fact model whose YAML includes a `dimension_imports` block referencing a seeded common model via `POST /api/models/validate` + `PUT`, and assert the imported dimensions appear on the model's `to_public()` and it loads (covers the import wiring the guided UI produces).

### Implementation for US3

- [X] T021 [US3] Elevate `renderImportPanel()` in `app/static/js/dimlab.js` into a guided flow: select common model → select dataset → specify the matching key column on the model (prefilled from the dataset's first dimension), then insert/patch the `dimension_imports` block via the editor's insert path; keep it consistent with the single-document model.
- [X] T022 [US3] Surface the import affordance prominently in the fact-model editor (in `app/static/js/editor.js`/`index.html` `#editor-imports` region), including the empty state that points to "create a common model" when `GET /api/dimensions` is empty.
- [X] T023 [P] [US3] In `app/static/style.css`: style the guided import (model/dataset/key selectors, empty state).

**Checkpoint**: US3 testable independently; import is guided, no raw-YAML knowledge required.

---

## Phase 6: User Story 4 — Expression intellisense in authoring (Priority: P2)

**Goal**: Context-aware completion + live validity in the YAML editor — polars completion inside `expr:` values, bare column-name completion in dimension/join/key contexts — reusing the Measure Lab engine, with invalid expressions blocked from silent save.

**Independent Test**: In the editor, `pl.col("` offers real columns, `.` offers methods, a column context offers bare names, accepting places the caret sensibly, and an invalid expression is flagged before save.

### Tests for US4

- [X] T024 [P] [US4] In `tests/test_static.py` (or `tests/test_measure_lab.py`): assert the shared completion module `app/static/js/completion.js` exports the vocabulary/engine and that `measurelab.js` imports it (guards against two divergent completion implementations). If the optional `GET /api/completion/methods` endpoint is added, test it in `tests/test_api.py` instead/additionally.

### Implementation for US4

- [X] T025 [US4] Extract the completion engine from `app/static/js/measurelab.js` into `app/static/js/completion.js`: move `TOP_FNS`/`METHODS` and the `suggestContext`/`updateSuggest`/`renderSuggest`/`applySuggest`/`hideSuggest` machinery into a reusable, parameterized form (textarea + item-source + column-source as inputs); refactor `measurelab.js` to consume it with no behavior change.
- [X] T026 [US4] Add a YAML-context classifier in `app/static/js/editor.js` (or `completion.js`): from caret position decide `expr` context (measure) → polars completion, vs column-name context (`name:`/`column:`, `on:`/`left_on:`/`right_on:`, spine `start:`/`end:`, geo `lat:`/`lon:`) → bare column names, vs none (per [contracts/completion.md](./contracts/completion.md)).
- [X] T027 [US4] Wire completion into `#yaml-editor` in `app/static/js/editor.js`: source columns from the latest `POST /api/models/validate` response (already fetched via `scheduleValidate`), methods from the shared vocabulary; keydown/blur handling mirrors the lab.
- [X] T028 [US4] Enforce save-guard in `app/static/js/editor.js`: when validation reports `ok: false`, block/warn on save so an invalid expression cannot be silently written (FR-015); valid save path unchanged.
- [X] T029 [P] [US4] (OPTIONAL, per research.md Decision 3) ~~Add `GET /api/completion/methods`~~ **Resolved by skipping** — the shared `completion.js` module (T025) is the single vocabulary source, asserted by the T024 test; a server endpoint was deemed unnecessary and not built.
- [X] T030 [P] [US4] In `app/static/style.css`: ensure the suggestion dropdown styles apply in the editor context (reuse `#lab-suggest` styling for the editor's suggestion box).

**Checkpoint**: US4 testable independently; intellisense works across the whole YAML with correct per-context suggestions.

---

## Phase 7: User Story 5 — Plain-text YAML editing stays first-class + unsaved-edit guard (Priority: P1)

**Goal**: Guarantee raw-YAML parity (validate/save/hot-reload/delete/revert) and add the warn-before-leaving guard so unsaved edits are never lost silently nor persisted silently.

**Independent Test**: Valid raw edit saves+reloads; invalid raw edit is reported and not persisted; navigating away while dirty warns; a reload drops unsaved edits but keeps saved ones.

### Tests for US5

- [X] T031 [P] [US5] In `tests/test_api.py`: assert raw-YAML round-trip parity is intact — `PUT /api/models/{name}/yaml` with valid YAML persists+reloads; invalid YAML returns 400 and does not change the file on disk (read back the origin file).

### Implementation for US5

- [X] T032 [US5] Add the `editor.dirty` lifecycle to `app/static/js/editor.js`: set dirty on `#yaml-editor` input, clear on open/save/revert (transitions per [data-model.md](./data-model.md)).
- [X] T033 [US5] Add the warn-before-leaving guard: intercept editor exit at the shared chokepoints — the `showView` transition in `app/static/js/state.js` (or a guard hook it calls), opening a different model/bundle, and a `window` `beforeunload` — and `confirm()` when `editor.dirty`; discard only on confirm, persist nothing.
- [X] T034 [US5] Verify guided affordances (US2 picker, US3 import, US4 suggestions) all write through the single `#yaml-editor` document so their results are visible/editable in the raw text (FR-017); adjust any affordance that bypassed the textarea.

**Checkpoint**: US5 testable independently; raw editing retains full parity and the ephemeral/persisted boundary is proven by reload.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, full-suite verification, and the browser golden paths required before "done".

- [X] T035 [P] Update `README.md` (Constitution Development Workflow): document the Modelling workspace, dataset picker, guided import, and editor intellisense; note DATA→MODELLING.
- [X] T036 Run the full suite: `pytest -q` all green; fix any regressions and add a regression test for each per Constitution III.
- [X] T037 Execute the browser golden paths GP1–GP6 in [quickstart.md](./quickstart.md) with a zero-console-errors check; capture the required screenshots (Studio sans authoring buttons, Modelling workspace, picker inserting a source, intellisense dropdown, warn-before-leaving dialog).
- [X] T038 [P] Remove any dead code left by the explorer fold-in (T010) and confirm no orphaned imports/handlers remain in `main.js`/`explorer.js`.

---

## Dependencies & Execution Order

- **Setup (Phase 1)** → **Foundational (Phase 2)** → user stories.
- **Foundational blocks everything**: T004–T006 (rename plumbing + shared state) must land before Phase 3+.
- **US1 (Phase 3)** is the MVP and the home for the affordances; recommended first. US2/US3/US4 modify `editor.js`/`dimlab.js`, which are reachable during development regardless, so they can be built in parallel branches after Foundational — but they *land* most coherently after US1 so their entry points live in the final IA.
- **US5 (Phase 7)** depends on the affordances existing (T034 verifies all of US2/US3/US4 route through the single document) and on the editor from Foundational; the dirty-guard itself (T032–T033) only needs Foundational.
- **US2** internal order: T015 (helpers) → T016 (endpoint) → T017 (patcher) → T018 (picker UI). Tests T013/T014 can be written first (TDD) or alongside.
- **US4** internal order: T025 (extract engine) → T026 (classifier) → T027 (wire) → T028 (save-guard).
- **Polish (Phase 8)** last.

### Story independence

| Story | Depends on | Independently testable? |
|-------|-----------|--------------------------|
| US1 (P1) | Foundational | Yes — IA move + workspace |
| US2 (P2) | Foundational (US1 for final home) | Yes — pick a dataset → source+columns |
| US3 (P2) | Foundational (US1 for final home) | Yes — guided import |
| US4 (P2) | Foundational (US1 for final home) | Yes — completion + save-guard |
| US5 (P1) | Foundational; T034 needs US2–US4 | Yes — raw parity + dirty guard |

## Parallel Execution Examples

- **Setup**: T002, T003 in parallel (different new files).
- **US1**: T007 (test) and T012 (css) in parallel with T008–T011 authoring; core JS tasks T008–T011 touch overlapping files, keep sequential.
- **US2**: T013, T014 (tests, different files) in parallel; T019 (css) parallel to backend T015/T016.
- **Across stories (post-Foundational)**: backend US2 (T015/T016) can proceed in parallel with US4's engine extraction (T025) — different files, no shared state.

## Implementation Strategy

**MVP = Phase 1 + Phase 2 + Phase 3 (US1).** That alone satisfies the explicit IA request (authoring out of Studio, into a renamed Modelling workspace) and is shippable. Layer US2 (dataset picker) next for the biggest delight win, then US3 (import) and US4 (intellisense) in either order, and finish with US5's guard + parity verification. Each story is a self-contained increment that can be demoed and browser-verified on its own per [quickstart.md](./quickstart.md).
