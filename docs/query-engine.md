# Query Engine

**Source:** `app/engine.py` (1618 lines) · `app/duck.py` (562 lines) ·
`app/sqlgrammar.py` (845 lines) · `app/extract.py` (510 lines) ·
`app/cache.py` (54 lines)

This is the layer that turns a JSON semantic query into **one DuckDB SQL
statement**, runs it through the platform's single shared connection, and
gets the bytes back out of S3 as cheaply as possible. Five modules, one
job each:

| Module | Job |
|---|---|
| `engine.py` | Semantic query (dict) → SQL statement, bound params, column metadata. Runs it, returns JSON rows. |
| `sqlgrammar.py` | The allowlisting boundary every authored SQL fragment passes through before it can reach a connection. |
| `duck.py` | The one process-wide DuckDB connection: extensions, S3 credentials (as DuckDB `SECRET`s), and the two caches DuckDB can't build itself. |
| `cache.py` | A tiny generic TTL cache — the primitive `duck.py` and `engine.py` both build their specific caches on top of. |
| `extract.py` | Instant-mode: decomposes a query into an Arrow extract a dashboard tile can re-aggregate client-side, with no further round trips. |

## Why one statement

`engine.build_sql()` compiles the joins, the point-in-time timelines, any
`from:` intermediary relations, and the merge across several unrelated fact
tables all as clauses (mostly CTEs) of a **single** query the DuckDB planner
sees whole. Against a real object store that's the difference between one
round of I/O and several — see
[Architecture → Deployment topology](architecture.md) and the constitution's
Principle II ("Pushdown by Default, Round Trips Counted").

Nothing an author wrote is ever concatenated into that statement directly:
measure expressions and `from:` blocks arrive as text rendered from their
own validated AST (`sqlgrammar`), filter values are bound as prepared
parameters (`_coerce`, never string-interpolated), and every identifier the
engine emits is quoted through `_q()`. The only things this module
interpolates are source paths and column names it resolved itself.

## The compilation pipeline

```
engine.build_sql(model, query, row_cap)
        │
        ├─ model.is_composite? ──► _build_parts()   (several unrelated fact tables — see below)
        │
        └─ else ──────────────► _build_single()
                                    │
                                    ├─ scan(model, dims_in_play)         one joined relation:
                                    │                                    fact + joins + dimension-bundle imports
                                    ├─ scan_schema(model, dims_in_play)  cached column→type map
                                    ├─ filters → WHERE / spine bounds
                                    ├─ _split_measures()  → plain | window | framed
                                    ├─ __model CTE: filtered rows, dimension columns materialized
                                    │                under their semantic names — what a `from:` block
                                    │                reads as `{model}`
                                    ├─ __agg CTE: GROUP BY ALL over the plain measures
                                    ├─ _window_cte()   (if any window measures)
                                    ├─ _framed_ctes()  (if any `from:` measures — one CTE each,
                                    │                    FULL OUTER JOINed back on the query's dims)
                                    └─ final SELECT … ORDER BY … LIMIT
```

`run_query_arrow()` executes the compiled statement on `duck.cursor()` and
returns the raw Arrow table plus column metadata and elapsed time;
`run_query()` wraps that into the `{columns, rows, row_count, elapsed_ms}`
JSON shape `POST /api/query` returns, converting every value through
`_json_safe` (dates → ISO strings, `Decimal` → `float`, …). `build_sql()`
itself is public specifically so it's inspectable — tests assert its shape
directly.

### Building the scan (`scan()`)

`scan(model, dimensions)` is the model's base source plus its joins and
imported dimension bundles, as one projected subquery. `dimensions` maps
each dimension the caller is about to use to its requested grain (or `None`
for "the dimension's own", or the whole map is `None` for "unknown — apply
everything," which is what schema introspection uses). It decides, per
query:

- A `how: left` dimension-bundle import is only joined in when the query
  actually reads one of its dimensions — a model importing a calendar
  purely to [conform two fact tables](semantic-layer.md#several-fact-tables-in-one-model)
  doesn't pay for that join on a query that never touches a date.
  `how: inner` filters rows, so it's always applied.
- An **interval** import (`how: between`) is applied only when one of its
  dimensions is in play, and is first thinned to **one row per bucket at the
  query's grain** (`_period_rows`) before the interval join — so a model row
  is counted once per bucket at whatever grain the query asked for, and an
  additive measure stays correct across grain changes.

`_projection()` builds an explicit `SELECT` list (never `SELECT *`) so a
join whose two sides share a column name doesn't produce an ambiguous
duplicate — first occurrence keeps the name, a later one gets suffixed
`_right`, matching the pre-DuckDB engine's convention.

### Point-in-time: spines vs. interval imports

Both point-in-time mechanisms — a `spine:` dimension and a `how: between`
import — share one predicate, `match_predicate(start, end, lo, hi, match)`,
so `overlap`/`period_start`/`period_end` mean the same thing wherever
they're used (see [Semantic Layer](semantic-layer.md#point-in-time-spines-and-calendar-imports)
for the modeling side). A spine's timeline is **generated**
(`_spine_cte` → a `range()` CTE bounded by `_spine_bounds`, cached
per-model/per-filter-set with a short TTL since the underlying data can
move); an interval import reads a **real** date table, thinned to the
query's grain by `_period_rows`.

### Filters and relative dates

`_filter_sql()` compiles one filter spec into `(predicate SQL, bound
values)`. `FILTER_OPS = {eq, ne, gt, gte, lt, lte, in, not_in, contains}`;
`contains` compiles to a case-insensitive regex
(`regexp_matches(CAST(col AS VARCHAR), ?)`) over the value as a *pattern*,
not a literal substring — this is why instant-mode extracts never hoist a
`contains` filter (see [Instant mode](#instant-mode-appextractpy) below).

A date/time filter's value is either a fixed ISO date or a **relative
date** — one keyword (`today`, `start_of_month`, `end_of_quarter`, …),
optionally followed by exactly one offset (`start_of_year-1y`). The whole
grammar is defined once, as data, in `RELATIVE_DATE_KEYWORDS` /
`RELATIVE_OFFSET_UNITS` / `resolve_relative_date()` — and everything that
*describes* this grammar to a person or an LLM (the chat tool schema and
system prompt in `app/llm.py`, the builder's filter control in
`static/js/filters.js`) is built from these same constants rather than
restating them, so what a human or an LLM is told is legal can never drift
from what the engine actually accepts. An offset always shifts *today*
first and the keyword then takes that shifted date's period edge, so
`end_of_month+1mo` is a real month-end, whatever that month's length. A
value outside the grammar raises `QueryError` (never a bare `ValueError`) —
`date_value_error()` lets a caller upstream (`nlq.py`'s re-validation)
reject a bad LLM-proposed value using the engine's own message before a
query ever runs.

### Measures: plain, window, framed

`_split_measures()` sorts a query's requested measures (and any inline
ones) into three buckets, resolving a window measure's sibling references
transitively (a `SUM(revenue) OVER w` measure that wasn't explicitly
requested still gets computed if another requested window measure depends
on it, then trimmed from the final projection if not asked for directly):

- **plain** — aggregated in the one `__agg` CTE's `GROUP BY ALL`.
- **window** (`_window_cte`) — computed *after* the group-by, over a window
  the engine supplies and names `w`: `PARTITION BY <the query's other
  dimensions> ORDER BY <its one time dimension>`. Exactly one time dimension
  is required to order by (`QueryError` otherwise — ambiguous ordering with
  more than one, nothing to order by with none).
- **framed** (`_framed_ctes`) — a `from:` measure gets its own CTE, evaluated
  against the model relation with the query's dimensions already
  materialized, then merged onto the rest via a **null-safe `FULL OUTER
  JOIN`** (`IS NOT DISTINCT FROM`) — so a bucket only one measure's relation
  has rows for keeps its row, with everything else null (read as "nothing
  measured here," not zero). A dimension named in the measure's `emits:` is
  the block's own output column, grouped on afterward at the query's grain
  rather than inherited from the raw scan.

### Several fact tables in one model (`_build_parts`)

For a composite model, `_build_parts()`:

1. Groups the query's requested measures by which `ModelPart` owns each
   (`semantic.part_for_measure`) — **only the parts a measure was actually
   requested from are read at all**.
2. Takes `shared_dimensions()` over just those parts (a subset of the
   model's all-parts intersection — see
   [Semantic Layer](semantic-layer.md#several-fact-tables-in-one-model)).
3. Builds one `_build_single()` CTE per part, each grouped by the shared
   dimensions (types normalized — a `DATE` meets a `TIMESTAMP` — via a cast
   before the merge key comparison).
4. Merges the per-part CTEs with the same null-safe `FULL OUTER JOIN`
   pattern as framed measures, on the shared dimension columns.
5. Sorts and applies `LIMIT` **after** the merge, so a limit can never drop
   a bucket a still-unread-from-yet part has rows for.

Inline (visual-scoped, ad-hoc) measures are refused outright on a composite
model (`QueryError`) — an inline expression needs one frame to evaluate
against, so it has to be declared on the specific dataset it belongs to.

## The SQL grammar

`app/sqlgrammar.py` is the single boundary every fragment of authored SQL
passes through before it can reach a DuckDB connection — a model measure's
`expr:`, a `from:` block, an inline (visual-scoped) measure, and a
chat-proposed inline measure all go through the *identical* path. There is
no separate, more-trusted or less-trusted variant.

**Mechanism.** DuckDB's own parser (`json_serialize_sql()`) turns the text
into an AST as JSON — parse only, no planning, binding, or catalog access,
so validating hostile input is not itself an execution. `_Walker` walks
that JSON and refuses any node `class` not in an explicit allowlist
(`_EXPRESSION_CLASSES` for a measure, `_RELATION_CLASSES` — adds
`SUBQUERY`/`STAR` — for a `from:` block). **Fail closed**: an unrecognized
class (including one a future DuckDB version introduces) is refused, never
silently passed through. The engine never embeds the author's own text —
the validated node is transplanted into a clean template statement and
**re-serialized** (`_emit`), so a trailing `--` comment or an unbalanced
quote that survives a naive parse can never reach the emitted SQL.

**The function allowlist is derived, not hand-written**: every scalar,
aggregate or macro function in `duckdb_functions()`, minus `_DENY_PREFIXES`
(`pg_`, `duckdb_`, `read_`, `write_`, `sniff_`, `install_`, `load_`) and a
short `_DENY_NAMES` list (`current_setting`, `getenv`-adjacent
introspection, `nextval`, `random`/`uuid`-family nondeterminism, …).
Filtering by `function_type` is what excludes **every one of DuckDB's ~150
table functions structurally** — `read_parquet`, `delta_scan`,
`glob`, `duckdb_settings` — rather than by name, so nothing has to remember
to blocklist a new one when DuckDB adds it.

**What's legal inside an `expr:`**: column references, literals, the usual
operators, `IN`, `AND`/`OR`/`NOT`, `IS [NOT] NULL`, `BETWEEN`,
`LIKE`/`ILIKE`, `CASE WHEN`, `CAST`, `COALESCE`, aggregate
`FILTER (WHERE …)`, `DISTINCT` aggregates, and any allowed scalar/aggregate/
macro call. `_require_aggregate()` rejects an expression with no aggregate
at all (a bare column reduces nothing). A **window** measure
(`is_window_expr`) is legal too — `_WINDOW_ONLY` names (`lag`, `rank`,
`row_number`, …) are refused *without* an `OVER` clause, with a message
naming the fix, since DuckDB itself only complains at bind time, too late
to be useful.

**What's legal inside a `from:` block** (`compile_relation`, a stricter
profile): exactly one `SELECT`/`SET_OPERATION`, **no table function at
all**, and no base table other than `{model}` (rendered as the literal name
`MODEL_RELATION = "__model"`) or a CTE the block declares itself
(`_cte_names`). That single rule — no table functions — is the entire I/O
boundary: with them gone there's no `read_parquet`, no `glob`, no
`iceberg_scan`, nothing that reaches outside the relation it was handed.
Inside those bounds it's ordinary SQL — CTEs, joins between its own CTEs,
window functions, `QUALIFY`.

**Visual parameters** (`param('name')`) are resolved to a literal **before**
parsing (`_substitute_params`), and the substituted text is then parsed and
walked like everything else — so a value that tried to be more than a
literal fails the walk just like any other illegal construct. Each
parameter's declared values are validated by type
(`engine.PARAM_TYPES = {int, float, string}`,
`resolve_parameter_values()`), and `LAG`/`LEAD`'s offset position gets one
extra rule (`lag_period_param_names`): whatever resolves there must be a
genuine `int`, so a `float`-typed parameter is refused even when its value
happens to be numerically whole.

**Limits** (`MAX_SQL_LEN=2000`, `MAX_RELATION_LEN=20000`, `MAX_NODES=400`,
`MAX_DEPTH=40`) bound the *shape* of an expression, not its cost — a
statement timeout and DuckDB's own memory limit are the separate mechanisms
for that.

**The old (pre-DuckDB) measure DSL** is gone; its syntax is a load-time
error (`LEGACY_REPLACEMENTS`, `kind="legacy_dsl"`) naming the SQL
equivalent — `count_distinct(x)` → `COUNT(DISTINCT x)`, `where(v, pred)` →
`SUM(v) FILTER (WHERE pred)`, `running_total(m)` → `SUM(m) OVER w`, etc. —
rather than a confusing "unknown function."

**Decomposition for instant mode** (`rollup_plan`) is the one thing this
module does beyond validate-and-emit: it walks a *validated* expression and,
where possible, factors it into additive components a browser can
recombine after a roll-up. See [Instant mode](#instant-mode-appextractpy)
below.

## `duck.py`: one connection, tuned for a real object store

**One process-wide `duckdb.DuckDBPyConnection`** (`connection()`), opened
lazily on first use; every caller gets a `cursor()` off it instead of a
connection of its own. This is deliberate, not incidental: DuckDB's
`enable_object_cache` (parquet footers), `enable_external_file_cache` (file
bytes, block-level LRU) and `http_keep_alive` all cache at the *instance*
level, so a second connection starts cold and a connection-per-query makes
every one of those caches useless. One setting is deliberately **off**:
`enable_http_metadata_cache` — it has no TTL or invalidation, and both other
caches validate against it, so leaving it on would mean an object
overwritten in the bucket keeps answering with stale rows for the process's
whole lifetime.

**Credentials as scoped DuckDB `SECRET`s.** Two store-level secrets
(`cash_intel_s3` for the primary store, `cash_intel_demo_s3` scoped to
`s3://<demo bucket>` when the two stores differ) — DuckDB picks between them
by longest matching scope, which is what lets one query read a demo path
and a real path through two different endpoints and credentials at once. A
bucket whose *actual* AWS region differs from its store's configured region
gets a third, still-narrower secret (`_bucket_secret`, keyed by
`s3.bucket_region`) — getting this wrong isn't cosmetic: SigV4 puts the
region in the credential scope, so a mismatched region is refused outright
rather than merely warned about.

**Two caches DuckDB can't build for itself**, both fronted by `app/cache.py`'s
generic `get_or_set(key, ttl, compute)`:

- **Object listings** (`objects()` / `_list_objects`, TTL
  `CI_SOURCE_CACHE_TTL`, default 60s) — resolving a glob to a concrete file
  list once, so `read_parquet([...])` is handed an already-resolved list
  instead of re-`LIST`ing the prefix on every query. Past
  `CI_LIST_MAX_KEYS` (default 20,000), this falls back to letting DuckDB
  glob the prefix itself per query instead — a listing that wide isn't
  worth resolving and holding.
- **Pinned small sources** (`relation()` / `_pin_source`) — a source under
  `CI_SOURCE_CACHE_MAX_BYTES` (16 MB, checked against the *object-store*
  byte total *before* reading) is read once into a real local DuckDB table
  and queried from there instead of re-scanned every time; a second check
  (`CI_SOURCE_CACHE_MAX_RESIDENT_BYTES`, 256 MB) drops it again if it
  expanded past that once decompressed in memory. This targets exactly the
  sources that get re-read on *every* query that touches them and are
  usually tiny — a `joins:` lookup table, a dimension bundle's datasets —
  while large fact tables stay streamed with pushdown intact.

**`invalidate()`** drops every pinned table and clears the external file
cache — called only where the platform itself *writes* to the bucket (a
successful pipeline run, a dataset upload/delete), never on a mere
model/bundle YAML edit, since that changes what a path *means*, not a byte
in the bucket. A model reload is deliberately cache-transparent: every
cache entry here is keyed on what actually determines its answer (a source
path, a rendered scan's SQL, or a per-`Model`-instance token from
`engine._model_cache_key` that dies with the object a reload replaces), so
clearing on every edit would only have made the authoring loop cold for no
correctness benefit. See [Performance](#performance) below for the latency
numbers this design earns.

## Performance

Measured against a real object store with **40ms of injected per-request
latency**, on the `sales` model (parquet glob + a CSV join + two dimension
bundles). *naive* is every cache off (DuckDB's own and `CI_SOURCE_CACHE_TTL=0`)
— what pointing an untuned engine at `s3://` costs; *tuned* is the shipped
defaults:

| | S3 requests | naive | tuned |
|---|---|---|---|
| first query in a session | 29 → 22 | 1881ms | 1397ms |
| the same visual re-run | 14 → 0 | 725ms | **22ms** |
| edit it: different dimensions/measures | 16 → 0 | 851ms | **18ms** |
| 6-tile dashboard | 74 → 0 | 3878ms | **78ms** |

The first query is deliberately barely changed — nothing is prefetched, and
a cold cache still pays for data it has never read (what's saved is the
re-`LIST`ing and the per-read handshakes). What goes away is paying for it
*again* on every interaction after — the number that decides whether
clicking around a dashboard feels alive.

The same harness on the **model-authoring loop** — a 130MB, 4-file parquet
fact table, the demo catalog loaded alongside, credentials via an
`AWS_PROFILE` whose resolution costs 150ms (an SSO/corporate stand-in) —
before and after three fixes (one held credential resolver instead of
one per DuckDB cursor, no cache-clearing on model save, the HTTP metadata
cache left off):

| | before | after |
|---|---|---|
| open the model form (cold validate) | 2019ms | 253ms |
| per-keystroke re-validation | ~990ms | **11ms** |
| save the model | **20,668ms** | **149ms** |
| first query (cold read of the data) | 2558ms | 1312ms |
| re-run / after-save query | 2144–3001ms, re-reading everything | **185–214ms**, 4 requests |

The save was the headline failure: with a slow credential chain, every
DuckDB cursor re-resolved it under the connection lock, a save
re-validates every measure in the catalog through a cursor each, and the
editor's debounced re-validations queued up behind the lot — "saving…"
sat for tens of seconds while the browser looked hung.

Against a 13M-row NYC-taxi fact table (`python -m app.load_taxi`, 4 months,
336MB across 4 snappy parquet files, measured through the full stack —
HTTP → semantic layer → one DuckDB statement over emulated S3 → Arrow →
JSON):

| query | rows out | cold | warm |
|---|---|---|---|
| grand totals (trips, revenue, tip %) | 1 | 1.8s | 59ms |
| monthly trend (trips, revenue) | 4 | 1.6s | 240ms |
| avg fare by payment type | 6 | 810ms | 34ms |
| daily trend, filtered to 2 weeks | 14 | 810ms | 45ms |

*cold* is a fresh connection with nothing cached — what the first query
after a restart costs; *warm* is the same query again on that connection —
what every interaction after the first actually costs. Predicate/projection
pushdown does the heavy lifting on the cold read (only referenced columns'
row groups leave the bucket); the parquet-metadata and external-file
caches do it on the warm one.

**If every query takes as long as the first** — the same visual re-run
costs the same tens of seconds, forever — the likely culprit is between
the app and the bucket, not in it: DuckDB probes range-request support
when it opens a file, and an intermediary that mangles that probe
(corporate TLS-inspection proxies, some VPN gateways) makes it fall back to
downloading **entire objects** into memory on every query, bypassing the
byte cache. The tell is DuckDB's own warning (`Falling back to full file
download … the server does not support HTTP range requests`) and
cold-sized timings on warm repeats; verify with
`curl -sI -H "Range: bytes=0-99" https://<bucket>.s3.<region>.amazonaws.com/<key>`
from the same network — anything other than `206` with a `Content-Range`
is the proxy talking, not S3.

## `cache.py`: the shared primitive

A tiny, generic in-process TTL cache (`get_or_set`, `clear`) with no
invalidation tracking of its own — entries simply expire. `duck.py`'s two
caches above, and `engine.py`'s `scan_schema`/`_spine_bounds` memoization,
are all built on this one primitive. A cache miss also sweeps already-expired
entries, which is what keeps a cache fed a stream of one-off keys (distinct
ad hoc filter combinations, say) from growing without bound.

## Instant mode (`app/extract.py`)

Instant mode is what lets a dashboard tile answer a cross-filter, a view
filter change, a coarser grain change, or opening focus mode **with zero
network calls** after its first fetch. It's a client-side re-aggregation
scheme, not a new query capability — `engine.run_query`'s authorization,
model resolution and pushdown are all untouched; this module only decides
what to put in the one extract a tile fetches, and enforces the size cap
that sends a tile back to the live path.

`POST /api/query/extract` (see [API Layer](api-layer.md)) answers with
either an **Arrow IPC stream** or a small `{"fallback": {reason, cap?}}`
JSON — declining is a *routine*, expected answer on a dashboard that mixes
instant and live tiles, never an error status.

**Three things make an extract different from an ordinary query result:**

1. **It's wider.** Alongside the tile's own dimensions, `plan()` unions in
   every *other* dashboard tile's dimensions that this tile's model also
   has (so a cross-filter originating elsewhere can be applied locally) —
   except time dimensions, since no chart renderer ever emits a cross-filter
   from a time mark, and they'd be the most expensive thing to carry at
   ungrained resolution.
2. **Measures are decomposed, not re-averaged.** Each requested measure is
   replaced by its additive components via `sqlgrammar.rollup_plan` — e.g.
   `AVG(fare)` becomes `SUM(fare)` + `COUNT(fare)`, recombined by the
   browser only *after* the roll-up, which is what keeps a mean or ratio
   exact rather than an average-of-averages. A measure with no
   decomposition (`COUNT(DISTINCT …)`, `MEDIAN`, `STDDEV`, a window measure,
   a `from:` measure) makes that tile ineligible (`NotInstantable`) — it
   silently stays on the live path rather than rendering a plausible-looking
   wrong number.
3. **It carries precomputed coarser time buckets** (`_add_coarser_grains`,
   via `pyarrow.compute.floor_temporal`) for the tile's own date columns, so
   a grain-override toward something *coarser* is answered from a column
   already there — weeks don't nest in months, so a week-grained extract
   offers nothing coarser and a request for one re-fetches.

**Filter hoisting.** A dashboard view's filter is normally baked into the
extract's pushdown (changing it re-fetches); `plan(hoist=True)` instead
leaves an *interactive* filter's field out of the pushdown and carries its
dimension as a column, so changing its value is a local re-slice. Two kinds
never hoist: a **time** filter (the extract holds dates truncated to the
tile's grain, so a raw-column range filter wouldn't reproduce exactly) and
`contains` (a regex the browser can't reproduce as a substring match).
`build()` tries hoisted first and retries once with everything pushed back
down if that trips the size cap — giving up the hoisting, not the whole
tile.

**Size cap**, checked against the real serialized response:
`CI_EXTRACT_MAX_ROWS` (150,000) and `CI_EXTRACT_MAX_BYTES` (25 MB) — either
tripping raises `CapExceeded` and the tile runs live. `_normalize()` then
coerces every Arrow column into a shape Perspective (the client-side
aggregation engine — see [Frontend](frontend.md#instant-mode-client-side-re-aggregation))
and the live JSON path agree on byte-for-byte, so a cross-filter value
clicked on a live tile compares equal to the same value inside an extract.

Fallback is per-tile, silent, and permanent for the session — a dashboard
routinely ends up with a mix of instant and live tiles, each carrying a
visible `⚡ instant`/`live` badge whose tooltip names the row count/size or
the reason it fell back.
