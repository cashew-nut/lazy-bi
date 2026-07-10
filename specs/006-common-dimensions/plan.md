# Implementation Plan: Common Dimensional Models

**Branch**: `feature/common-dimensions` | **Date**: 2026-07-10 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/006-common-dimensions/spec.md`

## Summary

Add a second kind of semantic YAML unit — a **dimension bundle** (`dimensions/*.yaml`) — that declares one or more **datasets** (source + dimensions) and the joins between them, independent of any fact model. A fact model gains a new `dimension_imports` block that names a bundle, an **anchor** (how the fact model's own source connects into one dataset in the bundle), and an optional subset of the bundle's datasets. At load time, an imported bundle's dimensions are merged into the importing model's own `dimensions` dict (native dimensions win on name collision), and at query time `engine.scan()` extends the existing join chain to include the bundle's datasets, joined to each other exactly as the bundle declares and then into the fact model via the anchor. No new query API, no new UI surface, no new trust boundary — this is an extension of the existing `Model`/`Join` mechanism, not a parallel system.

## Technical Context

**Language/Version**: Python 3.12 (unchanged — matches the existing Docker image and app code)

**Primary Dependencies**: FastAPI, Polars, PyYAML (all existing — no new dependency)

**Storage**: Bundle definitions are files (`dimensions/*.yaml`), same as models — not a database concern. No SQLite schema change (visuals/dashboards are unaffected; they already just reference dimension *names*, which continue to work whether a dimension is native or imported).

**Testing**: pytest, extending the existing `tests/` suite (semantic parsing, engine, API) — same pattern as every prior feature.

**Target Platform**: Unchanged — same single-worker Docker image, same embedded-emulator/external-S3 duality.

**Project Type**: Web service (FastAPI backend + vanilla-ES-module frontend) — unchanged.

**Performance Goals**: A query against a fact model with one bundle import (2-3 datasets) MUST NOT regress the existing grand-totals/trend benchmarks in [002](../002-time-spine-dashboard-views/spec.md) by more than the cost of the extra join(s) — i.e. still comfortably interactive, not a new order of magnitude.

**Constraints**: All new joins (bundle-internal and anchor) MUST stay lazy — no `.collect()` before the final query execution, consistent with Principle II. Import resolution (merging a bundle's dimensions into a model) is a load-time/hot-reload-time cost, not a per-query cost.

**Scale/Scope**: Bundles are expected to hold a handful of datasets (the examples given are 3: accounts/opportunities/products, or studies/study_countries/study_sites) — not hundreds. No pagination or lazy-bundle-loading concerns at this scale.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design — no changes to this table were needed after design.*

| Principle | Check | Notes |
|---|---|---|
| I. Semantic layer is the only contract | **PASS** | Imported dimensions are merged into `Model.dimensions` before the API/builder ever see them — the builder, query API, and store gain no new code path. A dimension is a dimension regardless of origin. |
| II. Lazy evaluation, pushdown by default | **PASS** | Bundle-internal and anchor joins are additional `.join()` calls on the existing lazy chain in `engine.scan()`, built before the one `.collect()` at the end of `run_query`. No eager materialization introduced. |
| III. Every feature ships with tests | **PASS (commitment)** | Plan includes semantic-parsing tests (bundle YAML, cycle/collision rejection), engine tests (multi-dataset join resolution, subset import), and an API test — see tasks.md. |
| IV. Browser-verified before done | **PASS (commitment)** | Implementation phase verifies in-browser: imported dimensions appear in the builder, a query using one returns correct joined values, and the Data explorer correctly attributes bundle-sourced files (see below). |
| V. Ephemeral vs persisted state | **N/A** | This feature adds no interactive/session state — it is a semantic-layer/data-modeling change, not a UI-interaction feature. |
| VI. Trusted-config security boundary | **PASS** | Bundle YAML is trusted configuration at the same level as model YAML — loaded from the same trusted `dimensions/` directory server-side. No new input surface, no widening of the eval trust boundary (bundles declare no measures, so no new `eval` surface at all). |
| VII. Feature branches | **PASS** | This work is on `feature/common-dimensions`, branched from `main` after PR #2 merged. |

No violations — Complexity Tracking table is empty (see bottom).

## Project Structure

### Documentation (this feature)

```text
specs/006-common-dimensions/
├── spec.md                     # Feature specification
├── plan.md                     # This file
├── data-model.md               # Phase 1: new entities and how they extend Model
├── contracts/
│   └── api-changes.md          # Phase 1: new/changed HTTP endpoints
├── quickstart.md               # Phase 1: runnable end-to-end validation
├── checklists/
│   └── requirements.md         # Spec quality checklist (already passed)
└── tasks.md                    # Phase 2 output (/speckit-tasks) — not created by this file
```

### Source Code (repository root)

```text
dimensions/                     # NEW — bundle YAML, parallel to models/
└── *.yaml                      # e.g. dimensions/geography.yaml, dimensions/sales_dimensions.yaml

app/
├── config.py                   # + DIMENSIONS_DIR
├── semantic.py                 # + Dataset, DatasetJoin, DimensionBundle, Import dataclasses;
│                                #   + load_dimension_bundles(), resolve_imports()
├── engine.py                   # scan() extended to resolve a model's imports into the join chain
├── registry.py                 # + dimension_bundles dict; reload order: bundles, then models
│                                #   (models validate their imports against already-loaded bundles)
├── api/
│   ├── dimensions.py           # NEW router — GET /dimensions, POST /dimensions/reload,
│   │                           #   GET/PUT /dimensions/{name}/yaml (read/author bundles;
│   │                           #   mirrors the read/write shape of api/models.py, no new UI
│   │                           #   required to consume it per spec Assumptions)
│   ├── models.py               # to_public() gains an "imports" field; no other endpoint changes
│   │                           #   (validate/create/put already funnel through _parse_or_400 +
│   │                           #   reload, which now also resolves imports)
│   └── explorer.py             # matcher-building extended to also cover each imported bundle's
│                                #   dataset sources, so bundle-sourced files aren't misflagged
│                                #   "unmapped" (see contracts/api-changes.md)
└── main.py                     # app-factory startup: load bundles before models

tests/
├── test_semantic.py            # + bundle parsing: datasets, inter-dataset joins, cycle/
│                                #   collision rejection, import resolution onto a Model
├── test_engine.py              # + query correctness through an anchor join and a bundle-
│                                #   internal join together; subset-import correctness
├── test_api.py                 # + /dimensions endpoints; /models list surfaces imports
└── test_explorer.py            # (or existing explorer coverage) + bundle-sourced files
                                 #   attributed correctly, not "unmapped"

dimensions/geography.yaml       # Demo bundle for quickstart.md (see that file) — imported into
                                 # 2+ existing fact models to prove cross-model reuse (spec SC-001,
                                 # SC-003), without touching those models' existing inline joins.
```

**Structure Decision**: Single project, extending the existing FastAPI backend in place — no new service. The query builder needs zero changes (imported dimensions arrive through the same `Model.dimensions` dict it already renders), but the **authoring UI is now in scope** (spec User Story 4, added after first use) and touches the existing frontend:

```text
app/static/
├── index.html                  # + sidebar "Common Dimensions" section (bundle list + "new");
│                                #   editor side gains a "Common Dimensions" import panel
├── js/editor.js                # generalized to editor.kind = "model" | "bundle":
│                                #   per-kind template / validate / save / delete endpoints
├── js/dimlab.js  (NEW)         # bundle list rendering + the import-into-model panel
├── js/main.js                  # wiring for the new controls
└── style.css                   # reuses existing editor/chip/col-chip classes

app/api/dimensions.py           # + POST /dimensions (create), POST /dimensions/validate
│                                #   (per-dataset column introspection), DELETE /dimensions/{name}
│                                #   (refused when a model imports it — spec FR-017)
app/engine.py                   # + scan_source() public helper (per-dataset schema for validate)
```

The authoring surface deliberately reuses the fact-model editor's YAML-with-live-assists pattern rather than a form builder (spec Assumptions) — `editor.js` is generalized over a `kind` rather than duplicated, so bundles and models share one editor with per-kind endpoints. Bundles are kept out of the builder's model `<select>` by construction: it is populated from `/api/models`, and bundles only ever come from `/api/dimensions` (spec FR-016).

## Complexity Tracking

*No constitution violations — table intentionally empty.*

One design risk worth naming even though it isn't a constitution violation: resolving "which dataset joins to which, in which direction" when a fact model anchors to a dataset other than the first one declared in the bundle requires a graph walk (BFS from the anchor dataset over the bundle's declared edges), not a simple ordered list of joins like today's `Model.joins`. This is more complex than anything the join mechanism has done before, but it is the direct, necessary consequence of spec requirement FR-006 ("every dataset in the bundle... including datasets reachable only via the bundle's own internal joins") — there is no simpler alternative that still satisfies that requirement, so it is scoped as a normal implementation task (see tasks.md) rather than a complexity violation to justify away.
