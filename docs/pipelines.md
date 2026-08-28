# Pipelines

**Source:** `app/pipelines.py` (534 lines) · `app/pipeline_runner.py`
(119 lines) · `app/pipeline_jobs.py` (194 lines) · `app/pipelinestore.py`
(183 lines) · `app/materialize.py` (243 lines)

A pipeline hosts a **SQL transformation** — not a low-code builder, an
actual script the platform runs, materializes, and documents lineage for.
It's the mechanism for turning raw landed files into curated, queryable
models (bronze → silver → gold, if a deployment wants those labels).

## Shape

```yaml
# pipelines/silver_orders.yaml
name: silver_orders
sources:
  - name: sales
    format: parquet
    path: s3://cash-intel/sales/*.parquet
    layer: bronze                  # optional — see Layers below
target:
  path: s3://cash-intel/silver/orders
  format: delta                    # delta (default, required for upsert) | parquet (replace only)
  layer: silver
materialization:
  mode: upsert                     # replace | upsert
  keys: [order_id]
  on_delete: soft_delete           # ignore (default) | sync | soft_delete | predicate
  soft_delete_column: is_deleted
timeout_seconds: 120                # default 600, max 3600
sql: |                              # each declared source is a view of its own name
  SELECT order_id, order_date, region, channel, category,
         (unit_price - unit_cost) * quantity AS net_revenue
  FROM sales
lineage:                            # optional — documents transformation logic on the target model
  - field: order_id
    from: [sales.order_id]
    transform: pass-through
```

Each declared source is registered as a view of its own name, so the SQL
just reads it by name (`FROM sales`) — never a bucket path directly. The
script's whole contract: **end on a `SELECT`, or create a relation called
`output`** (`app/pipelines.py`'s `validate_sql`/`returns_rows`, checked at
save time by parsing the SQL — not executing it). The platform performs
every *write*, which is what makes the materialization modes below
enforceable and a failed run non-corrupting: a pipeline's SQL never issues
its own `COPY … TO` against its declared target.

## Trust model

A pipeline's `sql:` keeps every table function a measure may never name
(`read_parquet`, `delta_scan`, `COPY … TO`, `ATTACH`) — that's how it reads
and writes arbitrary bucket paths. That *reach*, not code execution (SQL
offers none), is what the **admin** gate measures — see
[Auth & Security → The trust boundary](auth-and-security.md#the-trust-boundary-principle-vi).
Creating, editing, deleting, and **running** a pipeline all require the
admin role; every mutation and every run lands in the audit log
(`app/authstore.py`). Every role can read pipeline definitions, run
history, and the lineage graph — reads are never gated.

Iceberg is **read-only** as a source or target for now: writing a new
Iceberg table needs a catalog to allocate a location/schema/snapshot
atomically, which only `app/seed.py`'s demo data uses (a throwaway
in-memory `pyiceberg` `SqlCatalog`, discarded once written). Reading one
needs no catalog at all — see `app/iceberg_util.py`, which resolves the
current snapshot by listing the table's `metadata/` directory for the
highest-versioned `*.metadata.json` file, the same self-describing-directory
convention Delta's `_delta_log` already relies on.

## Execution: subprocess, killable, serialized per target

**Manual trigger only** — no scheduler. `POST /api/pipelines/{name}/run`
(admin) creates a `pipeline_runs` row (`PipelineStore.create_run`, status
`queued`) and hands its id to `pipeline_jobs.enqueue()`.

```
pipeline_jobs._drain()  (a daemon thread, started from app/main.py's lifespan
    │                    on any node whose CI_ROLE runs pipelines)
    │  claims the oldest queued run with one atomic UPDATE
    │  (PipelineStore.claim_next_run) and takes the lock on its target
    ▼
pipeline_jobs._execute(run_id, pipeline, registry)
    │  subprocess.Popen(["python", "-m", "app.pipeline_runner"], stdin=PIPE, ...)
    │  writes the job spec (sources/target/materialization/sql + write
    │  credentials for the target's own bucket) as JSON to stdin
    │  proc.communicate(timeout=pipeline.timeout_seconds)
    ▼
pipeline_runner.main()  (a fresh process, never imported into the app)
    │  registers each source as a TEMP VIEW via duck.relation() — the SAME
    │  seam every query reads sources through
    │  runs every statement in the sql in order (parse_statements)
    │  materializes whichever relation the contract names — a table/view
    │  literally called `output`, else the last statement's own rows
    ▼
    prints exactly ONE JSON result line to stdout (everything else the
    run prints goes to stderr, so it can never corrupt that one line)
```

**Why a subprocess and not a thread**: a thread can't be forcibly killed,
and neither can a runaway cross join or an infinite loop from inside one —
so the parent enforces `timeout_seconds` by killing the OS process outright
on `subprocess.TimeoutExpired`, which a hung query genuinely can't survive.
`pipeline_runner.py` is never `import`ed by the main FastAPI process for
exactly this reason: a crash in it can only ever take down its own
subprocess.

**No two runs ever write the same target.** This used to read "one run at a
time, platform-wide", guaranteed by `_drain()` being a single consumer on a
single in-process queue — no locking required. That reasoning is a property of
the *deployment*, not the code: it holds for one process and evaporates at two,
where two threads on two queues will happily materialize into one bucket path
at once, and nothing errors. So the guarantee moved into the store
(see [Scaling §3](scaling.md#3-pipeline-runs-the-one-dangerous-assumption)):

- **The queue is the `pipeline_runs` table.** `claim_next_run` claims the
  oldest queued run with one atomic `UPDATE … WHERE status = 'queued'`, so
  exactly one worker gets it however many ask. The in-process `queue.Queue`
  survives as a doorbell that wakes a worker in the same process immediately;
  a run triggered on another replica is found by the poll.
- **The lock is named for the target**, not the platform
  (`pipeline_target:<path>`), which is the invariant that actually matters.
  Two pipelines with *different* targets now run concurrently when there is
  more than one worker; a single worker still runs them one at a time, exactly
  as before.
- Triggering the *same* pipeline again while it has a pending run is still
  refused with `409`.

An app restart mid-run still marks that run `interrupted` rather than leaving
it stuck `running` forever — but scoped to the restarting node's own claims
(`PipelineStore.sweep_interrupted(node_id)`), because the old blanket sweep
would have had a restarting replica declare a *peer's* live run dead and drain
the queue on its way past. A worker that dies rather than restarts stops
renewing its lease and is reaped by `sweep_expired()`.

**Post-run bookkeeping** (`_execute`, on success): `cache.clear()` +
`duck.invalidate()` — the run wrote from a subprocess the main process
can't see into, so anything cached about the written path (a pinned small
table, DuckDB's external file cache) is now stale and would otherwise keep
serving pre-run rows until the TTL lapsed. `duck.invalidate()` also bumps the
cluster's data generation, so every *other* replica does the same pair within
a poll interval — without that, they would each go on serving pre-run rows,
which is the same staleness bug one process solved locally. Then `_sync_lineage()` validates
declared lineage against the run's real output schema and, if a loaded
model scans this pipeline's target, regenerates that model's
`pipeline_lineage:` YAML section (see [Traceability](#traceability-layers-and-lineage)
below).

## Materialization (`app/materialize.py`)

The platform's own write step — a pipeline never writes to the bucket
itself, it only produces an `output` relation. Every guard below runs
**before** any write, so a rejected run leaves the target exactly as it
was (never a partial write).

- **`replace`** — the target is atomically overwritten: one Delta
  transaction (`write_deltalake(..., mode="overwrite")`), or one `PUT` for
  a parquet target. Readers see the old data or the new data, never
  neither.
- **`upsert`** (Delta targets only) — the run's output is merged into the
  target by `keys`: matched rows update, unmatched rows insert
  (`DeltaTable.merge(...).when_matched_update_all().when_not_matched_insert_all()`).
  Guards before the merge: no null or duplicate key values in the output
  (`_guard_keys`), and an output schema compatible with the existing
  target, excluding the platform-managed soft-delete column
  (`_guard_schema` — reports missing/extra/mismatched columns by name). The
  soft-delete flag column is only ever added automatically on a target's
  **first** upsert run; retrofitting `soft_delete` onto a target created
  another way needs one `replace` run first, because `deltalake`'s
  merge-time schema evolution mis-populates a brand-new column on
  `when_not_matched_by_source_update` (verified: it leaves those rows null
  rather than `true`).

| `on_delete` policy | Behavior |
|---|---|
| `ignore` (default) | Rows the output no longer contains are left alone. |
| `sync` | Deleted (`when_not_matched_by_source_delete`). An **empty** output + `sync` halts the run unless `allow_empty_sync: true` is also set — it would otherwise delete the entire target. |
| `soft_delete` | Flagged `true` in `soft_delete_column`; cleared back to `false` automatically if the key reappears in a later run. |
| `predicate` | Rows matching `delete_predicate` (a Delta SQL predicate) are deleted before the merge. |

## Traceability: layers and lineage

**Layers** (`▤ LAYERS` in Modelling, `pipelines/layers.yaml` or the
DB-stored equivalent once anyone has `PUT`) are a purely organizational,
optional, ordered name list (bronze/silver/gold, or anything a deployment
prefers) — a pipeline tags its own sources/target with `layer:`, and
everything works with none declared at all.

**Field-level lineage** — which source field(s) a target field derives
from, plus a human-readable transform description — is declared per
pipeline, optional per field. `validate_lineage()` compares declared fields
against a run's *real* output schema on every successful run: a declared
field the output no longer has (`declared_missing`) or an output field
nobody declared (`undeclared_field`) is flagged on the run record — this
never blocks the write, it's purely informational.

When a loaded model's fact table scans this pipeline's target
(`match_target_model()` — delta targets match by exact path, parquet
targets by glob match), the validated lineage is regenerated into a
dedicated section of that model's own YAML:

```yaml
# ── managed by pipeline 'gold_daily_revenue' — do not hand-edit this section ──
pipeline_lineage:
  pipeline: gold_daily_revenue
  updated: '2026-07-17T21:34:04+00:00'
  fields:
    - field: total_net_revenue
      sources: [silver:silver.net_revenue]
      transform: "sum(net_revenue) per day, region"
```

This section is **entirely pipeline-owned** — regenerated idempotently
after every run (`semantic.replace_lineage_yaml`, the same
comment-preserving text-surgery family as the measure-lab's YAML edits —
see [Semantic Layer](semantic-layer.md#provenance-locking-and-text-preserving-edits)),
appended if absent, replaced in place if present, with every other byte of
the model's file untouched. Deleting the owning pipeline marks the section
`orphaned: true` rather than deleting it (`app/api/pipelines.py`'s
`delete_pipeline`).

**The lineage graph** (`GET /api/lineage/graph`) is a read-only, hand-rolled
SVG layered DAG rendered client-side (see
[Frontend](frontend.md)) — datasets/models as nodes, pipelines as directed
edges colored by their latest run status. It's assembled purely from
already-loaded pipelines/models/layers/run-status, so it needs no live
bucket scan and tolerates cycles by construction: each edge comes from one
pipeline's own declared source/target, never a recursive graph walk.

## Run history (`app/pipelinestore.py`)

`PipelineStore` is the append-only `pipeline_runs` table: id, pipeline
name, triggering user, status (`queued`→`running`→`succeeded`/`failed`/
`timed_out`/`interrupted`), timestamps, and (on success) `rows_written`/
`rows_deleted`/`rows_flagged`, the output schema, and the lineage
validation result. `latest_for(name)` backs the "latest run" badge in the
pipeline list; `pending_for(name)` is what the 409-on-duplicate-trigger
check reads.

## API surface

See [API Layer](api-layer.md#pipelines) for the full route table. In brief:
`GET /api/pipelines` (list + latest run), `POST /api/pipelines/validate`
(parse-check only — **never** executes the SQL), `GET`/`PUT`
`/api/pipelines/{name}/yaml` (name is immutable — a `PUT` that changes
`name:` is a 400), `POST /api/pipelines/{name}/run` (202, 409 if already
pending), `GET /api/pipelines/{name}/runs` / `GET /api/runs/{id}`,
`GET`/`PUT /api/lineage/layers`, `GET /api/pipelines/{name}/lineage/suggest`
(pass-through suggestions by name-matching the output schema against
declared sources — never auto-saved), `GET /api/lineage/graph`.
