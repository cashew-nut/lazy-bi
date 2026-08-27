# Implementation Plan: DuckDB SQL Engine

## Sequencing

The migration is bottom-up: runtime → grammar → semantic layer → engine →
everything that calls the engine. Each phase leaves the tree importable, and
the test suite is ported alongside the phase that breaks it rather than at
the end.

| # | Phase | Modules |
|---|---|---|
| 0 | Runtime | `app/duck.py` (new), `app/config.py`, `requirements.txt`, `Dockerfile` |
| 1 | Grammar | `app/sqlgrammar.py` (new, replaces `app/measure_dsl.py`) |
| 2 | Semantic layer | `app/semantic.py` |
| 3 | Engine | `app/engine.py` |
| 4 | Catalog | `models/*.yaml`, `dimensions/*.yaml` |
| 5 | Write paths | `app/seed.py`, `app/materialize.py`, `app/iceberg_util.py` |
| 6 | Pipelines & sandbox | `app/pipelines.py`, `app/pipeline_runner.py`, `app/sandbox.py`, `app/sandbox_runner.py`, `app/sandbox_agent.py` |
| 7 | Instant mode | `app/extract.py` |
| 8 | API & LLM | `app/api/*.py`, `app/llm.py`, `app/nlq.py`, `app/composer.py`, `app/skills_analytics.py` |
| 9 | Frontend | `app/static/js/*` |
| 10 | Docs | `README.md`, `.specify/memory/constitution.md` |

## Phase 0 — `app/duck.py`

The single place a DuckDB connection is created, configured and handed out.

**Connection.** One per process, created lazily, guarded by a lock. DuckDB
connections are not thread-safe for concurrent use, so the module hands out
short-lived **cursors** (`con.cursor()`) from that one connection — cursors
share the instance's caches (which is the entire point) while isolating
per-query state.

**Extensions.** `httpfs`, `avro`, `iceberg`, `delta`, loaded by absolute path
from the installed `duckdb_extension_*` wheels. Never `INSTALL` — no network
at runtime. A missing wheel degrades honestly: parquet/csv still work, and a
model naming a Delta or Iceberg source reports which package is missing.

**S3 secret.** `CREATE OR REPLACE SECRET` from `config.resolve_credentials()`,
refreshed when the resolved credential changes so an SSO/`AWS_PROFILE`
credential keeps working. `URL_STYLE 'path'` and `USE_SSL false` for the
emulator, derived from `CI_S3_ENDPOINT` rather than hardcoded.

**Latency settings.** Set once at connection open:

| Setting | Why |
|---|---|
| `enable_object_cache = true` | parquet footers cached process-wide |
| `enable_external_file_cache = true` | file bytes cached, block-level LRU |
| `enable_http_metadata_cache = true` | HEAD responses cached |
| `http_keep_alive = true` | no TCP/TLS handshake per range read |
| `http_retries`, `http_timeout` | tuned for a real endpoint, not loopback |
| `threads`, `memory_limit` | from config, so a container can be sized |

**Object listings.** `list_objects(path)` — the existing glob-aware,
segment-by-segment matcher moved over unchanged, cached for
`CI_SOURCE_CACHE_TTL`, returning (urls, total bytes).

**Pinned sources.** `relation_for(source)` returns either a local table name
(pinned) or a table-function expression. Pinning uses the same two gates as
the polars frame cache: object-store byte total checked *before* reading,
resident size checked after (`SELECT estimated_size` via
`duckdb_memory()`/`pragma database_size`, or the table's own byte estimate).
Invalidation hooks stay where they are — `registry.reload_all()`, a successful
pipeline run, dataset upload/delete.

## Phase 1 — `app/sqlgrammar.py`

Public surface, mirroring what `measure_dsl.py` exposed so the call sites
change shape rather than structure:

| Function | Replaces |
|---|---|
| `validate_expression(text, schema, *, window, parameter_values)` | `compile_measure` |
| `validate_relation(text, *, allowed_tables)` | `validate_frame` |
| `is_window_expr(text)` | same |
| `referenced_names(text)` | same |
| `referenced_parameter_names(text)` / `lag_period_param_names(text)` | same |
| `rollup_plan(text)` | same, on the SQL AST |
| `SqlCompileError(kind=...)` | `MeasureCompileError` |

`validate_expression` returns the **rewritten SQL text** (with `param()`
resolved to literals and, for window measures, `OVER w` intact) plus the set
of columns and sibling measures it referenced. The engine embeds that text; it
never embeds the author's raw string.

## Phase 2 — `app/semantic.py`

- `Measure.expr_source` stays; `frame_source`/`frame_emits` become
  `from_source`/`emits`.
- `compile_expr`, `validate_frame`, `compile_frame`, `_EVAL_GLOBALS`,
  `_FRAME_BUILTINS` deleted.
- `Measure.sql(schema)` returns validated SQL text instead of a `pl.Expr`.
- `Source` gains nothing — the format→relation mapping lives in `duck.py`.
- The old-DSL detector runs at parse time so a stale YAML fails on load.
- Comment-preserving YAML writers (`append_measure_yaml` etc.) learn the
  `from:`/`emits:` keys.

## Phase 3 — `app/engine.py`

Rewritten as a SQL builder per `contracts/engine-sql.md`. The module keeps its
public surface — `run_query`, `run_query_frame`, `scan_schema`,
`dimension_values`, `QueryError`, the relative-date vocabulary — so `api/`,
`extract.py`, `nlq.py` and the skills keep calling what they call.

Internal shape:

- `_relation(source)` → SQL text for one source (delegates to `duck`).
- `_fact_cte(model, part, dims)` → the joined scan.
- `_where(model, filters, schema)` → predicate SQL + bound parameters.
- `_group_select(...)` → the aggregate select list.
- `_window_select(...)` → the outer select with the `WINDOW` clause.
- `_from_measure_cte(...)` → one CTE per `from:` measure + its join.
- `_spine_cte(...)`, `_interval_cte(...)` → point-in-time.
- `_merge_parts(...)` → the multi-fact `FULL OUTER JOIN`.
- `build_sql(model, query)` → `(sql, params)`, exposed for tests and for the
  "show me the SQL" affordance.

Results come back as an Arrow table (`.arrow()`), converted to JSON-safe rows
for `run_query` and passed straight through for `extract.py`.

## Phase 4 — Catalog

Every measure in `models/*.yaml` rewritten per the migration table.
`median_tenure_days` becomes `expr:` + `from:`. Comments explaining the old
DSL are rewritten to explain SQL. `taxi.yaml`'s `VendorID` confirms the
identifier-quoting path.

## Phase 5 — Write paths

- `seed.py` keeps its deterministic Python row generation (so the demo data,
  and every test asserting a number from it, is byte-identical) and hands rows
  to DuckDB through pyarrow for the parquet/csv writes. Delta and Iceberg
  writes keep `deltalake`/`pyiceberg`.
- `materialize.py`'s guards and merge logic move to SQL over the run's output
  relation; the Delta write itself stays `deltalake`.
- `iceberg_util.py` keeps `resolve_metadata_path` (now feeding
  `iceberg_scan`) and loses its polars scan.

## Phase 6 — Pipelines & sandbox

- `pipelines.py`: `script:` → `sql:`, validated as a multi-statement SQL
  script producing `output`.
- `pipeline_runner.py` / `sandbox_runner.py`: exec SQL against a DuckDB
  session with the sources registered as views. Subprocess isolation and the
  killable timeout stay.
- `sandbox.py`: cell combining, `read()`-detection → source detection from
  table-function calls, convert-to-pipeline rewriting.
- `sandbox_agent.py`: the prompt becomes a DuckDB SQL brief.

## Phase 7 — Instant mode

`rollup_plan` re-implemented on the SQL AST; component measures are still
requested through the ordinary inline-measure path. Coarser-grain columns come
from `date_trunc`. Arrow IPC is written from the DuckDB result via pyarrow.

## Phase 8 — API & LLM

Route shapes are unchanged. What changes is validation error text, the measure
save payload (`from`/`emits`), and every prompt/tool-schema that described the
polars DSL.

## Phase 9 — Frontend

- `completion.js`: SQL function names from a shipped list, columns, sibling
  measures, `param('`.
- `measurelab.js`: the `from:` block gets an editor; SAVE TO VISUAL now
  accepts a framed measure.
- `pyhighlight.js` → `sqlhighlight.js`.
- `sandbox.js`: SQL cells.

## Phase 10 — Docs

README sections rewritten; constitution amended:

- **Principle II** — "Lazy Evaluation, Pushdown by Default" becomes
  "Pushdown by Default", stated in terms of a query planner rather than a
  polars `LazyFrame`. The requirement is unchanged: no full-table
  materialization without a documented reason and a benchmark.
- **Principle VI** — the `frame:` and pipeline-`script:` amendments are
  replaced by the two-way boundary in `spec.md`'s "Trust model". The
  eval-capable construct is gone; the remaining gate is I/O reach.
- **Technology Constraints** — Polars → DuckDB; the extension-wheel packaging
  rule joins the existing "vendored, not CDN" posture.

## Risks

| Risk | Mitigation |
|---|---|
| Float aggregates differ in the last bits from polars | Already true run-to-run today (README documents it); tests compare with a tolerance, and the README note is rewritten for DuckDB |
| `GROUP BY ALL` semantics differ from an explicit list in an edge case | Emitted SQL is asserted in tests, not just its results |
| DuckDB's parser accepts old-DSL text like `sum(x)` as valid SQL | The `legacy_dsl` detector runs *before* the allowlist, keyed on constructs that are not SQL functions (`where`, `if_`, `col`, `count_distinct`, `running_total`, two-arg `cast`) |
| Extension wheels lag a DuckDB release | Both pinned to the same version in `requirements.txt`; a mismatch fails loudly at startup |
| A `from:` block is expensive | Same row cap and statement bounds as any other query; noted in the validator contract as explicitly out of its scope |
