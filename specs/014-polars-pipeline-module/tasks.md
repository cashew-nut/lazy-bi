# Tasks: Polars Pipeline Module

**Input**: Design documents from `/specs/014-polars-pipeline-module/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED — Constitution Principle III mandates pytest coverage alongside every feature; quickstart.md defines the browser verification (Principle IV).

**Organization**: Tasks are grouped by user story so each story is an independently testable increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 (author & run), US2 (upsert + delete handling), US3 (lineage + layers), US4 (graph)

## Phase 1: Setup

**Purpose**: Configuration and directory scaffolding every story needs

- [X] T001 Add `PIPELINES_DIR` (default `PROJECT_ROOT / "pipelines"`, env `CI_PIPELINES_DIR`) and `PIPELINE_TIMEOUT_DEFAULT = 600` / `PIPELINE_TIMEOUT_MAX = 3600` to `app/config.py`
- [X] T002 [P] Create `pipelines/` top-level directory (with a `README`-style comment header in a placeholder `layers.yaml`, demo content arrives in T045) and mount it in `docker-compose.yml` + include it in `Dockerfile` COPY, mirroring how `models/` is handled

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The pipeline definition contract, run persistence, and registry wiring — every story builds on these

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T003 Implement `app/pipelines.py`: dataclasses (`Pipeline`, `PipelineSource`, `Target`, `Materialization`, `LineageEntry`, `Layer`) + YAML parsing/validation per `contracts/pipeline-yaml.md` and `data-model.md` — field rules, materialization cross-rules (upsert⇒delta, parquet⇒replace, csv target rejected, soft_delete/predicate/keys requirements), script syntax check via `compile(..., "exec")` (pattern: `semantic.validate_frame`), `load_pipelines(dir)` + `load_layers(dir)` with unique-name and unique-target-path validation across the loaded set, layer references checked against `layers.yaml`. `load_pipelines` MUST explicitly skip `layers.yaml` (and any other reserved filename) when globbing `pipelines/*.yaml` — it is not itself a pipeline definition; an empty/comment-only `layers.yaml` is equivalent to the file being absent.
- [X] T004 [P] Implement `app/pipelinestore.py`: `PipelineStore` on `cash_intel.db` with append-only `pipeline_runs` table per `data-model.md` (including the `output_schema` JSON column) — `create_run` (queued), `mark_running`, `finish_run(status, stats, lineage, output_schema, error)`, `sweep_interrupted()` (any queued/running → interrupted), `runs_for(pipeline, limit)`, `get_run(id)`, `pending_for(pipeline)` (follow `app/store.py` conventions: `_conn`, Row factory, ISO timestamps). `pending_for(pipeline)` backs the same-pipeline 409 in T011 (a pipeline with a queued/running row refuses a new trigger; a different pipeline's trigger still queues platform-wide).
- [X] T005 Wire the registry in `app/registry.py`: `self.pipelines`, `self.layers`, `self.pipeline_store`; load both in `reload_all()` (after models — target→model matching needs models loaded) and init the store in `init()`
- [X] T006 [P] Foundational tests in `tests/test_pipelines.py`: YAML parse happy path, every cross-rule rejection (with message asserts), duplicate name / duplicate target-path rejection, layers file parse + unknown-layer reference error, script syntax error rejection, `PipelineStore` run lifecycle CRUD + `sweep_interrupted`

**Checkpoint**: Definitions parse and validate; runs persist — user story implementation can begin

---

## Phase 3: User Story 1 - Author and run a hosted transformation (Priority: P1) 🎯 MVP

**Goal**: Admins create/edit/delete pipelines in the Modelling workspace, trigger runs, and watch serialized background execution write `replace`-mode targets atomically, with full run history and audit

**Independent Test**: quickstart.md golden path steps 1–5 — create a pipeline joining two seeded datasets, run it, query the target through a model, see run history; failing script leaves the target intact

- [X] T007 [P] [US1] Implement `replace` materialization in `app/materialize.py`: delta `write_deltalake(mode="overwrite", schema_mode="overwrite", storage_options=config.delta_write_options())` and parquet buffer + single `put_object` via `app/s3.py` (pattern: `seed.py`); returns `{rows_written}`; collect the script's `output` (LazyFrame → collect) here
- [X] T008 [P] [US1] Implement `app/pipeline_runner.py` (subprocess entry, `python -m app.pipeline_runner`): read one JSON job from stdin per `contracts/pipelines-api.md` runner protocol; build `sources` dict of LazyFrames from the job's storage config; `exec` the script in a `{sources, pl}` namespace; require `output` LazyFrame/DataFrame; call `materialize`; print one JSON result line (`ok, rows_written, rows_deleted, rows_flagged, output_schema, error`) — never import `app.registry`/FastAPI (config-complete from stdin)
- [X] T009 [US1] Implement `app/pipeline_jobs.py`: FIFO `queue.Queue` + single daemon worker thread; per job: mark_running → spawn runner subprocess with job JSON on stdin → wait with `timeout_seconds` → on timeout `kill()` + finish `timed_out` → parse stdout JSON → finish `succeeded`/`failed`; parent owns ALL SQLite writes; `start_worker()`/`stop_worker()`; startup calls `store.sweep_interrupted()` (depends on T007, T008)
- [X] T010 [US1] Wire lifecycle in `app/main.py` lifespan: after `registry.init()`, sweep interrupted runs and start the job worker; stop it on shutdown
- [X] T011 [US1] Implement `app/api/pipelines.py` router + register in `app/api/__init__.py`: `GET /pipelines` (any role, latest-run summary), `POST /pipelines` (admin, validate→write file→hot-reload, 409 duplicate name/target), `GET/PUT /pipelines/{name}/yaml` (PUT admin, name immutable), `DELETE /pipelines/{name}` (admin, 409 while queued/running), `POST /pipelines/validate` (any, parse-only), `POST /pipelines/reload` (admin), `POST /pipelines/{name}/run` (admin, 202 `{run_id, status: "queued"}`; enqueue platform-wide — a run for a *different* pipeline queues behind whatever is executing; 409 if *this* pipeline already has a queued/running row via `pending_for`), `GET /pipelines/{name}/runs`, `GET /runs/{id}` — audit `pipeline.create/update/delete/run` via `registry.auth_store.record_audit` (pattern: `app/api/models.py`)
- [X] T012 [US1] Extend `tests/test_role_matrix.py` with every new route's role expectations (mutations+run admin; reads any authenticated role; validate any)
- [X] T013 [P] [US1] Run-lifecycle + materialization tests in `tests/test_pipelines.py`: replace-mode delta and parquet writes against the moto bucket (target readable back via `pl.scan_*`), failing script → `failed` with error + pre-run target intact (SC-003), timeout (`timeout_seconds: 1` + sleeping script) → `timed_out` + process gone, missing/wrong-typed `output` → failed, serialized execution (second trigger queues), sweep on synthetic stale rows
- [X] T014 [P] [US1] API tests in `tests/test_pipeline_api.py` via TestClient: CRUD round-trip incl. 409s (duplicate name/target, delete-while-pending, run-while-pending), validate endpoint ok/error shapes, run trigger → poll to terminal status, audit rows recorded
- [X] T015 [US1] Frontend `app/static/js/pipelines.js`: pipelines section in the Modelling left rail (list + latest-run status chip, admin-only `+ PIPELINE`), pipeline YAML editor reusing the live-validation editor machinery from `app/static/js/editor.js` (validate endpoint on keystroke, unsaved-edit guard), RUN button + run history panel with polling while a run is live (polling state ephemeral per Constitution V)
- [X] T016 [US1] Wire routes/views: `app/static/js/router.js` (`/modelling/pipeline/{name}`), `app/static/js/modelling.js` (rail section mount), `app/static/js/main.js` (module wiring); role-gate all mutation controls off `/api/auth/me`
- [X] T017 [US1] Browser-verify US1 per quickstart.md steps 1–5 + failure/timeout checks: golden path, cold-restart persistence of pipeline + run history, zero console errors; fix what surfaces

**Checkpoint**: MVP — hosted scripts run, materialize, and are audited end to end

---

## Phase 4: User Story 2 - Incremental upsert with delete handling (Priority: P2)

**Goal**: `upsert` mode merges output into the target by key with `ignore` / `sync` / `soft_delete` / `predicate` delete policies, guarded against bad keys, schema drift, and accidental truncation

**Independent Test**: quickstart.md step 6 + safety checks — seed via replace, flip to upsert, verify update/insert/missing-key behavior under each policy; guards fail the run before any target change

- [X] T018 [US2] Implement upsert in `app/materialize.py` per research R4: `DeltaTable.merge(predicate on keys)` + `when_matched_update_all` + `when_not_matched_insert_all`; policies — `sync` → `when_not_matched_by_source_delete()`, `soft_delete` → `when_not_matched_by_source_update({flag: True})`, `predicate` → `DeltaTable.delete(delete_predicate)` before merge. The `soft_delete_column` is platform-managed and never appears in the script's output, so `when_matched_update_all` alone cannot clear it on a reappearing key — before merging, add a literal `pl.lit(False).alias(soft_delete_column)` column to the output frame (or use an explicit per-column update mapping instead of `update_all`) so a matched row's flag is always driven back to false. Return `{rows_written, rows_deleted, rows_flagged}` from merge metrics.
- [X] T019 [US2] Pre-write guards in `app/materialize.py` (run before any target modification, error strings name the rule): null/duplicate key values in output → fail; output schema incompatible with existing target → fail with schema diff; empty output + `sync` without `allow_empty_sync` → fail; missing target on first upsert run → create (initial write)
- [X] T020 [US2] Upsert matrix tests in `tests/test_pipelines.py` against the moto bucket (SC-002): all four policies × {updated row, new row, missing key} asserting exact target state, soft-delete flag set then cleared on reappearance, predicate pre-delete, plus every guard (null keys, dup keys, schema diff, empty-sync halt, empty-sync with opt-in)
- [X] T021 [P] [US2] Surface delete-policy effects in `app/static/js/pipelines.js` run history (rows deleted / flagged columns) and verify in browser per quickstart step 6

**Checkpoint**: US1 + US2 — both materialization modes fully functional and guarded

---

## Phase 5: User Story 3 - Field-level lineage documented in the target model (Priority: P3)

**Goal**: Declared lineage validates against the real output schema every run, lands as a regenerated `pipeline_lineage:` section in the target model's YAML (stale/orphan-marked, never silently dropped), pass-through suggested; optional global layers organize datasets

**Independent Test**: quickstart.md steps 5, 7 + persistence — lineage section appears after a run with hand-authored YAML untouched; removing an output field flags the run and marks the entry stale

- [X] T022 [P] [US3] Add lineage-section support to `app/semantic.py`: tolerant parse of a model's `pipeline_lineage:` block into the `Model` (exposed via `GET /api/models` output; ignored by the engine), and `replace_lineage_yaml(text, section)` text surgery — banner comment + section regenerated idempotently, appended when absent, byte-identical preservation of everything outside it (pattern: `append_measure_yaml`/`replace_measure_yaml`)
- [X] T023 [P] [US3] Add lineage/matching helpers to `app/pipelines.py`: `validate_lineage(declarations, output_schema)` → issues list (`declared_missing`/`undeclared_field` per data-model.md), `match_target_model(pipeline, models)` (delta: exact path; parquet: model source glob `fnmatch`es target key), and section-payload builder (`layer:source.field` refs, `stale` marks, `orphaned` flag)
- [X] T024 [US3] Post-run lineage sync in `app/pipeline_jobs.py`: after a successful run, validate declarations against the runner-reported `output_schema`, persist it on the run row (`finish_run`'s `output_schema` param, T004) and record `lineage_ok`/`lineage_issues` (non-blocking, FR-018), then regenerate the matched model's section via `replace_lineage_yaml` + hot-reload (depends on T022, T023). Orphan-marking is a *write* triggered by the DELETE request itself, not a reload side effect: in `DELETE /pipelines/{name}` (T011), after removing the pipeline file, look up its matched model (if any) and call `replace_lineage_yaml` with `orphaned=true` before returning — never defer this to `reload_all()`.
- [X] T025 [US3] Layers + suggestion endpoints in `app/api/pipelines.py`: `GET /lineage/layers` (any), `PUT /lineage/layers` (admin, write `pipelines/layers.yaml` + hot-reload, 409 when removal orphans a referenced layer, audit `layers.update`), `GET /pipelines/{name}/lineage/suggest` (any; falls back through: current target schema (if the target dataset already exists) → the pipeline's last **successful** run's persisted `output_schema` (T004/T024) → 409 if neither is available; name-match against declared source schemas)
- [X] T026 [US3] US3 tests in `tests/test_pipelines.py` + `tests/test_pipeline_api.py`: lineage validation outcomes, YAML surgery (append/replace/idempotence/byte-preservation outside the section — assert exact prefix+suffix), stale + orphan marking, target→model matching (delta exact, parquet glob), suggest endpoint incl. 409, layers CRUD + 409 + role matrix rows for the new routes
- [X] T027 [US3] Frontend lineage/layer affordances in `app/static/js/pipelines.js`: lineage entries editable in the pipeline YAML editor with a SUGGEST action inserting accepted pass-through entries (never auto-persisted, FR-017), run panel shows lineage warnings naming fields; layer badges on pipeline list; layers editable in the Modelling UI (admin); browser-verify quickstart steps 5 and 7

**Checkpoint**: Transformations are documented where the model lives, and the docs cannot silently drift

---

## Phase 6: User Story 4 - Lineage graph visualization (Priority: P4)

**Goal**: A read-only layered DAG of datasets/models (columns by layer), pipeline edges with latest run status, field-level upstream tracing across hops

**Independent Test**: quickstart.md step 8 — two chained pipelines render both hops with layer columns; expanding a gold field highlights its bronze origin; a failed run shows on the edge

- [X] T028 [US4] Implement `GET /api/lineage/graph` in `app/api/pipelines.py` (any role): assemble `{nodes, edges, field_lineage, layers}` per data-model.md from `registry.pipelines`, `registry.models`, `registry.layers`, and latest runs — one node per distinct source/target path with `model` attached when a loaded model scans it, one edge per (source, target) pair with latest-run status, flattened per-hop field links
- [X] T029 [P] [US4] Graph payload tests in `tests/test_pipeline_api.py`: chained pipelines (A→B→C) produce correct nodes/edges/field hops, layer grouping present/absent, latest-run status on edges, cycles (A→B, B→A) return a well-formed payload, disconnected datasets included
- [X] T030 [US4] Implement `app/static/js/lineagegraph.js`: hand-rolled SVG layered DAG — columns by layer order (topological rank fallback), rank-tie-breaking so cycles render (FR-023), edges colored by run status, node click → detail (model link, fields), field click → upstream highlight walked across hops client-side, all selection state ephemeral (Constitution V); theme-aware via existing CSS custom properties
- [X] T031 [US4] Wire the graph view: `app/static/js/router.js` (`/modelling/lineage`), entry point in `app/static/js/modelling.js`, module load in `app/static/js/main.js`; browser-verify quickstart step 8 including the failed-run edge state and zero console errors

**Checkpoint**: All four user stories independently functional

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T032 Ship the demo content (research R9): `pipelines/layers.yaml` (bronze/silver/gold) + a two-pipeline chain over seeded sales data (`pipelines/silver_orders.yaml` upsert w/ soft_delete + lineage, `pipelines/gold_daily_revenue.yaml` replace + lineage) and a demo model over the gold target so the seeded deployment demonstrates US1–US4 out of the box
- [X] T033 [P] Update `README.md`: pipeline module section (file format, script contract, materialization modes + delete policies, lineage section, layers, graph, admin gating + audit, route table additions), project-layout tree, and the API table — per Constitution Development Workflow
- [X] T034 [P] Record the Principle VI amendment in `.specify/memory/constitution.md`: pipeline scripts as a new admin-gated eval-capable construct (pattern of the spec-008/011 amendments), bump version + amendment date
- [X] T035 Full-suite pass `python -m pytest tests/` + fresh-clone boot check (empty `pipelines/` dir, no layers file — everything dormant per FR-020)
- [X] T036 Full quickstart.md validation in the browser: golden path end to end, all failure/safety checks, persistence round-trips, role checks as author/viewer accounts, zero console errors — report per Constitution IV (what changed, what was verified, rough edges)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)** → nothing
- **Foundational (Phase 2)** → Setup; **blocks all stories**
- **US1 (Phase 3)** → Foundational
- **US2 (Phase 4)** → US1 (extends `materialize.py` and the run path)
- **US3 (Phase 5)** → US1 (needs runs reporting `output_schema`); independent of US2
- **US4 (Phase 6)** → US1 (needs pipelines+runs); richer with US3's layers/lineage but functional without (nodes/edges/status only)
- **Polish (Phase 7)** → all stories (T032's demo chain uses US2+US3 features)

### Key task-level dependencies

- T009 ← T007, T008 · T010 ← T009 · T011 ← T005, T009 · T015/T016 ← T011
- T018/T019 ← T007 · T020 ← T018, T019
- T024 ← T022, T023, T009 · T026 ← T024, T025
- T028 ← T005 (registry) + T004 (runs) · T030/T031 ← T028

### Parallel Opportunities

- Phase 2: T004, T006 alongside T003 (T006 finalizes after T003's API settles)
- US1: T007 ∥ T008 (different files); T013 ∥ T014 after T011
- US3: T022 ∥ T023 (semantic.py vs pipelines.py)
- Polish: T033 ∥ T034
- After Foundational, US2/US3/US4 can proceed in parallel once US1's T009+T011 land

## Parallel Example: User Story 1

```bash
# After Phase 2:
Task: "T007 replace materialization in app/materialize.py"
Task: "T008 subprocess runner in app/pipeline_runner.py"
# After T011:
Task: "T013 run-lifecycle tests in tests/test_pipelines.py"
Task: "T014 API tests in tests/test_pipeline_api.py"
```

## Implementation Strategy

**MVP first**: Phases 1–3 (T001–T017) deliver the complete US1 story — hosted, audited, replace-mode pipelines with run history and UI. Stop, validate via quickstart steps 1–5, demo.

**Incremental delivery**: add US2 (upsert — the headline data capability), then US3 (the traceability differentiator), then US4 (the visual payoff). Each checkpoint leaves the app shippable. Polish (T032–T036) closes with demo content, docs, the constitution amendment, and the full browser pass.
