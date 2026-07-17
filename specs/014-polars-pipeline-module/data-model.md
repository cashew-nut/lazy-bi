# Data Model: Polars Pipeline Module

## Entity overview

```
Layer (pipelines/layers.yaml, optional, one global ordered list)
  ▲ referenced by name
Pipeline (pipelines/<name>.yaml) ──── sources (≥1, declared) ──► bucket datasets
  │  └── target (exactly 1; ≤1 owning pipeline per target path)
  │  └── materialization config
  │  └── lineage declarations (per target field, optional)
  ├──► PipelineRun (sqlite pipeline_runs, append-only)
  ├──► audit_events (existing table; every mutation + trigger)
  └──► Model.pipeline_lineage (regenerated section in target model YAML)
```

## Pipeline (file: `pipelines/<name>.yaml`)

| Field | Type | Rules |
|---|---|---|
| `name` | str | required; must match filename stem; unique; `[a-z0-9_]+` like models |
| `label` | str | optional; auto-titled from name when absent |
| `description` | str | optional |
| `sources` | list | ≥1 entry: `{name, format: parquet\|csv\|delta, path, layer?}`; `name` unique within the pipeline; `path` any scan-able bucket path/glob (delta: table root) |
| `target` | map | required: `{path, format: delta\|parquet, layer?}`; delta is the default format; **at most one pipeline may own a given target path** (validated across the loaded set at reload) |
| `materialization` | map | see below |
| `timeout_seconds` | int | optional; default `config.PIPELINE_TIMEOUT_DEFAULT` (600); 1–3600 |
| `script` | str | required; literal block; syntax-checked at load with `compile(..., "exec")` (same as `validate_frame`); contract: reads `sources[...]`, assigns `output` |
| `lineage` | list | optional; entries `{field, from: [source_name.field, ...], transform}`; `field` unique in list; every `from` source name must be a declared source |

### Materialization config

| Field | Type | Rules |
|---|---|---|
| `mode` | `replace` \| `upsert` | required |
| `keys` | list[str] | required iff mode=`upsert`; ≥1 |
| `on_delete` | `ignore` \| `sync` \| `soft_delete` \| `predicate` | upsert only; default `ignore` |
| `soft_delete_column` | str | required iff on_delete=`soft_delete`; boolean flag column, platform-managed |
| `delete_predicate` | str | required iff on_delete=`predicate`; delta SQL predicate string passed to `DeltaTable.delete()` |
| `allow_empty_sync` | bool | default false; only meaningful for `sync` (FR-011) |

Cross-rules (load-time validation errors):
- `upsert` requires `target.format == delta` (FR-009).
- `parquet` target requires `mode == replace`.
- csv targets rejected.
- `layer` values (sources and target) must exist in `layers.yaml` when used;
  using layers with no `layers.yaml` present is an error naming the fix.

## Layers (file: `pipelines/layers.yaml`, optional)

```yaml
layers:            # ordered — graph columns render in this order
  - name: bronze
    label: Bronze  # optional
  - name: silver
  - name: gold
```

Rules: names unique, `[a-z0-9_]+`; order is meaningful; file absent ⇒ layer
features simply dormant (FR-020).

## PipelineRun (sqlite table `pipeline_runs`, append-only)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | run id |
| `pipeline` | TEXT | pipeline name (survives pipeline deletion — history retained) |
| `status` | TEXT | see state machine |
| `triggered_by` | INTEGER | user id (audit attribution) |
| `triggered_label` | TEXT | display name snapshot |
| `queued_at` / `started_at` / `finished_at` | TEXT ISO | started/finished nullable |
| `rows_written` | INTEGER | inserts+updates (upsert) or total rows (replace) |
| `rows_deleted` | INTEGER | sync/predicate deletions |
| `rows_flagged` | INTEGER | soft-delete flags set this run |
| `lineage_ok` | INTEGER | 1/0/NULL (NULL = no declarations to validate) |
| `lineage_issues` | TEXT JSON | list of `{kind: declared_missing\|undeclared_field, field}` |
| `output_schema` | TEXT JSON | nullable; `[{name, dtype}]` reported by the runner on a successful run — persisted so the lineage-suggest endpoint (R5/FR-017) can fall back to "last successful run's schema" when the target doesn't exist yet |
| `error` | TEXT | failure/timeout/interrupt detail |

### Run status state machine

```
queued ──► running ──► succeeded
   │           ├─────► failed        (script error, guard failure, write error)
   │           ├─────► timed_out     (parent killed the subprocess)
   │           └─────► interrupted   (app restarted mid-run — startup sweep)
   └─────────────────► interrupted   (queued at restart — startup sweep)
```

Terminal states: `succeeded`, `failed`, `timed_out`, `interrupted`. No row is
ever left in `queued`/`running` after the startup sweep. Only the parent
worker writes this table.

## Lineage declaration (within pipeline YAML)

| Field | Type | Rules |
|---|---|---|
| `field` | str | target output column name |
| `from` | list[str] | `source_name.column` refs; source_name must be declared |
| `transform` | str | human-readable description; `"pass-through"` conventional for identity |

Validation per run (against runner-reported output schema, non-blocking,
FR-018): declared `field` absent from output ⇒ `declared_missing`; output
column with no declaration ⇒ `undeclared_field`. Result lands on the run row
and drives staleness marks in the model section.

## Model `pipeline_lineage:` section (regenerated in target model YAML)

Owned entirely by the platform; rewritten via `semantic.replace_lineage_yaml`
after each successful run of the owning pipeline whose target matches the
model's source (delta: exact path match; parquet: model glob `fnmatch`es the
target key). Hand-authored YAML outside the section is never touched; hand
edits inside are overwritten by design (spec edge case).

```yaml
# ── managed by pipeline 'silver_orders' — do not hand-edit this section ──
pipeline_lineage:
  pipeline: silver_orders
  updated: "2026-07-17T12:00:00Z"
  orphaned: true            # present only if the owning pipeline was deleted
  fields:
    - field: net_revenue
      sources: ["bronze:raw_orders.gross", "bronze:raw_orders.returns"]
      transform: "net revenue = gross − returns"
    - field: legacy_col
      sources: ["bronze:raw_orders.legacy_col"]
      transform: "pass-through"
      stale: true           # declared but absent from the latest output
```

`sources` entries render as `layer:source.field` when the source has a layer,
`source.field` otherwise. Parsed (tolerantly) by `semantic.py` so the model
API and Modelling UI can surface it; ignored by the query engine.

## Lineage graph payload (derived, not stored)

Assembled on request from loaded pipelines + models + latest runs:

- **nodes**: `{id (dataset path), label, layer?, model?, fields: [...]}` —
  one per distinct source/target path; `model` set when a loaded model scans
  that path.
- **edges**: `{pipeline, source_id, target_id, status (latest run or none),
  transform_summary}` — one per (source, target) pair of each pipeline.
  `transform_summary` is the pipeline's `description` field when set, else
  "N fields documented" (count of its `lineage` declarations), else "No
  transformation documented".
- **field_lineage**: `{node_id, field, upstream: [{node_id, field}],
  transform}` — flattened per-hop links; the client walks hops for
  multi-level tracing.

Cycles allowed in data; the client layout breaks rank ties (FR-023).

## Audit events (existing `audit_events` table)

New `action` values: `pipeline.create`, `pipeline.update`, `pipeline.delete`,
`pipeline.run`, `layers.update` — recorded with the verified acting account,
same call pattern as measure provenance (FR-004, SC-006).
