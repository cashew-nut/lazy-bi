# Implementation Plan: Polars Pipeline Module

**Branch**: `claude/polars-pipeline-module-dacwj2` (spec dir `014-polars-pipeline-module`) | **Date**: 2026-07-17 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/014-polars-pipeline-module/spec.md`

## Summary

Host user-authored polars transformation scripts as first-class **pipelines**:
YAML files in a new `pipelines/` directory (script embedded, hot-reloadable
like models), executed on manual trigger as strictly serialized background
jobs — each run in a killable subprocess that scans the declared sources
lazily, execs the script (`sources` in → `output` frame out), and lets the
*platform* materialize the result: `replace` (delta overwrite / atomic
parquet PUT) or `upsert` (delta merge by key with four delete policies:
ignore / sync / soft-delete / predicate). Declared field-level lineage is
validated against the real output schema on every run and regenerated into a
pipeline-owned `pipeline_lineage:` section of the target's model YAML; an
optional global layer list (bronze/silver/gold …) organizes datasets, and a
new hand-rolled SVG lineage graph in the Modelling workspace renders
datasets/models as nodes, pipelines as status-bearing edges, expandable to
field level across hops. Admin-gated end to end (Principle VI re-opened),
fully audited, run history in SQLite.

## Technical Context

**Language/Version**: Python 3.12 (`python:3.12-slim` image), browser ES modules (no build step)

**Primary Dependencies**: FastAPI 0.139, polars 1.42.1, deltalake 1.6.1 (already a dependency — provides `DeltaTable.merge` incl. `when_not_matched_by_source_*`), boto3, PyYAML, moto (tests/demo emulator). **No new dependencies.**

**Storage**: S3 bucket (emulated or real) for pipeline targets (Delta tables / parquet objects); SQLite `cash_intel.db` for `pipeline_runs` + existing `audit_events`; `pipelines/*.yaml` + `pipelines/layers.yaml` files as the definition contract; target model YAML gains a regenerated `pipeline_lineage:` section

**Testing**: pytest — semantic parsing, materialization against the moto-emulated bucket (real delta merges), run lifecycle, lineage YAML surgery, API surface via TestClient, role-matrix extension

**Target Platform**: Linux server, single Docker image, one uvicorn worker (deliberate); runs spawn short-lived local subprocesses

**Project Type**: web app — FastAPI backend (`app/`, one router per resource) + vanilla-JS SVG frontend (`app/static/js/`)

**Performance Goals**: run execution is batch (minutes-scale OK, timeout-bounded); pipeline CRUD/graph endpoints interactive (<500ms against loaded registry); graph renders 50+ nodes without jank

**Constraints**: single writer (SQLite + emulator) — runs strictly serialized platform-wide, all SQLite writes from the parent process; failed/timed-out/interrupted runs must never corrupt a target (transactional delta writes, atomic parquet PUT); no new frontend dependencies

**Scale/Scope**: tens of pipelines, run history in the thousands of rows, graph of ~10–100 nodes; not a distributed orchestrator

## Constitution Check

*GATE: evaluated pre-Phase-0 and re-checked post-Phase-1 — PASS (two principles deliberately engaged, both documented in the spec's Constitution Notes).*

- **I. Semantic layer is the only contract** — PASS. Pipelines produce
  *datasets*; querying them still goes through model YAML. The
  `pipeline_lineage:` model section is documentation parsed and surfaced,
  never a query path. New data onboarding remains "write a model".
- **II. Lazy evaluation, pushdown by default** — PASS with documented
  engagement. The query engine is untouched. Pipelines materialize by
  declared purpose (write jobs, not query paths); scripts receive
  `LazyFrame`s so their reads keep pushdown until the run's single collect.
  Spec Constitution Notes call this out explicitly per the governance rule.
- **III. Every feature ships with tests** — planned: `tests/test_pipelines.py`
  (YAML parse/validate, materialization matrix against moto, run lifecycle,
  lineage validation + YAML surgery), `tests/test_pipeline_api.py`
  (TestClient CRUD/run/graph), `tests/test_role_matrix.py` additions.
- **IV. Browser-verified before done** — quickstart.md defines the golden
  path (create → run → query → lineage section → graph), persistence
  round-trip (cold reload) and zero-console-errors check.
- **V. Ephemeral vs. persisted is deliberate** — persisted: pipeline files,
  run history, layer file, model lineage section. Ephemeral: graph
  selection/field-expansion state, run-panel polling state (reset on
  reload). Stated here up front.
- **VI. Trusted-config boundary never silently widened** — **explicitly
  re-opened** (spec Constitution Notes): pipeline scripts are a new
  eval-capable construct at application-code trust; every mutation path
  (pipeline CRUD via API, raw YAML PUT, run trigger) is admin-only; no
  inline/query-time script path exists at all. Constitution amendment to be
  recorded when the feature ships (same pattern as spec 008/011).
- **VII. Feature branch** — PASS: `claude/polars-pipeline-module-dacwj2`.

**Technology constraints**: no new backend deps; frontend stays hand-rolled
SVG ES modules; single image, state outside the image (`pipelines/` becomes a
mounted dir like `models/`); delta/parquet/csv sources remain first-class as
pipeline *sources* (csv excluded only as a *target*).

## Project Structure

### Documentation (this feature)

```text
specs/014-polars-pipeline-module/
├── spec.md              # Feature specification
├── plan.md              # This file
├── research.md          # Phase 0 — decisions R1–R9
├── data-model.md        # Phase 1 — entities, schemas, state machines
├── quickstart.md        # Phase 1 — end-to-end validation guide
├── contracts/
│   ├── pipeline-yaml.md # Pipeline file + layers file + model lineage section formats
│   └── pipelines-api.md # HTTP API + runner subprocess protocol
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
app/
├── pipelines.py             # NEW: pipeline + layers YAML parsing/validation,
│                            #      lineage declarations, target→model matching
├── pipeline_runner.py       # NEW: subprocess entry (python -m app.pipeline_runner):
│                            #      scan sources → exec script → materialize → JSON result
├── materialize.py           # NEW: replace/upsert writers (delta overwrite, parquet PUT,
│                            #      DeltaTable.merge + delete policies, pre-write guards)
├── pipelinestore.py         # NEW: SQLite pipeline_runs table (append-only lifecycle)
├── pipeline_jobs.py         # NEW: FIFO queue + worker thread, subprocess supervision,
│                            #      timeout kill, startup interrupted-run sweep
├── semantic.py              # MOD: parse/expose model pipeline_lineage section;
│                            #      replace_lineage_yaml() text surgery
├── registry.py              # MOD: pipelines + layers in reload_all(); pipeline_store
├── config.py                # MOD: PIPELINES_DIR, PIPELINE_TIMEOUT_DEFAULT
├── main.py                  # MOD: lifespan starts/stops job worker, interrupted sweep
├── seed.py / pipelines/     # MOD/NEW: demo pipeline chain + layers.yaml (R9)
└── api/
    ├── __init__.py          # MOD: register pipelines router
    └── pipelines.py         # NEW: CRUD/validate/run/runs/graph/layers routes

app/static/js/
├── pipelines.js             # NEW: Modelling rail section, pipeline YAML editor
│                            #      (reuses editor.js machinery), run panel + history
├── lineagegraph.js          # NEW: hand-rolled SVG layered DAG, field expansion
├── modelling.js / router.js # MOD: /modelling/pipeline/{name}, /modelling/lineage
└── main.js                  # MOD: wire new views

pipelines/                   # NEW top-level dir (host-mounted like models/)
├── layers.yaml              # demo: bronze/silver/gold
└── *.yaml                   # demo pipeline chain

tests/
├── test_pipelines.py        # NEW: parse/validate, materialization matrix, runs, lineage
├── test_pipeline_api.py     # NEW: API surface via TestClient
└── test_role_matrix.py      # MOD: new routes' role expectations
```

**Structure Decision**: extends the established one-router-per-resource
FastAPI layout and the files-as-contract pattern (`pipelines/` sits beside
`models/` and `dimensions/`); execution machinery is split runner (child
process) / jobs (parent supervision) / materialize (write semantics) so the
write path is testable without a subprocess and the subprocess protocol stays
thin. Frontend follows the existing one-module-per-surface convention.

## Complexity Tracking

No constitution violations requiring justification. The one structural
addition beyond a typical resource — the subprocess runner — is required by
FR-015 (enforceable timeout; threads cannot be killed) and crash isolation,
documented in research R2.
