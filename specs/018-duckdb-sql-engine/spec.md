# Feature Specification: DuckDB SQL Engine

**Feature Branch**: `claude/duckdb-migration-polars-rfbngk`
**Status**: Draft
**Supersedes**: the polars query path (spec 001), the safe measure DSL
(spec 008 — grammar only; its *security posture* is kept and strengthened),
the `frame:` intermediary-frame carve-out, and the Python script contract of
specs 014 (pipelines) and the sandbox notebook feature.

## Summary

Replace polars with **DuckDB** as the single execution engine, and replace
every authored grammar in the product with **SQL**.

Three things change for a user:

1. **Measures are SQL.** `sum(unit_price * quantity)` becomes
   `SUM(unit_price * quantity)`. Everything the old DSL could express has a
   direct SQL spelling, and everything it *couldn't* — date arithmetic,
   `CASE`, `QUALIFY`, quantiles, `ARG_MAX` — now works.
2. **Complex metrics stop being a different language.** A measure that needs
   business logic between the scan and the reduce declares `from:` — a SQL
   `SELECT` producing the rows it aggregates over — and keeps the same
   `expr:` key, holding the same kind of SQL aggregate, as every other
   measure. The Python `frame:` construct, and with it the last `eval`/`exec`
   path reachable from authored content, is deleted.
3. **A visual costs one S3 round trip, not tens.** DuckDB's HTTP layer —
   parquet metadata cache, external file cache, keep-alive connection reuse,
   parallel range reads — plus one long-lived in-process connection and a
   cached object listing, replaces the bespoke frame cache the polars engine
   needed.

Pipelines and sandbox notebooks become SQL too, which removes unsandboxed
Python execution from the product entirely.

## Motivation

### The latency problem is structural, not incidental

Polars' laziness optimizes *bytes*; a real object store bills *round trips*.
`app/engine.py`'s current header says so plainly: left alone, each
`collect()` re-lists every glob, re-reads every parquet footer, and re-reads
every joined lookup file. The existing fix (spec: "Against a real object
store: round trips, not bytes") is a Python-side cache of listings and small
source frames, bolted on outside the engine because polars has nowhere to
put it. It works — and it is a reimplementation of three things DuckDB does
natively and better:

| Concern | Today (polars + app cache) | DuckDB |
|---|---|---|
| parquet footers re-read per query | not cached | object cache, process-wide |
| file bytes re-fetched per query | whole-frame cache under a byte cap | external file cache, block-level, LRU |
| TCP/TLS handshake per request | new connection each time | `http_keep_alive` connection reuse |
| range reads | sequential per column chunk | parallel prefetch across row groups |
| N unrelated fact tables in one model | N `collect()`s + a Python merge | one statement, one plan, one round of I/O |

The last row is the one the app cannot fix from the outside: a multi-fact
model runs one full query per fact table today and merges the results in
Python. In SQL it is one statement, planned and executed once.

### Two grammars for one idea is the actual product bug

A metric author today picks a language based on whether their metric happens
to fit inside one reduce. `sum(where(revenue, region == "EU"))` is the safe
DSL; "median tenure per subscription, bucketed by churn month" is a Python
snippet with `pl`, `lf` and `dims` in scope, restricted to admins because it
runs through `eval`. Same person, same intent, same YAML file, two languages
and two trust levels.

SQL collapses that. `SUM(revenue) FILTER (WHERE region = 'EU')` and
`MEDIAN(tenure_days)` over a declared `from:` are the same language, and the
second is no longer capable of running code.

## User Scenarios

### US-1 — An analyst writes an ordinary metric (P1)

An author opens the measure lab, types `SUM(unit_price * quantity)`, and the
chart updates on every keystroke. Autocomplete offers SQL aggregate
functions, the source's real columns, and sibling measures.

**Acceptance**
1. `SUM(unit_price * quantity)` compiles and returns the same numbers the
   polars engine returned for `sum(unit_price * quantity)`.
2. `SUM(x) FILTER (WHERE region = 'EU')`, `COUNT(DISTINCT order_id)`,
   `CASE WHEN`, `COALESCE`, `CAST`, and arithmetic between aggregates all
   compile.
3. A non-aggregate expression (`unit_price`) is refused at compile time with
   a message saying a measure must reduce to one value per group.

### US-2 — An analyst writes a complex metric in the same place (P1)

The same author needs median subscription tenure, bucketed by churn month.
They stay in the measure lab, write `MEDIAN(tenure_days)` in `expr:`, and add
a `from:` block computing `tenure_days` and `churn_month` per subscription.

**Acceptance**
1. `expr:` holds a SQL aggregate in both the simple and the complex case —
   the syntax of the metric itself does not change.
2. The `from:` block sees `{model}` (the fact scan with the query's filters
   applied) and `{dims}` (the query's grouping columns), and may use CTEs,
   joins between the model's own datasets, window functions and `GROUP BY`.
3. A dimension the `from:` block computes itself is declared in `emits:` and
   buckets the *derived* rows, not the raw ones — the `frame_emits` semantics,
   unchanged.
4. Framed and plain measures mix in one query; groups the derived rows do not
   cover come back null.
5. The construct requires the **author** role, not admin, and is available to
   inline (visual-scoped) measures — see "Trust model" below for why that is
   a narrowing, not a widening.

### US-3 — A dashboard stays interactive against real S3 (P1)

An operator points `CI_S3_ENDPOINT` at a bucket 40ms away and opens a
six-tile dashboard.

**Acceptance**
1. The first load pays for the data it has never read. Every interaction
   after it — cross-filter, grain change, re-run — issues **zero** new S3
   requests for source metadata already read, within the staleness contract.
2. A model with N unrelated fact tables issues one query, not N.
3. A query that reads no columns from an optional joined bundle does not
   read that bundle's files.
4. `s3:ListBucket` remains an optimization, not a requirement: a bucket that
   denies it still answers every query, via DuckDB's own glob.

### US-4 — An admin writes a pipeline (P2)

**Acceptance**
1. A pipeline declares `sql:` instead of `script:`; its contract is to
   produce a relation named `output`.
2. Sources are addressable by their declared names; the platform still owns
   every write, so `replace`/`upsert`/delete policies are unchanged.
3. Multi-statement SQL works (`CREATE TEMP TABLE`, CTEs, a final `SELECT`).

### US-5 — An admin explores in the sandbox (P2)

**Acceptance**
1. Notebook cells are SQL. A cell's result renders as a table.
2. Cells run top-to-bottom in one shared DuckDB session, so a `CREATE TEMP
   VIEW` in cell 1 is visible in cell 3.
3. Convert-to-pipeline still detects sources and rewrites them.

### US-6 — Existing content fails loudly, never silently (P1)

**Acceptance**
1. A model YAML carrying old-DSL text (`sum(x)`, `count_distinct(x)`,
   `where(v, p)`, `running_total(m)`, `frame:`) fails to load with a message
   naming the SQL equivalent.
2. A saved visual whose `inline_measures` hold old-DSL text reports the same
   error against that tile, and the rest of the dashboard still renders.

## Requirements

### The measure grammar

- **FR-001** A measure's `expr:` is a SQL expression that must be an
  aggregate over the fact table's rows (or a window function — see FR-004).
- **FR-002** Bare identifiers in `expr:` are source columns (post-join),
  exactly as before.
- **FR-003** `param('name')` remains a call in the grammar, resolved at
  compile time to a typed literal from the visual's declared values. It is
  legal wherever a literal is legal.
- **FR-004** A measure whose expression contains a window function is a
  *window measure*: bare identifiers are sibling **measure** names, and the
  engine supplies a named window `w` as `PARTITION BY <the query's other
  dimensions> ORDER BY <its time dimension>`. `SUM(revenue) OVER w` is a
  running total; `(revenue - LAG(revenue) OVER w) / LAG(revenue) OVER w` is
  period-over-period change. The requirement of exactly one time dimension is
  unchanged.
- **FR-005** A measure may declare `from:` — a SQL `SELECT` — plus optional
  `emits:`. `expr:` then aggregates that relation instead of the fact scan.
- **FR-006** `{model}` and `{dims}` are the only placeholders. `{dims}`
  always expands to at least one column, so `SELECT {dims}, x` and
  `GROUP BY {dims}, y` are safe even when the query groups by nothing.
- **FR-007** Old-DSL text is a load-time / compile-time error naming the SQL
  equivalent (FR-U6-1).

### The SQL validator

- **FR-010** Every authored SQL expression and `from:` block is parsed with
  DuckDB's own parser via `json_serialize_sql()` and validated against an
  allowlist **before** it reaches a connection. Nothing authored is ever
  string-concatenated into a statement without passing this.
- **FR-011** An `expr:` may contain only: constants, column references,
  operators, comparisons, conjunctions, `CASE`, `CAST`, `COLLATE`, function
  calls whose names are on the allowlist, `FILTER (WHERE …)`, aggregate
  `ORDER BY`, and (window measures only) `OVER w`. Subqueries, table
  references, stars, lambdas, prepared parameters and table functions are
  refused.
- **FR-012** A `from:` block may additionally contain CTEs, joins, `GROUP
  BY`/`HAVING`/`QUALIFY`, set operations, subqueries and window
  specifications — but its only base-table references may be `{model}` and
  CTEs it declares itself. **Table functions are refused outright**, which is
  what denies it filesystem, HTTP and catalog access.
- **FR-013** The function allowlist is built from DuckDB's own
  `duckdb_functions()` catalog minus an explicit deny set (filesystem,
  network, secrets, settings, catalog introspection, `nextval`, `error`, the
  `pg_*`/`duckdb_*` families, and anything not marked as a scalar/aggregate/
  window function), so a DuckDB upgrade adding a maths function does not
  require a code change, while a DuckDB upgrade adding an I/O function does
  not silently widen the grammar.
- **FR-014** Statements the engine builds are single statements; the
  validator refuses any authored fragment that parses to more than one.

### The engine

- **FR-020** One semantic query compiles to **one** DuckDB statement,
  including the multi-fact case, which merges parts with a null-safe `FULL
  OUTER JOIN` on the shared dimensions rather than N queries plus a Python
  merge.
- **FR-021** Projection and predicate pushdown are preserved: a query reads
  only the columns and row groups it needs. This is Principle II, restated
  for a SQL planner.
- **FR-022** Every existing semantic-layer behaviour is preserved exactly:
  joins, dimension bundles and their reachability rules, `how: left` imports
  applied only when read, `how: between` interval imports thinned to one row
  per bucket at the query's grain, spine dimensions, `match:` modes, geo
  coordinate columns, sort/limit defaults, the composite-model dimension
  intersection, and every error message's *meaning* (wording may change).
- **FR-023** Time grains map to `date_trunc`; the five grains
  (`1d 1w 1mo 1q 1y`) and the relative-date filter vocabulary are unchanged.

### S3 latency

- **FR-030** One DuckDB connection per process, created once, with
  extensions loaded and the S3 secret installed once — so its caches are
  process-lifetime, not per-query.
- **FR-031** Object listings are cached for `CI_SOURCE_CACHE_TTL` and the
  resolved file list is passed to `read_parquet([...])`, so a glob costs one
  `LIST` per TTL rather than one per query.
- **FR-032** Sources under `CI_SOURCE_CACHE_MAX_BYTES` in the object store
  are **pinned as local DuckDB tables**, so the lookup tables in `joins:` and
  the datasets behind a dimension bundle are joined from memory. Same two
  gates as today (byte total checked before reading; resident size checked
  after), same TTL, same immediate invalidation on model reload, pipeline run
  and dataset upload/delete.
- **FR-033** DuckDB's own caches are enabled explicitly: parquet metadata
  (`enable_object_cache`), file bytes (`enable_external_file_cache`), HTTP
  metadata, and connection reuse (`http_keep_alive`), with `http_retries` and
  `http_timeout` set for a real endpoint rather than a loopback emulator.
- **FR-034** Credentials are re-resolved per `CREATE SECRET` refresh so an
  `AWS_PROFILE`/SSO credential keeps refreshing, as today.
- **FR-035** DuckDB extensions ship as **pinned PyPI wheels** loaded from
  disk. No runtime download from `extensions.duckdb.org`, in the image or
  outside it.

### Pipelines and the sandbox

- **FR-040** A pipeline declares `sql:`; its contract is a relation named
  `output`. Sources are addressable by their declared names.
- **FR-041** Sandbox notebook cells are SQL, sharing one session per run.
- **FR-042** Subprocess isolation and the killable timeout are kept — they
  are a runaway-query containment measure, and a SQL cartesian product needs
  them as much as a Python `while True` did.
- **FR-043** Materialization behaviour (`replace`/`upsert`, the four delete
  policies, the pre-write guards) is unchanged.

### Instant mode

- **FR-050** Extracts keep working: rollup decomposition is re-implemented on
  the SQL AST (`SUM`/`COUNT`/`MIN`/`MAX` decompose; `AVG` decomposes to
  `SUM`/`COUNT`; anything else makes the tile live), and Arrow IPC is
  serialized from DuckDB.

### Removals

- **FR-060** `polars` is removed from `requirements.txt` and from every
  module. `deltalake` and `pyiceberg` remain **only** for the Delta and
  Iceberg *write* paths (seeding and pipeline materialization); all reads go
  through DuckDB.
- **FR-061** `app/measure_dsl.py`'s AST compiler, `semantic.compile_expr`,
  `semantic.validate_frame`, `semantic.compile_frame` and the `_EVAL_GLOBALS`
  namespace are deleted. No module in `app/` calls `eval`, `exec` or
  `compile` on authored content afterwards.

## Trust model

The three-way boundary of Principle VI becomes a two-way one, and the axis
changes from *"can this run code?"* to *"what data can this reach?"*:

| Construct | Today | After |
|---|---|---|
| scalar measure `expr:`, model or inline | allowlisted Python AST → `pl.Expr` | allowlisted SQL AST, no table refs. **Any role that can author.** |
| complex measure (`frame:` → `from:`) | `eval`/`exec` Python, **admin**, model-only | allowlisted SQL SELECT, no table functions, base tables limited to `{model}`. **Author**, and available inline. |
| pipeline / sandbox script | unsandboxed Python, **admin** | SQL with table functions and write access to the bucket. **Admin**, unchanged. |

The middle row is the substantive change and it is a **narrowing of
capability paired with a widening of access**, in that order: the construct
loses the ability to import a module, open a socket, read a file or reach the
process, and in exchange stops needing the highest trust level in the system.
What still requires admin is the ability to name arbitrary bucket paths and
write to them — an I/O capability, which is the honest thing for that gate to
be measuring.

## Out of scope

- A scheduler for pipelines (still manual-trigger only).
- Writing Iceberg (still read-only; the demo seeder's throwaway catalog is
  the only writer).
- Cross-model queries — a query still names exactly one model.
- Parameterizing dashboard/visual *filters* (still measure expressions only).
- Any change to the chart renderers, the theme system, auth, or the stores.

## Success criteria

- **SC-001** Every semantic behaviour in `tests/test_engine.py` holds, in
  SQL, including the `match:` truth table pinned for both point-in-time
  mechanisms and the spine/calendar grain-for-grain agreement.
- **SC-002** A six-tile dashboard re-run against a 40ms-latency endpoint
  issues zero S3 requests after the first load, measured, and the numbers in
  the README's latency table are replaced with measured ones.
- **SC-003** The multi-fact model answers in one statement — asserted, not
  assumed.
- **SC-004** `grep -r "eval(\|exec(" app/` returns nothing that touches
  authored content.
- **SC-005** `median_tenure_days` — today's only `frame:` measure — is
  expressed in `expr:` + `from:`, requires only the author role, and returns
  the same numbers.
- **SC-006** The 13M-row taxi benchmark is re-measured and reported.
