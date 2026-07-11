# Implementation Plan: Modelling Workspace — Delightful Model Creation & Editing

**Branch**: `feature/modelling-workspace` | **Date**: 2026-07-10 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/007-modelling-workspace/spec.md`

## Summary

Consolidate all semantic-model authoring into a renamed **Modelling** workspace (today's "Data" / Explorer mode), removing the three authoring controls from Studio's sidebar. Within Modelling, make authoring delightful without abandoning raw YAML: a **dataset picker** that groups bucket objects into pickable glob datasets (drillable to a single object) and writes the `source` block; an elevated, guided **common-model import**; and **expression intellisense** that fires anywhere in the single YAML editor — polars completion inside `expr:` values and bare column-name completion in dimension/join/key contexts — plus a warn-before-leaving guard on unsaved edits. The YAML textarea remains the single source of truth; every affordance inserts/patches that text, so guided and textual views cannot diverge. Backend adds one read-only `/api/datasets` endpoint (grouping the objects the Explorer already enumerates) and a completion-vocabulary endpoint; everything else reuses the existing `/api/models` + `/api/dimensions` validate/CRUD surface.

## Technical Context

**Language/Version**: Python 3.12 (backend), ES2020 vanilla modules (frontend) — matches the repo's existing runtime; note the local dev box runs 3.10 (see project memory) but the Docker/target image is `python:3.12-slim`.

**Primary Dependencies**: FastAPI + Polars + PyYAML + boto3 (backend); no frontend dependencies — hand-rolled ES modules loaded natively, no bundler/framework/build step (Constitution Technology Constraints).

**Storage**: Model YAML files on the host (`models/*.yaml`, `dimensions/*.yaml`) remain the single editable contract. No new persistence store. S3/bucket objects (via moto emulator locally) provide the dataset list. SQLite is untouched.

**Testing**: pytest against a moto-emulated bucket (`tests/conftest.py` fixtures: `moto_server`, `seeded`, TestClient). New API tests join `tests/test_api.py`; semantic/grouping helpers get unit coverage in `tests/test_semantic.py` or a new `tests/test_datasets.py`. Frontend is verified in-browser (Constitution IV) and by the existing `tests/test_static.py` smoke checks.

**Target Platform**: Single Docker image, one uvicorn worker, served to a desktop browser.

**Project Type**: Web application (FastAPI backend + static ES-module frontend in one app).

**Performance Goals**: Preserve lazy/pushdown (Constitution II) — no change to the query path; the dataset list is a bucket `list_objects_v2` walk (same call the Explorer already makes), not a data scan. Column introspection for completion uses `collect_schema()` on a `LazyFrame` (metadata only, no materialization), exactly as today's validate/schema endpoints.

**Constraints**: No build step; no new framework; trusted-config eval boundary unchanged (Constitution VI — models stay single-user, developer-authored; intellisense surfaces the same `pl`-only eval, does not widen who authors expressions). Warn-before-leaving on unsaved edits is session-only ephemeral state (Constitution V).

**Scale/Scope**: Demo bucket ~ tens–hundreds of objects; grouping is O(objects). Frontend adds ~1 new module (modelling workspace) and elevates 2 existing ones (editor, dimlab); backend adds 1–2 read-only endpoints.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment |
|-----------|------------|
| **I. Semantic layer is the only contract** | ✅ Reinforced. All authoring still flows through model/bundle YAML; the dataset picker only fills a model's `source`, and completion only *suggests* columns — nothing lets the query builder reference an undeclared column (FR-022). |
| **II. Lazy/pushdown by default** | ✅ Unaffected. No query-path change. Dataset listing is a bucket object walk; column completion uses `collect_schema()` (no scan). No full-table materialization introduced. |
| **III. Every feature ships with tests** | ✅ Planned. New `/api/datasets` + completion endpoint get TestClient coverage against the moto bucket; grouping/format-inference helper gets unit tests; regressions found in browser verification land as tests. |
| **IV. Browser-verified before done** | ✅ Planned. quickstart.md defines the end-to-end golden path (create a model from a picked dataset → save → cold reload → build a visual in Studio) plus a zero-console-errors check. |
| **V. Ephemeral vs persisted is deliberate** | ✅ Explicit. Unsaved editor state is ephemeral (warn-before-leaving, never written unless saved); saved YAML persists and must survive reload (FR-021). Recorded in data-model.md. |
| **VI. Trusted-config eval boundary** | ✅ Not widened. Completion/validation reuse the existing `pl`-scoped `compile_expr`; no new actor can influence YAML or expressions. Called out explicitly. |
| **VII. Feature branch per development** | ✅ On `feature/modelling-workspace`, merges via PR. |

**Technology-constraint checks**: one router per resource (`app/api/datasets.py` new; existing routers reused), runtime state in `registry.py` (unchanged), no bundler/framework on the frontend (new UI is vanilla ES modules following the existing pattern), parquet/csv/delta all first-class in format inference.

**Result**: PASS — no violations, Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/007-modelling-workspace/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── datasets.md          # GET /api/datasets (new)
│   ├── completion.md        # GET /api/completion/methods (new) + reuse of /validate
│   └── existing-reuse.md    # models/dimensions endpoints reused as-is
├── checklists/
│   └── requirements.md  # from /speckit-specify
└── tasks.md             # /speckit-tasks output (not created here)
```

### Source Code (repository root)

```text
app/
├── api/
│   ├── datasets.py        # NEW: GET /api/datasets — bucket objects grouped into pickable datasets
│   ├── models.py          # reused (validate already returns columns); add completion vocab if backend-served
│   ├── dimensions.py      # reused as-is (bundle validate/CRUD)
│   └── explorer.py        # kept (Modelling absorbs its data-overview view)
├── s3.py / config.py      # reused for bucket walk + BUCKET/endpoint
├── semantic.py            # add a small, pure grouping/format-inference helper (unit-testable)
└── static/
    ├── index.html         # rename DATA→MODELLING nav; move 3 authoring buttons out of Studio sidebar into the Modelling view
    ├── js/
    │   ├── modelling.js   # NEW: Modelling workspace shell — datasets + models + common models + authoring entry points (absorbs explorer.js view)
    │   ├── editor.js      # ELEVATED: dataset-picker affordance, warn-before-leaving, YAML-context intellisense
    │   ├── dimlab.js      # ELEVATED: guided import (already inserts import block; make it first-class in the editor)
    │   ├── measurelab.js  # completion engine extracted/shared (see research.md) — no behavior change to the lab
    │   ├── explorer.js     # folded into modelling.js (or kept as the data-overview sub-view)
    │   ├── main.js        # rewire nav + move authoring event wiring out of Studio
    │   └── state.js       # view rename data→modelling; dirty-edit flag
    └── style.css          # Modelling workspace + picker/intellisense styling

tests/
├── test_api.py           # + /api/datasets, + completion vocab, + editor-flow round-trips
├── test_semantic.py      # + grouping/format-inference unit tests (or test_datasets.py)
└── test_static.py        # + smoke assertions for moved controls / renamed nav
```

**Structure Decision**: Existing single-app web layout (FastAPI `app/` + static ES-module frontend under `app/static/`). This feature adds one backend router (`datasets.py`) and one pure helper in `semantic.py`, and on the frontend adds one workspace module (`modelling.js`) while elevating `editor.js`/`dimlab.js` and extracting the reusable completion engine currently inside `measurelab.js`. No new top-level directories, no build tooling.

## Complexity Tracking

*No constitutional violations — section intentionally empty.*
