# Contract: Pipelines HTTP API + Runner Subprocess Protocol

All routes sit under `/api` behind the existing AuthMiddleware (session
cookie or bearer token; cookie mutations need `X-Requested-With: fetch`).
**Mutations + run trigger: admin. Reads: any authenticated role.**
`tests/test_role_matrix.py` must cover every row.

## Pipelines

| Route | Role | Behavior |
|---|---|---|
| `GET /api/pipelines` | any | List: name, label, layer(s), target, mode, latest-run summary `{id, status, finished_at}` |
| `POST /api/pipelines` | admin | Body `{yaml}`. Validate (parse + cross-rules + script syntax + unique target) → write `pipelines/<name>.yaml` → hot-reload → 201. 409 if name or target already owned. Audit `pipeline.create`. |
| `GET /api/pipelines/{name}/yaml` | any | `{name, file, yaml}` |
| `PUT /api/pipelines/{name}/yaml` | admin | Validate (name immutable — 400 on rename) → write → hot-reload. Audit `pipeline.update`. |
| `DELETE /api/pipelines/{name}` | admin | 409 while a run for it is queued/running. Removes file, hot-reloads; run history retained; target model section marked orphaned. Audit `pipeline.delete`. |
| `POST /api/pipelines/validate` | any | Body `{yaml}` → `{ok, error?, pipeline?}`. Parse-only, never executes. |
| `POST /api/pipelines/reload` | admin | Re-read `pipelines/` (mirror of models/reload). |
| `GET /api/pipelines/{name}/lineage/suggest` | any | Pass-through suggestions: output columns (target schema, else last successful run's schema, else 409) name-matched against declared source schemas → `{suggestions: [{field, from, transform: "pass-through"}]}`. Never persists (FR-017). |

## Runs

| Route | Role | Behavior |
|---|---|---|
| `POST /api/pipelines/{name}/run` | admin | Enqueue → 202 `{run_id, status: "queued"}`. 409 if a run for this pipeline is already queued/running (platform-wide execution is serialized regardless). Audit `pipeline.run`. |
| `GET /api/pipelines/{name}/runs` | any | Run history, newest first (limit param, default 50). |
| `GET /api/runs/{id}` | any | Full run record incl. `lineage_issues`, `error`; `running` rows report live status. |

Run record fields: see data-model.md `pipeline_runs`.

## Lineage & layers

| Route | Role | Behavior |
|---|---|---|
| `GET /api/lineage/graph` | any | `{nodes, edges, field_lineage, layers}` per data-model.md payload spec. |
| `GET /api/lineage/layers` | any | `{layers: [{name, label}]}` (empty list when file absent). |
| `PUT /api/lineage/layers` | admin | Replace ordered list → write `pipelines/layers.yaml` → hot-reload. 409 if removal orphans a referenced layer. Audit `layers.update`. |

## Error semantics

- 400 validation failure (message names the field/rule), 401 unauthenticated
  (middleware), 403 role, 404 unknown pipeline/run, 409 conflict (duplicate
  name/target, delete-while-running, run-already-pending, layer in use).
- Guard failures during a run (null/dup keys, schema diff, empty-sync halt)
  are NOT HTTP errors — the trigger already returned 202; they land as
  `status: failed` with a diagnostic `error` on the run record.

## Runner subprocess protocol (internal contract)

Parent (worker thread in `app/pipeline_jobs.py`) ⇄ child
(`python -m app.pipeline_runner`):

- **stdin** (one JSON doc): `{pipeline: <parsed definition>, storage:
  {s3 endpoint/creds/bucket}, timeout_seconds}` — child is config-complete,
  reads no app state.
- **stdout** (one JSON line on completion):
  `{ok, rows_written, rows_deleted, rows_flagged, output_schema:
  [{name, dtype}], error?}` — `output_schema` reported even on lineage-clean
  runs (parent does all lineage validation + model YAML writing).
- **exit codes**: 0 ok; 1 script/guard/write failure (details in stdout
  JSON); parent-initiated kill on timeout ⇒ recorded `timed_out`.
- Child performs the materialization write itself (transactional delta /
  atomic parquet PUT); parent performs every SQLite write and the model-YAML
  lineage regeneration. One child at a time, platform-wide.
- Parent startup sweep: any `queued`/`running` row → `interrupted`.
