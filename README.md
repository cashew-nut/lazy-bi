# CASH_INTELLIGENCE

Lightweight BI over data files in S3. Polars scans the files **lazily** — only
the columns and row-groups a query needs leave the bucket — aggregates them, and
returns results to a cyberpunk query-builder UI. A YAML **semantic layer**
defines the sources (**parquet / csv / Delta Lake / Iceberg**), **joins**, dimensions and
measures the builder works with; saved visuals and **dashboards** persist in
SQLite. **Pipelines** (see [below](#pipelines)) host real polars scripts that
materialize new sources into the bucket — replace or upsert, with delete
handling — and document their field-level lineage on the models they feed,
visualized as a graph.

```
browser (login + query builder + dashboards + SVG charts)
   │  POST /api/query {dimensions, measures, filters, sort, limit}
   │  session cookie or bearer token on every request
   ▼
FastAPI (auth middleware: viewer/author/admin roles)
   └──► semantic layer (models/*.yaml) ──► polars LazyFrame scan (+ lazy joins)
   │                                          │ predicate/projection pushdown
   ▼                                          ▼
SQLite (visuals + dashboards +            S3 (moto emulator in demo mode)
        users/sessions/audit)
```

## Run the demo

**Docker (recommended):**

```bash
docker compose up              # demo mode on http://127.0.0.1:8080
docker compose --profile minio up   # + MinIO-backed instance on :8081
```

The default service runs the embedded S3 emulator in-process and seeds it on
start. SQLite state lives in the `app-data` volume; `./models` is mounted so
semantic models are editable from the host (or the in-app editor); mount
`./data_cache` after `python -m app.load_taxi` for the big-data model. The
image runs a single uvicorn worker by design — the emulator is in-process and
sqlite expects one writer. Scale out only against an external S3 endpoint.

Conversational analytics, the Composer, and the sandbox coding agent are off
until `CI_LLM_API_KEY` reaches the container — copy `.env.example` to `.env`
and fill it in, or `export CI_LLM_API_KEY=...` before running `docker compose
up`; `docker-compose.yml` passes it (and the rest of the `CI_LLM_*` settings)
through automatically. Any OpenAI- or Anthropic-compatible endpoint works —
add `CI_LLM_BASE_URL` and `CI_LLM_MODEL` for anything other than Anthropic's
own API. See "Conversational analytics" below for what that enables, which
providers are supported, and what it sends to whichever one you configure.

**Local (no Docker):**

```bash
python3 -m venv .venv          # Python 3.10+
.venv/bin/pip install -r requirements.txt
./run.sh                       # or: .venv/bin/uvicorn app.main:app --port 8080
```

**Tests:**

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/    # ~6s: semantic, engine, store, API suites
```

Open http://127.0.0.1:8080. On startup the app launches an **embedded moto S3
server** on `127.0.0.1:9600`, creates the `cash-intel` bucket, and seeds it with
demo data — only if the bucket is empty. One dataset per source format:

| S3 key | format | model |
|---|---|---|
| `sales/<year>.parquet` | parquet glob | `sales` (60k order lines) |
| `ref/products.csv` | csv | joined into `sales` (supplier, tier) |
| `logistics/shipments` | Delta Lake | `logistics` (20k shipments) |
| `marketing/spend.parquet` | parquet | `marketing` |
| `support/tickets` | Iceberg | `support` (15k support tickets) |

To point at a real bucket or an external emulator (MinIO, LocalStack), set
`CI_S3_ENDPOINT` (this also disables the embedded moto server) plus the usual
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, and `CI_BUCKET`.

**Your own raw data**: drop a folder of `.csv`/`.parquet` files under
`raw_data/<dataset-name>/` (committed to the repo, unlike the gitignored
`data_cache/`) and it's uploaded unmodeled on startup into the same
`cash-intel` bucket, flat under `<dataset-name>/<filename>` — pick it up from
the Modelling workspace's source picker and build a model on it from
scratch. The repo doesn't ship a `raw_data/` dataset by default; the demo
catalog above is generated straight into the bucket instead.

**Signing in**: everything requires an account (there is no anonymous mode —
the demo exercises the same auth path as a real deployment). On first start
with no accounts, a bootstrap `admin` is created and its **random password is
printed once in the startup log** — sign in with it, then create your own
accounts under **ACCOUNT**. Three nested roles: **viewer** (query + read),
**author** (save visuals/dashboards/model measures), **admin** (raw model
YAML, user management). Sessions are cookie-based (7-day idle / 30-day
absolute by default — `CI_SESSION_IDLE_DAYS` / `CI_SESSION_MAX_DAYS`); set
`CI_COOKIE_SECURE=1` when serving over TLS. Scripts authenticate with
per-user tokens instead (see "Authoring model measures" below). The old
shared `CI_API_KEY` secret is retired and grants nothing.

## Project layout

```
app/
  config.py            env-driven settings (endpoints, paths, bucket, sessions)
  main.py              app factory + lifecycle + AuthMiddleware (default-deny /api)
  registry.py          runtime state: loaded models + pipelines + stores
  auth.py              identity core: principal, argon2id, sessions/tokens, roles
  authstore.py         sqlite persistence: users, sessions, api tokens, audit
  semantic.py          semantic layer: yaml -> Model/Dimension/Measure/Join/Spine/Geo/
                       DimensionBundle/Import/pipeline_lineage section
  engine.py            query engine: semantic query -> polars lazy scan
  extract.py           instant-mode extracts: what a tile must fetch to re-aggregate
                       client-side, as Arrow IPC, under a per-tile size cap
  store.py             sqlite persistence: visuals, dashboards, publications
  pipelines.py         pipeline layer: yaml -> Pipeline/Materialization/LineageEntry/Layer,
                       lineage validation + model-lineage-section building
  pipeline_runner.py   subprocess entry point: execs a pipeline's script, materializes its output
  pipeline_jobs.py     FIFO run worker (one subprocess at a time) + post-run lineage sync
  pipelinestore.py     sqlite persistence: pipeline_runs (append-only run history)
  materialize.py       replace/upsert writers: delta merge + delete policies, pre-write guards
  sandbox.py           sandbox notebooks: cell-combining, read()-call detection, convert-to-pipeline yaml
  sandbox_runner.py    subprocess entry point: execs a notebook's cells, reports per-cell output
  sandbox_agent.py     the sandbox coding agent's LLM seam: notebook context, polars prompt, lineage tool
  sandboxstore.py      sqlite persistence: saved sandbox notebooks
  iceberg_util.py      catalog-free iceberg reads: resolve + scan a table's current snapshot
  skills.py            Skill abstraction + registry + invoke_skill() dispatch (role check,
                       rate limit, audit) — see "Agents & MCP server" below
  agents.py            Agent abstraction + agents/*.yaml loader
  skills_analytics.py  the analytics agent's skills: ask_question, list_models
  mcpserver.py         the MCP server mounted at /mcp — tools/list and tools/call
                       synthesized live from the skill/agent registries on every request
  emulator.py, s3.py, seed.py, load_taxi.py
  api/                 one router per resource: auth, users (+tokens), models,
                       dimensions, datasets, query, visuals, dashboards
                       (+publish/portal), explorer (+health), pipelines (+lineage/layers/graph),
                       sandbox
  static/js/           ES modules: lib, state, auth, admin, filters, builder,
                       dashboard, instant, portal, modelling, editor, completion,
                       measurelab, lineagegraph, sandbox, sandboxagent,
                       pyhighlight, main
  static/js/charts/    one renderer per chart + shared frame/pivot/dispatch
  static/vendor/       third-party assets, committed not CDN-loaded (Perspective)
models/*.yaml          semantic models (the editable contract)
dimensions/*.yaml      dimension bundles shared across models (see below)
pipelines/*.yaml       hosted polars transformation scripts (see below); layers.yaml
agents/*.yaml          declared Agents — name + description + which skills they expose (see below)
tests/                 pytest: semantic, engine, store, API, pipelines
Dockerfile, docker-compose.yml
```

## The semantic model

One YAML file per model in `models/`. The query builder only exposes what the
model declares — the UI never touches raw columns directly.

```yaml
name: sales
label: Sales Orders
source:
  format: parquet                      # parquet | csv | delta | iceberg
  path: s3://cash-intel/sales/*.parquet  # any glob polars can scan (delta/iceberg: table root)

joins:                    # lookup tables joined lazily into the base scan;
  - name: products        # joined columns are then usable in dimensions/measures
    source: { format: csv, path: s3://cash-intel/ref/products.csv }
    on: product           # or left_on/right_on; how: left (default) | inner

dimensions:
  - name: order_date
    type: time            # gets day/week/month/quarter/year grains in the UI
  - name: region          # column defaults to the name; label auto-titled
  - name: category
    column: cat_code      # column can differ from the semantic name
    label: Category

measures:                 # the safe measure DSL — see below
  - name: revenue
    label: Revenue
    format: currency      # number | currency | percent (display hint)
    expr: sum(unit_price * quantity)
  - name: margin_pct
    format: percent
    expr: sum((unit_price - unit_cost) * quantity) / sum(unit_price * quantity)
```

A measure reduces to one value per group — ratios of aggregates,
`count_distinct`, filtered sums like `sum(where(x, flag))`, all fine.
Expressions are validated at load time; edit a YAML and hit
`POST /api/models/reload` (or restart) to pick it up.

`source:` + `joins:` above is the terse spelling of the single-table case. The
general shape is [`datasets:`](#several-fact-tables-in-one-model) — every table
the model reads, plus the relations between them — and the two parse into the
same thing, so a model written either way behaves identically.

### The safe measure DSL

A measure is **not** arbitrary Python. It's a small, allowlisted expression
language, parsed to an AST and compiled straight to a `polars.Expr` — the
compiler (`app/measure_dsl.py`) never calls `eval`, `exec`, or `compile` on
measure text, so there is nothing dangerous to execute regardless of who
supplies it. Both model measures (above) and inline/visual-scoped measures
(the measure lab, `inline_measures` on `/api/query`) compile through the
exact same allowlist — saving a model measure grants governance (see below),
not extra language power.

Grammar: column references (bare names, or `col("name")`), literals, the
arithmetic/comparison/boolean operators you'd expect (`+ - * / % **`,
`== != < <= > >= in not in`, `and or not`), and calls to a fixed set of
functions:

| Function | Meaning |
|---|---|
| `sum(x) mean(x) min(x) max(x) median(x) std(x) var(x) first(x) last(x)` | aggregations |
| `count()` / `count(x)` | row count / non-null count of `x` |
| `count_distinct(x)` | distinct count |
| `col("name")` | explicit column reference (bare `name` works too) |
| `where(value, predicate)` | filter before aggregating — `sum(where(revenue, region == "EU"))` |
| `if_(predicate, then, else)` | conditional — `pl.when(...).then(...).otherwise(...)` |
| `coalesce(a, b, ...)` | first non-null of the arguments |
| `cast(x, "int"\|"float"\|"str"\|"bool")` | change type |

Anything outside this — attribute access (`x.__class__`), subscripts,
lambdas, comprehensions, f-strings, I/O calls, calling anything that isn't a
bare allowlisted name — is rejected at compile time (`MeasureCompileError`),
along with unknown columns/functions and oversized or deeply-nested input.
See `specs/008-safe-measure-compilation/contracts/compile_measure.md` for the
full grammar and the node-by-node allowlist.

#### Window measures: running totals and period-over-period change

`running_total(x)` and `lag(x[, periods=1])` are a second kind of measure.
Every function above reduces *raw source rows* down to one value per query
group (that's what "aggregation" means); these two instead read a **sibling
measure's already-aggregated value** and look sideways/backwards across the
query's date axis — there's no such thing as "the previous quarter" until
quarters have been grouped. Using either anywhere in an expression makes the
whole measure a window measure: bare names inside it refer to other
measures in the same query, not raw columns, and the aggregate functions
(`sum`, `count`, ...) and `col()` aren't available inside it — there are no
raw rows left to reduce. `if_`/`coalesce`/`cast` still are, since they're
plain scalar transforms.

```yaml
measures:
  - name: revenue
    expr: sum(unit_price * quantity)
  - name: revenue_running_total
    expr: running_total(revenue)
  - name: revenue_pct_change   # % change vs. the previous point on the date axis
    expr: (revenue - lag(revenue, 1)) / lag(revenue, 1)
```

Querying `revenue_pct_change` grouped by `order_date` at quarter grain gives
quarter-over-quarter change; at month grain, month-over-month — the DSL text
doesn't hardcode a period, the query's own grain does. The engine applies
these `.over(partition_by=the query's other dimensions, order_by=its time
dimension)` right after the group-by, so add a breakout dimension (e.g.
`channel`) and each gets its own independent running total / prior-period
comparison. A window measure's referenced sibling is computed even if the
query didn't ask for it directly (dropped from the response unless also
requested), but a query needs **exactly one time dimension** to order by —
zero or more than one is rejected with a clear error. Window measures follow
the same trust model as everything else here: inline/query-time and saved
model measures compile through the identical allowlist, no distinction.

#### Visual parameters: viewer-toggleable, typed values anywhere a literal is legal

A visual can declare a named parameter — a fixed list of allowed values
plus a default, typed as `int`, `float`, or `string` (omit `type` for
`int`, matching every parameter declared before this existed) — and
reference it from `param('name')` anywhere a literal constant is already
legal in a measure: comparisons, `if_()`'s predicate and branches,
`coalesce()`'s arguments, `where()`'s predicate, `cast()`'s value
argument, and `lag()`'s periods argument.

```
period_list = [1, 2, 3, 4]  (int, default 1)
revenue_lag = lag(revenue, param('period_list'))

threshold = [10, 50.5, 100]  (float, default 50.5)
flagged_revenue = if_(revenue > param('threshold'), revenue, 0)

target_channel = ["online", "retail"]  (string, default "online")
channel_orders = count(where(channel, channel == param('target_channel')))
```

Whoever is viewing the visual gets a control listing the parameter's
declared values (a text picker for `string`, numeric for `int`/`float`);
picking one re-runs the query with that value, no expression editing
involved. `lag()`'s periods argument keeps one extra rule on top of the
general case: whatever resolves there must be a genuine `int` — a
`float`-typed parameter is rejected there even when its value is
numerically whole (e.g. `2.0`), and `cast()`'s *type-name* argument (its
`"int"`/`"float"`/`"str"`/`"bool"` string) never accepts `param()` at all,
only its value argument does. This all stays fully inside the same
allowlisting compiler as every other measure (see "The safe measure DSL"
above): the server only ever substitutes one of the parameter's own
declared, correctly-typed values, never an arbitrary one, the same way
`partition_by`/`order_by` are threaded in from query context today.
Because a parameter is visual-scoped context a shared model measure never
has, a measure referencing one — in any position — can only be
**SAVE TO VISUAL**'d, never promoted to the model — see "The measure lab"
below.

On a dashboard, a parameter's current selection is saved per named view,
alongside its filters (in portal/viewer mode, toggling a parameter stays
session-local and is never written back, matching how filters already
behave there). If two tiles' visuals declare a parameter with the same
name *and* an identical definition (same type, same values, same
default), the dashboard shows one shared control that drives both; if the
definitions differ — including a type mismatch alone, even when the
values happen to look similar (`int` `[1,2,3]` vs. `string`
`["1","2","3"]`) — the dashboard refuses to let both visuals sit on it
together (add-tile and every dashboard save both enforce this — see
`specs/009-visual-parameters/` and `specs/010-parameter-type-
generalization/`). Wiring a parameter into a dashboard/visual *filter*
(as opposed to a measure expression) isn't built — a distinct, larger
feature touching the filter subsystem instead of the measure DSL.

### Measures over an intermediary frame (authenticated model measures only)

Some metrics can't be written in the safe DSL above — they need business
logic *between* the scan and the final reduce ("per entity, derive X; then
take the median of X across entities"), which means real multi-step Python,
not a small expression. Give a measure a `frame:` block — a python snippet
that builds a derived LazyFrame, still `eval`/`exec`-based like model YAML
always has been — and its `expr:` then aggregates over that frame (using the
same pre-DSL polars-expression syntax, since it's reading columns the frame
itself produces, not the base schema):

This is a deliberate, narrow carve-out: it is **only ever available through
the authenticated model-measure save endpoint, and only to the admin role**
(see "Authoring model measures" below) — never as an inline/visual-scoped
measure, regardless of credentials. A `frame` submitted inline on
`/api/query` is rejected outright.

```yaml
measures:
  - name: median_days_to_75pct
    description: Median days for a study to log 75% of its events.
    frame: |                       # `lf`, `dims`, `pl` in scope
      keys = list(dict.fromkeys(["study_id", *dims]))
      ordered = lf.sort("event_date").with_columns(
          (pl.int_range(1, pl.len() + 1).over(keys) / pl.len().over(keys)).alias("cume"),
          pl.col("event_date").min().over(keys).alias("first_event"),
      )
      frame = (
          ordered.filter(pl.col("cume") >= 0.75)
          .group_by(keys)
          .agg(pl.col("first_event").first(), pl.col("event_date").min().alias("date_75"))
          .with_columns((pl.col("date_75") - pl.col("first_event")).dt.total_days().alias("days_to_75"))
      )
    expr: pl.col("days_to_75").median()
```

The snippet sees `lf` — the model's scan with the query's filters applied and
its dimension columns already materialized (grains included) — plus `dims`, the
list of those dimension names, and `pl`. It is either a single expression or
statements assigning the result to a variable named `frame`. Carry `dims`
through every `group_by` (as above) and the measure re-aggregates correctly at
whatever grouping the query asks for; the engine groups the derived frame by
`dims`, applies `expr`, and left-joins the result onto the other measures, so
framed and plain measures mix freely in one query. Groups the derived frame
has no rows for come back null. Everything stays lazy end to end.

**Timelines and `frame_emits`.** Grouping a framed measure by a time dimension
raises a question the model author has to answer: should the time bucket
partition the *raw events* before the intermediary step (splitting each
entity's history per bucket), or should it bucket the *derived rows* after it?
For per-entity milestone metrics like the example above, it's the latter — so
declare the dimension in `frame_emits` and output a column of that name from
the frame:

```yaml
    frame: |
      ...
          .with_columns(pl.col("date_75").alias("event_date"))   # the frame's own date
    frame_emits: [event_date]
    expr: pl.col("days_to_75").median()
```

An emitted dimension is withheld from `dims` during the step (the intermediary
partitions stay whole) and applied to the frame's output afterwards — the
engine truncates it at the query's grain and groups the derived rows by it. On
a timeline each entity then lands in the bucket of its own milestone date, and
buckets only exist where some entity crossed. Dimensions *not* listed in
`frame_emits` behave as before: carried through the step via `dims`.

See `median_tenure_days` in `models/subscriptions.yaml` for a live example
(median tenure of ended subscriptions, bucketed on timelines by each one's
churn month — date arithmetic like `end_date - start_date` is outside the
safe measure DSL, so it has to be derived in the frame step instead of a
plain `expr`). Inline/visual-scoped measures on the query API cannot use
`frame`/`frame_emits` — that construct is authenticated-model-measure-only
(see above).

### Time-spine (point-in-time) measures

A plain group-by puts each row in one time bucket. For "active customers"-style
questions — the row has a start and an end date and should count in **every**
bucket in between — mark a time dimension as a **spine**:

```yaml
dimensions:
  - name: active_at
    type: time
    spine:
      start: start_date
      end: end_date        # null end = still active
measures:
  - name: active_customers
    expr: count_distinct(customer_id)
```

Grouping by `active_at` generates a timeline at the requested grain and
interval-joins it against `[start_date, end_date]` (polars `join_where`), so
each row counts in every period it was active for. Range filters on the spine
(`>=`, `<=`, `=`) bound the timeline window; `=` gives a single-date snapshot
even with no grouping. Buckets with zero active rows are omitted. One spine
dimension per query. See `models/subscriptions.yaml` for a working example
(active customers, MRR, ARPU over a 30-month timeline).

#### What "active in this period" means (`match`)

Three readings, and the choice changes the numbers — so it is explicit. Take
two records: **A** open from Jan 1st with no end, **B** open Feb 2nd–15th only.

```yaml
    spine:
      start: start_date
      end: end_date
      match: overlap       # overlap (default) | period_start | period_end
```

| `match` | Jan | Feb | Q1 | 2026 | reading |
|---|---|---|---|---|---|
| `overlap` | 1 | **2** | **2** | **2** | active at *any point* during the period |
| `period_start` | 1 | 1 | 1 | 1 | already open on the period's first day |
| `period_end` | 1 | 1 | 1 | 1 | still open on the period's last day |

`overlap` is the default because it is what "active during February" normally
means. The other two are *snapshots*: **B** spans neither Feb 1st nor Feb 28th,
so it is invisible to both — and to a quarterly or yearly snapshot too, which
is rarely what a reader of "active in Q1" expects. Choose a snapshot when you
specifically want a point-in-time balance (month-end ARR, say); choose
`overlap` when you want activity. At day grain all three agree.

The same `match:` key, with the same three values and the same default, sits on
an interval import (below) — the two mechanisms answer the same question.
`tests/test_engine.py` pins the table above for both.

The timeline is **generated**, not read from a table: `start`/`end` name the
model's own interval columns, and there is nothing to create or maintain
alongside them. That also bounds what a spine can do — it produces bare dates
at the requested grain, so it carries no fiscal periods, holiday flags or
week numbers, and each model declares its own. For any of those, join a real
calendar table instead (next section).

### Calendar tables (`how: between`)

The other way to ask a point-in-time question is a **standalone date table**
joined on the interval rather than on a key. Declare the calendar as a dataset
in a dimension bundle — with **no join** to anything else, which is the whole
idea — and import it with `how: between`:

```yaml
# models/subscriptions.yaml
dimension_imports:
  - bundle: calendar
    anchor_dataset: days
    how: between
    left_on: [start_date, end_date]   # this model's interval; null end = still open
    right_on: date                    # the calendar's day column
```

Each model row is then counted in every period it was open for, and every
column of the calendar (`calendar_quarter`, `calendar_month_start`,
`calendar_day_of_week`, …) becomes an ordinary dimension — groupable,
filterable and cross-filterable like any other. Reach for this over a spine
when the periods need attributes, when several models must share one
definition of a period, or when the reporting window should be the calendar's
rather than whatever range the data happens to cover. See
`dimensions/calendar.yaml` and `models/subscriptions.yaml`, which declares both
mechanisms side by side.

**Grain is dynamic.** The table stores days, but the join is not a per-day
join: before joining, the engine narrows the date table to **one row per bucket
at the grain the query is asking for**. So a model row is counted once per
bucket, and an additive measure (`sum`, not just `count_distinct`) is correct at
every grain — change the builder's grain picker from Day to Quarter and the
numbers stay right. It matches a spine dimension bucket for bucket at `1d`,
`1w`, `1mo`, `1q` and `1y`; `tests/test_engine.py` asserts that for both an
additive measure and a distinct count.

The grain comes from whichever of the calendar's dimensions the query uses
(finest wins). A time dimension takes it from the grain picker. A column that
is *inherently* periodic declares its own, so it needs no picker at all:

```yaml
# dimensions/calendar.yaml
- name: calendar_quarter
  column: quarter
  grain: 1q          # this column is constant across a quarter
```

Undeclared means the table's own row grain, which is right for a plain day
column or a weekday flag. Those day-level attributes describe the one day that
represents each bucket, so they read meaningfully at day grain.

Which periods a row counts in is the same `match:` choice as a spine's —
`overlap` (default), `period_start` or `period_end`, described
[above](#what-active-in-this-period-means-match):

```yaml
    match: period_end    # month-end snapshot rather than "active during"
```

One refinement over the spine: a period's span is the days the date table
*actually holds* for it, not calendar arithmetic. So a table that starts
mid-month, or one listing only business days, reports the period it really
covers — an overlap against a business-day calendar ignores weekends.

Two more things:

- **The join is applied only to queries that use one of its dimensions.**
  Otherwise every measure on the model would silently multiply by the number of
  periods each row spans.
- A bundle may be imported more than once — once on a key for its reference
  data, once on a range for its calendar. `between` is a `dimension_imports`
  mode only; plain `joins:` still take `left`/`inner`.

### Common dimensional models (shared dimensions)

Some dimensions belong to more than one fact model — region, account,
product — and hand-copying the same join into every model that wants them
means one edit has to happen N times. A **dimension bundle**, one YAML file
per bundle in `dimensions/`, declares a set of reusable **datasets** (a
source plus dimensions, no measures) and the joins between them, once:

```yaml
# dimensions/geography.yaml
name: geography
label: Geography
datasets:
  - name: regions
    source: { format: csv, path: s3://cash-intel/ref/regions.csv }
    dimensions:
      - name: region
        geo: { lat: region_lat, lon: region_lon }
      - name: territory
    joins:                    # joins to another dataset *in this same bundle*
      - to: territories
        on: territory
  - name: territories
    source: { format: csv, path: s3://cash-intel/ref/territories.csv }
    dimensions:
      - name: territory_name
        column: name
```

A fact model imports a bundle by declaring an **anchor** — how its own
column maps to a key on one dataset in the bundle:

```yaml
# models/sales.yaml
dimension_imports:
  - bundle: geography
    anchor_dataset: regions
    on: region             # sales.region = geography.regions.region
    # datasets: [regions]  # optional — omit for the whole bundle (default)
```

By default the *whole* bundle becomes available, including datasets only
reachable through the bundle's own internal joins — importing `regions`
above also pulls in `territory_name` from `territories`, with no separate
declaration. Reachability cuts both ways, so "the whole bundle" never includes
a dataset the bundle's joins don't connect to the anchor. Naming such a dataset
explicitly under `datasets:` is a load-time error rather than a quiet omission
— the failure mode otherwise being an import that reads correctly in the YAML
while its dimensions never show up in the builder. Give it a join, or import it
as its own entry: a disconnected calendar table anchors itself, see
[`how: between`](#calendar-tables-how-between). Imported dimensions
behave exactly like native ones everywhere
(builder, filters, dashboards, cross-filtering by name); a same-named
dimension declared natively on the fact model always wins over an imported
one. The bundle arrives with every dimension already under its *dimension*
name rather than the bundle's own column name, so a bundle is free to have a
`month` column of its own even when the importing model does too.

**A `how: left` import is only joined into queries that read one of its
dimensions.** It can only add columns, so a query using none of them gets the
same answer without paying for it — which is what lets a model import a
calendar purely to [conform its fact
tables](#several-fact-tables-in-one-model) without
slowing down every query that has nothing to do with dates. `how: inner` also
*filters* the model's rows, so it is always applied. (The corollary, shared
with [`how: between`](#calendar-tables-how-between): a measure expression can
reference dimensions, and one referencing an imported dimension needs that
dimension in the query too.) See `dimensions/geography.yaml`, imported by both `models/sales.yaml`
and `models/logistics.yaml`, for a working example — editing the bundle
updates both models with no changes to either model file.

**Or author it in the app**: the **Modelling** workspace lists every common
model (**+ COMMON MODEL**, or click one to edit) and opens the same
live-validating YAML editor the fact-model editor uses — with per-dataset
source-column introspection. And while editing a fact model, the editor's
*Common Dimensions* panel lists every bundle and its datasets; clicking one
inserts a ready-to-go `dimension_imports` block (whose `on:` key gets
column-name intellisense). Common dimensional models never appear
in the builder's model picker — they provide dimensions, they aren't queried
directly — and one that's currently imported can't be deleted until its
importers drop it. Endpoints mirror the model API under `/api/dimensions`
(list, validate, create, `{name}/yaml` GET/PUT, delete, reload).

### Several fact tables in one model

A model's datasets do not have to be related to each other.

`source:` + `joins:` describes one fact table and its lookups. The general
shape is `datasets:` — every table the model reads, plus the relations between
them — and **each connected group of them is a fact table in its own right**:

```yaml
# models/commercial_overview.yaml (abridged)
name: commercial_overview
label: Commercial Overview

datasets:
  - name: orders
    source: { format: parquet, path: s3://cash-intel/sales/*.parquet }
    dimensions: [{ name: region }, { name: channel }]
    measures:   [{ name: revenue, expr: sum(unit_price * quantity) }]

  - name: spend                       # related to nothing above it
    source: { format: parquet, path: s3://cash-intel/marketing/*.parquet }
    dimensions: [{ name: region }, { name: channel }]
    measures:   [{ name: ad_spend, expr: sum(spend) }]

dimension_imports:                    # ...but both related to one calendar
  - bundle: calendar
    from_dataset: orders
    anchor_dataset: days
    left_on: order_date
    right_on: date
  - bundle: calendar
    from_dataset: spend
    anchor_dataset: days
    on: month                         # (right_on: date on the bundle side)
```

```
calendar_date  ad_spend      revenue
2025-01-01     241,880.55    3,918,204.10
2025-02-01     238,014.02    3,655,901.44
```

`orders` and `spend` share no key and are never joined. Each is scanned on its
own at the grain the query asked for, and the per-table *results* are merged on
the dimensions they share. This is the only shape that works: joining the two
would pair every order with every spend row for its month and inflate both
sides. Running them separately and merging the aggregates leaves every measure
at the grain of its own table, so asking for one measure or five returns
identical numbers for each.

`source:`/`joins:` is exactly this with one dataset (named after the model) and
one relation per join entry — there is no second concept, and a model written
either way parses into the same datasets, relations and fact tables.

**A relation is what merges two datasets into one fact table.** Declare
`joins: [{to: products, on: product}]` on `orders` and the two become one
scan, with `products`' columns available to `orders`' dimensions and measures.
Leave them unrelated and they stay separate. Nothing is implicit: the shape of
the relation graph *is* the shape of the model.

**Conformance is declared per fact table.** Two of them share a dimension when
they call it by the same name — either because both declare one (`region`
above), or because both import the same [common dimensional
model](#common-dimensional-models-shared-dimensions), which is what
`from_dataset:` on an import is for: it says which fact table the bundle
relates to, and importing one bundle into two of them is what puts them on a
single axis. How each gets there is its own business, and the three tables in
`models/commercial_overview.yaml` use three different mechanisms without
caring:

| dataset | import | how it gets there |
| --- | --- | --- |
| `orders` | `left_on: order_date` | an event date, one row per order |
| `spend` | `on: month` | already monthly, lands on each month start |
| `subs` | [`how: between`](#calendar-tables-how-between) | an interval join — a subscription counts in every period it was open for |

A spine dimension works here too. Whatever a fact table does to answer "group
me by time", it does before the merge.

**Only dimensions every fact table offers are groupable** — the intersection,
not the union. If `channel` is on orders and spend but not subscriptions,
there is no honest subscriptions number to put on a row labelled "net ads".

**That intersection follows the query, not the model.** Only the fact tables a
query names a measure from are read, so only those have to conform. Asking for
`revenue` and `ad_spend` *can* be grouped by `channel`, because subscriptions
contributes no row to that result:

```jsonc
{"dimensions": ["channel"], "measures": ["revenue", "ad_spend"]}   // fine
{"dimensions": ["channel"], "measures": ["revenue", "mrr"]}        // refused
```

So one model covers every combination of its fact tables, rather than needing
one per pair. A query naming a single table's measures conforms with itself and
can be grouped by anything that table has. Grouping or filtering by a dimension
one of the *read* tables lacks is a query error that names the table
responsible — the actionable part being which measure to drop.

`model.dimensions` — what the builder opens on, and what `/api/models` reports
— stays the all-tables intersection: the catalog that is safe whatever you go
on to ask for.

**Measures keep their own names** (`revenue`, `ad_spend`) and have to be unique
across the whole model — a query names a measure without saying which table it
came from. Dimension names are the opposite: repeating one across fact tables
is how they conform, and only a repeat *within* one fact table is a clash.

Other things worth knowing:

- A bucket only one fact table has rows for keeps its row and leaves the
  others null — "this table has nothing here", which is what happened. Charts
  draw a gap; zero would be a number nobody measured.
- Only the fact tables a query names a measure from are read at all. A query
  asking for `revenue` alone never touches the other files.
- Sorting and the row limit apply *after* the merge, so a limit can't drop a
  bucket another table still has rows for.
- A dataset related to nothing else *and* declaring no measures is a load-time
  error: it is a fact table with nothing to measure, and its only effect would
  be to narrow what the model's real fact tables can be grouped by. Relate it,
  or give it a measure.
- Two fact tables that disagree on a dimension's *type* — one calling `when` a
  time dimension, the other a category — is a load-time error, checked over
  every pair rather than only over the all-tables intersection.
- Inline (visual-scoped) measures need one frame to evaluate against, so a
  model with several fact tables doesn't take them; declare the measure on the
  dataset it belongs to.

**In the app**: the model form is the same general design as the common-model
form — step one (**DATASETS**) adds the tables and imports any common models,
step two (**RELATIONS**) says how they relate. RELATIONS names the fact tables
the current relations add up to, and lists what all of them can be grouped by,
so "these two aren't related to each other" is a visible choice rather than a
silent one. Relating one common model to a *second* fact table — the move that
conforms them — is done there too, by picking the common model and the dataset
it hangs off; it is a relation, not a second import. Dimensions and measures
are then declared per dataset, which is
what scopes a measure to one fact table. The list marks a model holding several
of them. Everywhere else it behaves like any other model — Studio, dashboards,
cross-filtering, Chat.

> Replaces the earlier `facts:` shape, in which a model listed *other models*
> as its fact tables. A `facts:` key is now a load-time error naming this
> section: the same analysis is one model with several datasets, authored in
> the form rather than by hand.

### Performance (13M-row fact table)

`python -m app.load_taxi` downloads 4 months of the public NYC TLC yellow-taxi
data (~13.1M rows, 209MB parquet) into `data_cache/`; on restart it is seeded
into the emulator and queryable as the `taxi` model. Measured through the full
stack (HTTP → semantic layer → polars lazy scan over emulated S3, x86 MacBook):

| query | rows out | cold | warm |
|---|---|---|---|
| grand totals (trips, revenue, tip %) | 1 | 679ms | 471ms |
| monthly trend (trips, revenue) | 9 | 2.5s | 2.1s |
| avg fare by payment type | 6 | 591ms | 464ms |
| daily trend, filtered to 2 weeks | 17 | 932ms | 933ms |

Predicate/projection pushdown does the heavy lifting: only referenced columns'
row groups leave the bucket. Against real S3, network latency dominates —
expect these numbers to grow with round-trips, not data size.

**Or author it in the app**: the **Modelling** workspace (see below) is the
home for model authoring — *edit yaml* on any model card, or *+ MODEL* — opening
a model editor with live validation (parse + measure-expression check on every
keystroke, debounced) and a source-column panel that introspects the scan —
including joined columns — with click-to-insert. Three delight affordances make
authoring less of a memory test:

- **◇ DATASET** browses the bucket as prefix-grouped datasets (drillable to a
  single object) and fills in the `source:` block for you — no hand-typed
  `s3://…` paths. Once a source is picked, its real columns light up the palette.
- **Intellisense anywhere in the YAML**: inside a measure `expr:` you get polars
  completion (`pl.`, `.`, `pl.col("` → real columns); in a dimension/join/key
  context you get bare column-name completion. Same engine as the measure lab.
- **Unsaved edits are guarded** — navigating away warns before discarding, and
  nothing is written to `models/` until you save.

Saving writes the YAML back to `models/`, hot-reloads the semantic layer, and
re-syncs the query builder. Plain-text YAML editing stays fully first-class —
the affordances only insert/patch the one document.

### The measure lab

*+ new measure* under the builder's measure list opens an inline editor on the
visual itself. Type in the safe DSL — a bare identifier offers function names,
source columns, *and* sibling measures (model measures plus this visual's
other inline measures, since a bare name inside `running_total()`/`lag()`
means a measure, not a column, and the client can't know which mode an
expression is in until it parses); `col("` offers the source's columns
(post-join, with dtypes); `param('` offers this visual's declared
parameters, each hinting its type, values, and default (legal anywhere a
literal is legal in the DSL — comparisons, `if_()`, `coalesce()`,
`where()`, `cast()`'s value argument, `lag()`'s periods argument). Every
keystroke re-runs the current query with the
draft measure so it renders live in the chart (with the value shown
directly when there are no dimensions). Two save paths:

- **SAVE TO VISUAL** — the measure travels inside the visual's spec
  (`inline_measures` on the query), works on dashboards and in focus mode, and
  shows as a dashed *visual* chip with edit/remove. No credentials needed —
  it's compiled through the same safe DSL as everything else, so there's
  nothing dangerous for an unauthenticated visual author to run.
- **SAVE TO MODEL** — appends the measure to the model's yaml
  (comment-preserving) and hot-reloads, promoting it to a shared model
  measure. This is an authoring action: the browser prompts once per tab for
  an API key and your name, which travel as `X-API-Key`/`X-Author` headers.
  Disabled whenever the draft references a `param(...)` — see "Visual
  parameters" above — since a shared model measure has no visual to be
  scoped to.

The **+ param** picker next to the format selector inserts `param('name')`
for any parameter declared on the current visual (see the Parameters
section in the sidebar) at the cursor.

> Inline measures are compiled through an allowlisting AST compiler
> (`app/measure_dsl.py`) that never calls `eval`/`exec`/`compile` — see "The
> safe measure DSL" above. Saved model measures compile through the same
> allowlist; the one exception is the `frame:` construct, which stays
> `eval`/`exec`-based (like model YAML always has been) but is reachable only
> through the authenticated save path below, never inline.

### Authoring model measures (auth + provenance)

Creating, updating, or deleting a saved model measure requires a signed-in
account with the **author** role (a `frame:` measure escalates to **admin**
— see the carve-out above). Identity comes from the session; in the app you
just click SAVE TO MODEL. From a script, create a **personal access token**
under ACCOUNT and send it as `Authorization: Bearer cipat_…` — it acts as
you, with your role, and can be revoked individually. The spec-008
`X-API-Key`/`X-Author` headers are retired; requests presenting only them
get 401.

| Route | Min role | What it does |
|---|---|---|
| `POST /api/models/{m}/measures` | author | create a measure (validated, then appended to the yaml) |
| `PUT /api/models/{m}/measures/{name}` | author | update a measure in place |
| `DELETE /api/models/{m}/measures/{name}` | author | remove a measure |
| same, with a `frame:`/`frame_emits` payload | **admin** | the eval-based carve-out stays behind the highest trust level |
| `GET /api/models/{m}/measures/{name}/history` | viewer | append-only provenance: author, version, expression snapshot per save |

Provenance now records the **verified account** (display name + user id) —
rows written before the auth feature keep their self-declared label and are
flagged `verified: false` ("legacy") in history responses and the measure
lab's history strip.

Every create/update is validated (the safe DSL, or `validate_frame` for a
`frame:` measure) before anything is written — an invalid measure is refused,
never partially saved. Provenance is recorded in a separate SQLite table
(`measure_provenance`, in `cash_intel.db`) alongside the yaml write; the yaml
file remains the sole executable source of truth, the table is the audit log.

### Locked (built-in) vs. local models

The 7 models under `models/` and both bundles under `dimensions/`
(`geography.yaml`, `calendar.yaml`) are the built-in demo catalog — curated to
be the minimal set that exercises every core-engine capability (a lazy read of
each supported format, a shared dimension bundle, models with one and with
several fact tables, a `frame:` expression, point-in-time range joins) plus one large fact
table for a performance benchmark. `GET /api/models` and `GET /api/dimensions`
report each one `"locked": true`. Structural changes — `POST /api/models`/
`POST /api/dimensions` under an existing name, `PUT /api/models/{m}/yaml`/`PUT
/api/dimensions/{b}/yaml`, `DELETE /api/models/{m}`/`DELETE
/api/dimensions/{b}` — 403 on anything locked, even for admin; the catalog
only changes by editing a file and committing it. On a model the lock is
structural only: measure-lab edits (create/update/delete a measure) still work
on a locked model exactly as documented above. A bundle has no such
non-structural surface, so a locked bundle's yaml can't be touched at all
through the API — this is what stops a "quick fix" to the shared Calendar or
Geography bundle from silently detaching it from every model built on it.

Any *new* model or bundle created through the app — `POST /api/models`/`POST
/api/dimensions`, or either guided form's generate-then-save flow — is
**local** instead: its yaml lives in `cash_intel.db`
(`app/localmodelstore.py` / `app/localbundlestore.py`, same gitignored SQLite
file as visuals/dashboards/pipeline runs), never as a file under `models/` or
`dimensions/`. It reports `"locked": false`, is freely
renamable/editable/deletable through the API, and survives a restart (it's a
real row in a real database) without ever becoming something `git status`
notices. A model's DELETE is a click away in the UI: a **✕** next to it in
the Models list, or **DELETE MODEL** in its guided form — both hidden for a
locked (built-in) model, since that request would only ever 403.

Build one over your own data with the Modelling landing page's **UPLOAD A
DATASET** control (or the same control inside a model form's source picker)
— not tied to building any particular model, so you can stage several files
before deciding what to do with any of them. Pick several files at once, or
**OR PICK A FOLDER** to upload a whole directory in one go — a folder's own
structure survives as-is under `local/<name>/` (two `2024/jan.csv` /
`2025/jan.csv` files don't collide the way flattening them would). `POST
/api/datasets/local` (author role; multipart `name` + one or more `files`,
each named with its path relative to the upload) drops them into the bucket
under `local/<name>/…`, unmodeled, where they show up in `GET /api/datasets`
exactly like a `raw_data/` file — a file with an unrecognized extension is
skipped rather than failing the whole batch (400 only if nothing in it was
usable) — then open the Modelling workspace's source picker and build a
model on it from scratch. `DELETE /api/datasets/local/{name}` removes every
object under that prefix again.

**Persistence**: an upload is also cached to local disk under
`config.LOCAL_DATA_DIR` (`local_data/<name>/<filename>` by default —
gitignored, override with `CI_LOCAL_DATA_DIR`). This matters because the
default embedded S3 emulator is in-memory — its bucket is entirely rebuilt
from scratch (`app/seed.py`) on every process start, so anything written only
to the bucket (an upload included) would otherwise vanish the moment the app
restarts. `app/seed.py`'s `_upload_local_data` re-uploads everything under
`local_data/` alongside the generated demo data and `raw_data/`, so an upload
survives a restart the same way `app/load_taxi.py`'s `data_cache/` does.
Point `CI_S3_ENDPOINT` at a real bucket (MinIO, real S3 — see `docker-compose
--profile minio`) and this becomes moot: the bucket itself persists, so the
disk cache is just a backup copy.

## API

Every route requires a signed-in identity — a session cookie from
`POST /api/auth/login`, or `Authorization: Bearer <token>` — except login
itself and `GET /api/health`. Cookie-authenticated mutations must also send
`X-Requested-With: fetch` (CSRF gate; bearer requests are exempt). Reads
need any role; the role column below is for mutations. The full
route-by-route matrix lives in
`specs/011-session-auth-rbac/contracts/auth-api.md` and is enforced by
`tests/test_role_matrix.py`.

| Route | What it does |
|---|---|
| `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/password` | session lifecycle (login is the only public route besides `/api/health`) |
| `GET/POST /api/users`, `PATCH /api/users/{id}` | **admin**: create accounts, change roles, deactivate/reactivate, reset passwords — no self-signup, no hard delete |
| `GET/POST /api/tokens`, `DELETE /api/tokens/{id}` | your personal access tokens — the secret is shown once at creation |
| `GET /api/models` | models with their dimensions + measures |
| `POST /api/models/reload` | re-read `models/*.yaml` |
| `GET /api/models/{m}/dimensions/{d}/values` | distinct values (filter pickers) |
| `GET/PUT /api/models/{m}/yaml` | read / save a model's YAML (save validates + hot-reloads) |
| `POST /api/models/validate` | parse-check YAML + introspect source columns |
| `POST /api/models`, `DELETE /api/models/{m}` | create a local model / delete one — 403 on a locked (built-in) model, see "Locked vs. local models" above |
| `GET /api/datasets` | bucket objects grouped into pickable datasets (source picker) |
| `POST /api/datasets/local`, `DELETE /api/datasets/local/{name}` | **author**: upload/remove a `.csv`/`.parquet` under `local/{name}/` — a source for a new local model |
| `POST/PUT/DELETE /api/models/{m}/measures[/{name}]` | create/update/delete a model measure (**author** role; `frame:` payloads **admin** — see "Authoring model measures" above) |
| `GET /api/models/{m}/measures/{name}/history` | append-only provenance for a saved measure |
| `POST /api/query` | run a semantic query, returns columns + rows + timing |
| `POST /api/query/extract` | the same query, same model resolution and same role check, answered as an Arrow IPC stream a dashboard tile can re-aggregate in the browser (metadata rides on `X-Extract-Meta`). Two extra fields: `cross_dimensions` (what other tiles display, so a cross-filter lands locally) and `interactive_filters` (filter fields whose values may change, carried as columns rather than pushed down). Answers 200 with a small `{"fallback": …}` JSON instead when the tile isn't eligible or trips the size cap — declining is routine, not an error. See "Instant mode" below |
| `GET/POST /api/visuals`, `PUT/DELETE /api/visuals/{id}` | saved visuals (SQLite: `cash_intel.db`) |
| `GET/POST /api/dashboards`, `GET/PUT/DELETE /api/dashboards/{id}` | dashboards — ordered tiles `{visual_id, w:1\|2}`; GET by id resolves tile visuals; create/update reject a tile set where two visuals declare a same-named, differently-defined parameter (see "Visual parameters" above) |
| `GET/POST /api/conversations`, `GET/PATCH/DELETE /api/conversations/{id}`, `POST /api/conversations/{id}/ask` | conversational analytics (SQLite: `cash_intel.db`) — strictly owner-scoped; 503 unless `CI_LLM_API_KEY` is set (see "Conversational analytics" below) |
| `GET/POST /api/notebooks`, `GET/PUT/DELETE /api/notebooks/{id}` | notebooks — freeform HTML narrative pages with live visuals/dashboards embedded (mutations **author**; see "Notebooks & the Composer" below) |
| `GET /api/composer/context`, `POST /api/composer/compose/stream` | the Composer — LLM-drafted notebook pages, SSE-streamed and sanitized server-side (**author**; 503 unless `CI_LLM_API_KEY` is set) |
| `GET /api/models/{m}/memories`, `POST/PATCH/DELETE /api/models/{m}/memories[/{id}]` | chat-learned model memories (synonyms/notes the assistant records against a model) — reads any role, mutations **admin** (see "Conversational analytics" below) |
| `GET /api/pipelines` | pipelines with materialization config + latest-run summary |
| `POST /api/pipelines/validate` | parse-check pipeline YAML (never executes the script) |
| `POST /api/pipelines/reload` | re-read `pipelines/*.yaml` (**admin**) |
| `GET/PUT /api/pipelines/{p}/yaml` | read / save a pipeline's YAML (PUT **admin**; name immutable — 400 on rename) |
| `POST /api/pipelines`, `DELETE /api/pipelines/{p}` | create a pipeline file / delete one (**admin**; delete 409s while a run is pending, marks the target model's lineage section orphaned) |
| `POST /api/pipelines/{p}/run` | trigger a run (**admin**; 202, 409 if this pipeline already has one pending) |
| `GET /api/pipelines/{p}/runs`, `GET /api/runs/{id}` | run history / a single run's full record |
| `GET /api/pipelines/{p}/lineage/suggest` | pass-through lineage suggestions by name-matching the output schema against declared sources (409 if no schema is available yet) |
| `GET /api/lineage/layers`, `PUT /api/lineage/layers` | the ordered layer list (PUT **admin**; write `pipelines/layers.yaml`; 409 if removing a layer still referenced by a pipeline) |
| `GET /api/lineage/graph` | the lineage graph payload (nodes/edges/field-hops/layers) — see "Pipelines" above |
| `GET /api/sandbox/notebooks`, `GET /api/sandbox/notebooks/{id}` | list / fetch a saved sandbox notebook — reads any role |
| `POST/PUT/DELETE /api/sandbox/notebooks[/{id}]` | create/update/delete a sandbox notebook (**admin**; see "Sandbox notebooks" below) |
| `POST /api/sandbox/run` | execute cells 0..`run_upto` of a (saved or unsaved) notebook and return each cell's output (**admin**) |
| `POST /api/sandbox/convert` | text-only transform: detect a notebook's `read(...)` bucket calls and render a starter pipeline yaml (**admin**; never executes anything). `with_lineage: true` additionally asks the coding agent for the pipeline's description + field lineage |
| `POST /api/sandbox/agent/stream` | the sandbox coding agent: proposes cells for the open notebook, streamed as SSE (**admin**; 503 unless `CI_LLM_API_KEY` is set — see "The coding agent" below) |

Query shape:

```json
{
  "model": "sales",
  "dimensions": [{"name": "order_date", "grain": "1mo"}, "region"],
  "measures": ["revenue", "margin_pct"],
  "filters": [{"field": "segment", "op": "in", "values": ["corpo", "solo"]}],
  "sort": {"by": "revenue", "desc": true},
  "limit": 1000,
  "parameters": [
    {"name": "period_list", "type": "int", "values": [1, 2, 3, 4], "default": 1},
    {"name": "threshold", "type": "float", "values": [10, 50.5, 100], "default": 50.5}
  ],
  "parameter_values": {"period_list": 2, "threshold": 100}
}
```

Filter ops: `eq ne gt gte lt lte in not_in contains`. `parameters` declares
a visual's parameters (travels with the query the same way `inline_measures`
does) — `type` is `int`/`float`/`string`, omitted meaning `int`; `parameter_values`
is the caller's current pick per parameter — missing a name falls back to
that parameter's own default, and any value outside its declared list (or
of the wrong type) rejects the whole query before anything runs. A
dashboard view's saved `parameters: {name: value}` map (alongside its
`filters`) is what a dashboard tile's query pulls this from.

## Studio, Modelling, Portal, Account

The header nav splits the app into four surfaces (what each shows follows
your role — viewers see no authoring controls at all):

- **STUDIO** — the query builder: pick a model, add dimensions/measures/filters,
  chart it, save visuals, and edit dashboards. Model *authoring* no longer lives
  here — Studio is for building visuals against whatever models exist.
- **PORTAL** — the consumption view. From a dashboard's toolbar in the studio,
  **PUBLISH** puts it in the portal under a slash-separated folder path
  (`ops/street` nests folders automatically; republish to move it, ✕ next to
  the live badge to unpublish). Portal users navigate the folder tree and open
  dashboards read-only: they can switch saved views, override the grain,
  cross-filter, and expand tiles — but nothing they do edits or persists
  anything (view switches in the portal don't even save the selection).
- **MODELLING** — the home for the semantic layer (formerly "Data"). A left
  rail manages every fact model, common model, and pipeline — *edit yaml*,
  *build ►*, and *+ MODEL* / *+ COMMON MODEL* / *+ PIPELINE* — and the right
  pane is the data overview: every object in the bucket with size and modified
  date, matched against each model's source and join globs (Delta/Iceberg
  table internals map to their model too). Clicking a model chip jumps to it in the
  builder; files no model reads are flagged as unmapped. This is where
  authoring — the dataset picker, guided common-model import, expression
  intellisense, and pipeline authoring described below — lives, along with
  **◈ LINEAGE** (the lineage graph) and **▤ LAYERS** (the optional bronze/
  silver/gold layer list — see [Pipelines](#pipelines) below).
- **ACCOUNT** — self-service for every signed-in role: personal access
  tokens, password change, and (see [Themes](#themes)) picking one of the 4
  visual themes. Admins additionally get user management here.
- **SANDBOX** — scratch polars/python notebooks over the bucket, with a
  path into a saved pipeline once a script is worth keeping — see
  [Sandbox notebooks](#sandbox-notebooks) below.

## Pipelines

A **pipeline** (specs/014-polars-pipeline-module/) hosts a real polars
transformation script — not a low-code builder, an actual `.py`-shaped
snippet the platform runs, materializes, and documents. A script's whole
contract is to produce a variable named `output`; the platform performs
every write, which is what makes the materialization modes below
enforceable and a failed run non-corrupting.

```yaml
# pipelines/silver_orders.yaml
name: silver_orders
sources:
  - name: sales
    format: parquet                # parquet | csv | delta | iceberg
    path: s3://cash-intel/sales/*.parquet
    layer: bronze                  # optional — see Layers below
target:
  path: s3://cash-intel/silver/orders
  format: delta                    # delta (default, required for upsert) | parquet (replace only)
  layer: silver
materialization:
  mode: upsert                     # replace | upsert
  keys: [order_id]                 # upsert: required
  on_delete: soft_delete           # ignore (default) | sync | soft_delete | predicate
  soft_delete_column: is_deleted   # soft_delete: required
timeout_seconds: 120               # default 600, max 3600
script: |                          # sees `sources` (dict of source name -> LazyFrame) and `pl`
  output = (
      sources["sales"]
      .with_columns(((pl.col("unit_price") - pl.col("unit_cost")) * pl.col("quantity")).alias("net_revenue"))
      .select(["order_id", "order_date", "region", "channel", "category", "net_revenue"])
  )
lineage:                           # optional — documents transformation logic on the target model
  - field: order_id
    from: [sales.order_id]
    transform: pass-through
  - field: net_revenue
    from: [sales.unit_price, sales.unit_cost, sales.quantity]
    transform: "(unit_price - unit_cost) * quantity"
```

Iceberg is a read-only source format for now (no `target: {format: iceberg}`)
— writing a new Iceberg table needs a catalog to allocate its
location/schema/snapshot atomically, which only the seeded demo data uses,
once, at startup (a throwaway in-memory catalog — see app/iceberg_util.py);
nothing at query time depends on a catalog existing. Reading one needs no
catalog at all: like a Delta table, an Iceberg source's path is the
table's root directory — the current snapshot is found by listing its
`metadata/` folder for the highest-versioned `*.metadata.json` file, the
same self-describing-directory convention Delta's `_delta_log` already
gets.

**Trust model**: a pipeline script is real, unsandboxed Python at
application-code trust — the same posture as a model measure's `frame:`
carve-out (see "Measures over an intermediary frame" above), just with a
whole script instead of one derived frame. Creating, editing, deleting, and
**running** a pipeline all require the **admin** role (Principle VI
re-opened for this feature — see `.specify/memory/constitution.md`); every
mutation and every run is written to the audit log. Every role can read
pipeline definitions, run history, and the lineage graph.

**Execution**: manual trigger only (no scheduler) — `▶ RUN` in the pipeline
editor, or `POST /api/pipelines/{name}/run`. Each run executes in its own
subprocess, supervised by a single FIFO worker thread: runs are strictly
serialized platform-wide (triggering a second pipeline while one is running
queues it; triggering the *same* pipeline again while it has a pending run
is refused with 409), and the parent enforces `timeout_seconds` by killing
the process outright — a runaway or crashed script can never take the app
down. An app restart mid-run marks that run `interrupted`, never stuck.

### Materialization

- **`replace`** — the target is atomically overwritten (a single Delta
  transaction, or one `PUT` for a parquet target): readers see the old data
  or the new data, never a partial write.
- **`upsert`** (Delta targets only) — the run's output is merged into the
  target by `keys`: matched rows update, unmatched rows insert. Four
  `on_delete` policies handle rows in the target that the output no longer
  contains:

  | Policy | Behavior |
  |---|---|
  | `ignore` (default) | left alone |
  | `sync` | deleted (an **empty** output + `sync` halts the run unless `allow_empty_sync: true` is also set — it would otherwise delete everything) |
  | `soft_delete` | flagged `true` in `soft_delete_column` (cleared back to `false` if the key reappears in a later run) |
  | `predicate` | rows matching `delete_predicate` (a Delta SQL predicate) are deleted before the merge |

  Guards run before any write, so a rejected run never touches the target:
  null/duplicate key values in the output, or an output schema incompatible
  with the existing target (a diff naming the missing/extra/mismatched
  columns). A soft-delete pipeline's flag column is only ever added
  automatically on a target's *first* upsert run — retrofitting
  `soft_delete` onto a target created some other way needs one `replace`
  run first, to introduce the column cleanly.

### Traceability: layers and lineage

Datasets can optionally be grouped into named, ordered **layers**
(bronze/silver/gold, or any naming a deployment prefers) via **▤ LAYERS** in
Modelling (writes `pipelines/layers.yaml`); a pipeline tags its own
sources/target with `layer:`. Layers are purely organizational — everything
works with none declared (FR-020).

Field-level **lineage** — which source field(s) a target field derives
from, plus a human-readable transform description — is declared per
pipeline (optional per field; **SUGGEST PASS-THROUGH** in the pipeline
editor proposes matches by name, never auto-saved). On every successful
run, declarations are validated against the run's *real* output schema — a
declared field the output no longer has, or an output field nobody
declared, is flagged on the run without blocking the write — and, when a
loaded model scans the pipeline's target, the validated lineage is
regenerated into a dedicated `pipeline_lineage:` section of that model's
yaml:

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

The section is entirely pipeline-owned — regenerated idempotently on every
run, appended if absent — and every byte of the model's yaml outside it is
preserved untouched. Deleting the owning pipeline marks the section
`orphaned: true` rather than removing it.

### Lineage graph

**MODELLING → ◈ LINEAGE** renders a read-only, hand-rolled SVG graph:
datasets/models as nodes (columns by declared layer, or topological rank
when no layers exist), pipelines as edges colored by their latest run
status. Clicking a node opens its detail (a link to its model, if any, and
its known fields); clicking a field traces its declared lineage upstream
across every hop, highlighting the full chain. The graph tolerates cycles
by construction (each edge comes from one pipeline's own source/target
declaration, never a recursive walk) and needs no live bucket scan, so it
stays fast regardless of graph shape.

The repo doesn't ship a bundled example chain — `pipelines/` starts empty.
Build one from **MODELLING → PIPELINES** over the seeded sales/marketing/etc.
data to see materialization, lineage, run history, and the graph populate
live. (Run history lives in `cash_intel.db`, not the repo, so a fresh clone
always starts with no pipelines defined and no runs — that first click is
the point: it's the same "queued → running → succeeded" flow a real
pipeline goes through.)

## Sandbox notebooks

The **SANDBOX** surface is a multi-cell polars/python scratch notebook over
the same bucket pipelines read from — the place to explore a dataset or
prototype a transformation *before* it's worth saving as a pipeline. Cells
run top-to-bottom in one shared namespace (`pl`, `bucket`, and a `read(path[,
format])` helper that infers parquet/csv/delta from the extension — iceberg
needs an explicit `format="iceberg"`, since its table root looks the same as
delta's on disk) — later cells see earlier cells' variables, and a cell's last bare expression
auto-displays, Jupyter-style: a polars `DataFrame`/`LazyFrame` renders as a
table (capped preview, lazily collected), anything else as its `repr()`.
**RUN** on a cell replays every cell from the top through that one; there is
no persistent kernel between separate runs — each run recomputes its whole
prefix from scratch, trading a little redundant work for never having
stale/drifted state to reason about.

The editor gets the same delight affordances as the model/pipeline yaml
editor: syntax highlighting (`static/js/pyhighlight.js`, the python sibling
of `yamlhighlight.js`) and intellisense — `read("` offers real bucket paths
(clicking a file under **Bucket Files** in the side panel inserts one at the
cursor too), `pl.` offers common polars constructors/scan functions, and a
bare name offers every variable assigned in any cell. Notebooks persist as
`{name, cells}` in SQLite (`sandbox_notebooks` — no execution state is ever
saved, only the code); **+ CELL**, the per-cell ▶/↑/↓/+/✕ controls, and
**SAVE**/**SAVE AS NEW**/**DELETE** round out authoring.

### The coding agent

**◈ AGENT** (admin-only, and only when `CI_LLM_API_KEY` is configured —
the same key conversational analytics uses) opens a panel that writes polars
*for the notebook that's open*. It sees the live, unsaved notebook: every
cell's source, the last run's stdout/traceback tails, each result's **schema**
(column names + dtypes — result rows are never sent), and the bucket's paths
collapsed to things a `read(...)` call can name. A reply is a set of proposed
cells you **APPLY** or **APPLY + RUN**, never something applied, run or saved
on your own behalf — and a cell that failed gets a **◈ FIX WITH AGENT**
button that hands the error straight back.

It's tuned for a fast interactive loop rather than for autonomy, because a
sandbox already has the fastest feedback channel there is — run the cell:

- **one model call per request**, no tool-result loop and no self-critique pass;
- **no tests, benchmarks, try/except scaffolding or logging** — a hard rule in
  the system prompt, since that's where a coding agent's tokens and latency
  usually go. A failing cell's error is simply context for the next request;
- **no extended thinking**, a **cached system prompt** (the polars performance
  doctrine is long, static and resent every turn), and a **bounded context**
  (per-cell source, output tails and the file listing are all capped —
  `app/config.py`'s `SANDBOX_AGENT_*`);
- **model per request**: the panel's dropdown picks from the same
  `LLM_MODEL_CHOICES` chat uses; `CI_SANDBOX_AGENT_MODEL` sets the default.

The prompt is a polars performance brief, not a generic coding one: stay lazy
end to end, filter/project on the scan so pushdown drops row groups before
they leave the bucket, expressions never Python (`map_elements`, `iter_rows`
and friends are out), batch expressions into one `with_columns`, semi/anti
joins for filtering, `pl.len()`, streaming collect for larger-than-memory
work. Whatever comes back is re-validated before you can apply it
(`app/sandbox.py`): a target naming a cell that isn't in the notebook is
downgraded to a new cell, duplicates are dropped, and a syntax error is
*reported on the proposal* rather than silently discarded.

**Convert to pipeline**: once a script is worth keeping, **→ CONVERT TO
PIPELINE** combines the notebook's cells, detects every `read("path"[,
format=...])` call as a would-be pipeline source (name derived from the
path — a glob like `sales/*.parquet` names itself `sales`, not a generic
placeholder), rewrites those call sites to the pipeline script convention
(`sources["name"]`), and opens the result as an unsaved draft in the
Modelling pipeline editor with a starter `target:`/`materialization:` the
admin fills in and reviews before saving — this is a text-transform assist,
never a silent one-click pipeline; a script with no `output = ...`
assignment (the pipeline contract) surfaces a warning rather than guessing
one. `app/sandbox.py`'s detection ignores anything mentioned only in a
comment, so an explanatory `# e.g. read("s3://...")` note is never mistaken
for a real source.

**→ CONVERT + LINEAGE** (shown only when the agent is configured) does the
same conversion and additionally asks the agent for the one part a text
transform can't derive: the pipeline's `description:` and its **field-level
`lineage:`** — per output field, which declared source columns it came from
and a one-line plain-English derivation. Conversion itself stays free and
offline; this is the opt-in call. The generated section is re-validated
before it's rendered (`sandbox.validate_lineage`): a field the last run's
output schema doesn't contain is dropped, so is a `from:` ref naming
anything that isn't a declared source (a pipeline citing an unknown source
wouldn't load at all), and every drop comes back as a warning. Run the
notebook first for the grounded version — without a run there are no output
columns to check field names against, and the response says so. If the API
call fails you still get the ordinary conversion plus a warning; a flaky
model call never costs you the converted notebook. It runs on
`CI_SANDBOX_LINEAGE_MODEL` (Haiku by default — mechanical summarization of a
script the platform already parsed, so it doesn't need the coding model).

**Trust model**: identical carve-out to a pipeline's `script:` (Principle
VI) — real, unsandboxed Python at application-code trust, not a new
eval-capable construct, just the existing pipeline-script trust boundary
applied to throwaway exploratory code. Authoring, running, and converting
all require the **admin** role; every role can browse saved notebooks
read-only. Each run executes in its own subprocess
(`app/sandbox_runner.py`) with a hard, killable timeout
(`CI`-configurable via `app/config.py`'s `SANDBOX_TIMEOUT_DEFAULT`/`_MAX`,
30s/120s by default) — the same crash/timeout containment pipelines get,
just answered synchronously in the HTTP request rather than queued: a
sandbox run is read-only (no materialization), so unlike pipeline runs it
never needs to be serialized against other runs.

## Conversational analytics

A **CHAT** surface (specs/012-conversational-analytics/) lets a signed-in
user ask plain-language business questions and get back a natural-language
answer grounded in the same semantic layer and query engine everything else
in this app uses — the assistant never queries data directly. Every
question is translated into a proposal (`propose_query` / `ask_clarification`
/ `show_last_query` / `decline`), which is then **re-validated against the
live model** before it's ever executed through `engine.run_query` — the
same code path `POST /api/query` runs. An LLM can propose, it can never
bypass the semantic layer: a proposal referencing an undeclared column, an
unjoined model, an out-of-vocabulary filter operator, or anything else
outside what's already declared is rejected before any query runs.
`show_last_query` lets a user reliably ask for the exact query behind a
prior answer (e.g. "what query did you just run?") without that request
being mis-translated as a new, unanswerable business question. It is
deliberately narrow: a follow-up that wants the previous answer *changed*
("break this down by quarter", "now just the top 5", "and last year?") is a
`propose_query` built on the prior turn — the assistant carries that turn's
model, dimensions, measures, filters, sort and limit forward and applies
only what the follow-up changes, and the complete result is re-validated
fresh like any other proposal (FR-008/FR-009). Conversations persist
per-user (SQLite) and are strictly owner-scoped; asking a question requires
only the **viewer** role, the same tier as the query builder.

**Ad-hoc measures (`propose_query`'s `inline_measures`).** A question that
needs a calculation nobody declared as a measure — a running total, a
period-over-period change or growth rate — doesn't have to decline or wait
for a model author. The assistant can define it itself, scoped to that one
query, using the same safe measure DSL model authors use: `running_total
(revenue)`, `lag(revenue)`, `lag(revenue, 4)`, or plain arithmetic around
them (e.g. `(revenue - lag(revenue)) / lag(revenue)` for a % change). Every
inline measure is re-validated exactly like everything else: it must be a
window expression over one of the model's own already-declared measures —
never a raw column, and never another inline measure — or the whole
proposal declines.

**Categorical "common sense."** A dimension's real stored values often
don't match a question's wording exactly — different case (`cardiology` vs.
the stored `Cardiology`), a different code system (ISO-2 vs. a column
declared as ISO-3), or a paraphrase. The catalog includes up to 200 real
sample values for each closed-vocabulary categorical dimension (omitted for
a free-text/ID-shaped column whose distinct values exceed that, or where the
data can't be reached), so the assistant can convert a question's wording to
what's actually on file before filtering. As a safety net, an `eq`/`ne`/
`in`/`not_in` filter's value is also case-insensitively corrected against
those same sample values before the query runs, in case the model's own
conversion falls short.

**Self-learning model memories.** Chat teaches the assistant about the
*models*, and what it learns compounds: any tool call may carry `memories` —
durable, user-independent facts learned from that exchange, stored against
the semantic model in SQLite (`model_memories`). Two kinds, a deliberately
closed vocabulary: **synonym** (the question used a business term for a
declared dimension/measure that the yaml doesn't list — e.g. users say
"gross takings" for `revenue`) and **note** (a short fact about the model's
vocabulary or data). Like a proposed query, a proposed memory is never
trusted: it's re-validated against the live model (the synonym's target must
be a declared dimension/measure, redundant/over-long/unknown-kind entries
are dropped, max 3 per turn, deduped, capped per model) before it's stored.
On every subsequent ask — by **any** user — stored synonyms merge into the
catalog's `also called` lists and notes appear as `learned fact` lines, so
a term taught once grounds every future conversation. Memories describe the
model, **never the person asking**: the system prompt forbids recording user
preferences/identity/habits, the kind vocabulary has no user-shaped entry,
and the table has no user retrieval axis at all (`created_by` is audit
attribution only). Admins curate the pool — every learned memory is
listable, editable, and deletable via `GET /api/models/{m}/memories`
(any authenticated role) and `POST/PATCH/DELETE` (**admin**), or from the
**◈ memory** button on a model card in MODELLING; every write (learned or
curated) lands in the audit log.

**Off by default.** The whole feature — nav entry, API routes, everything —
is disabled (`GET/POST /api/conversations*` return 503) unless
`CI_LLM_API_KEY` is set. Set it (and optionally `CI_LLM_MODEL`, default
`claude-sonnet-5`) to enable:

```bash
export CI_LLM_API_KEY=sk-ant-...
export CI_LLM_MODEL=claude-sonnet-5   # optional: the default a new conversation starts with
export CI_SANDBOX_AGENT_MODEL=claude-sonnet-5           # optional: sandbox coding agent (defaults to CI_LLM_MODEL)
export CI_SANDBOX_LINEAGE_MODEL=claude-haiku-4-5-20251001  # optional: convert-to-pipeline lineage generation
```

The same key switches on every LLM-backed surface: chat, the Composer, and
the sandbox's [coding agent](#the-coding-agent). Each sends different things
to the provider — the sandbox agent sends notebook *code*, schemas and bucket
paths (never result rows); see that section and the egress note below.

`CI_LLM_MODEL` only sets the *default* — each conversation can also pick its
own model in the CHAT header (`app/config.py`'s `LLM_MODEL_CHOICES`), a
trade-off between answer quality and cost/latency. The picker is populated
from `GET /api/health`, so it always reflects what the server actually
allows — never hardcoded per deployment.

### Any provider: a URL and a key

Nothing above this layer is Anthropic-specific. `app/llmclient.py` is the one
module that knows which *wire format* an endpoint speaks; the three seams
above it (`app/llm.py`, `app/sandbox_agent.py`, `app/composer.py`) build one
provider-neutral request each and never import a vendor SDK. All three ask
the model for exactly the same thing — *call one of these tools, with these
arguments* — which is why porting them across providers is a transport
concern and not a prompt rewrite.

Point the app anywhere by setting a base URL and a key. The URL's host picks
the wire format:

```bash
# OpenAI
export CI_LLM_BASE_URL=https://api.openai.com/v1
export CI_LLM_API_KEY=sk-...
export CI_LLM_MODEL=gpt-4o

# Azure OpenAI (the v1 surface — URL and key, nothing else)
export CI_LLM_BASE_URL=https://my-resource.openai.azure.com/openai/v1/
export CI_LLM_API_KEY=...
export CI_LLM_MODEL=my-deployment-name

# AWS Bedrock, OpenAI-compatible surface (with a Bedrock API key)
export CI_LLM_BASE_URL=https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1
export CI_LLM_API_KEY=...
export CI_LLM_MODEL=openai.gpt-oss-120b-1:0

# AWS Bedrock, native Claude models (IAM role / the AWS credential chain —
# no API key at all, which is the point for most AWS deployments)
export CI_LLM_PROVIDER=bedrock
export CI_LLM_AWS_REGION=us-east-1
export CI_LLM_MODEL=us.anthropic.claude-sonnet-4-5-20250929-v1:0

# anything OpenAI-compatible: OpenRouter, vLLM, Ollama, LiteLLM, Together, …
export CI_LLM_BASE_URL=http://localhost:11434/v1
export CI_LLM_MODEL=qwen2.5-coder:32b
```

| variable | what it does |
| --- | --- |
| `CI_LLM_BASE_URL` | the endpoint. Unset = Anthropic's own API (the default, unchanged) |
| `CI_LLM_API_KEY` | the key for that endpoint, and the on/off switch for every LLM feature |
| `CI_LLM_MODEL` | a model id that endpoint actually serves (on Azure, the deployment name) |
| `CI_LLM_PROVIDER` | `auto` (default) / `anthropic` / `openai` / `azure` / `bedrock` — override the guess |
| `CI_LLM_MODEL_CHOICES` | comma-separated ids the CHAT model picker offers |
| `CI_LLM_THINKING_MODELS` | comma-separated ids to request extended thinking/reasoning from; empty = never |
| `CI_LLM_REASONING_EFFORT` | `reasoning_effort` for those models on the OpenAI wire (default `medium`) |
| `CI_LLM_API_VERSION` | only for Azure's older dated surface; set `CI_LLM_BASE_URL` to the bare resource root with it |
| `CI_LLM_AWS_REGION` | region for native Bedrock (defaults to `AWS_REGION`) |
| `CI_LLM_MAX_TOKENS_PARAM` | `auto` (default) / `max_tokens` / `max_completion_tokens` — see below |

Detection is by URL because a URL is the only thing a deployer reliably has:
`*.anthropic.com` → the Anthropic wire, `*.azure.com` / `*.azure-api.net` →
Azure, `bedrock-runtime.*` → native Bedrock unless the path is its
`/openai/…` surface, and **anything else → the OpenAI wire**, which is what
essentially every gateway and self-hosted server speaks. The one case a URL
can't express is an *Anthropic*-format gateway on a neutral host — set
`CI_LLM_PROVIDER=anthropic` for that. `GET /api/health` reports the resolved
provider (`llm_provider`) so a misdetection is visible without reading logs.

Three details the abstraction absorbs rather than pushing onto you:

- **Model ids aren't portable, so the Claude defaults get out of the way.**
  `LLM_MODEL_CHOICES` and `LLM_THINKING_MODELS` only keep their built-in
  Claude values while `CI_LLM_MODEL` is one of them. Point `CI_LLM_MODEL`
  elsewhere and the picker narrows to that model alone (and the lineage
  helper stops reaching for a Haiku id no other provider serves) until you
  name the rest with `CI_LLM_MODEL_CHOICES`.
- **Reasoning models renamed `max_tokens` to `max_completion_tokens`.** The
  spelling is guessed from the model id and then *self-corrects* from the
  provider's own 400 on the first request, so an unrecognized id on an
  unfamiliar gateway still works. Pin `CI_LLM_MAX_TOKENS_PARAM` if a
  gateway's error text is too vague to key off.
- **Live streaming works on both wires.** The Anthropic SDK hands over
  already-parsed partial tool input; the OpenAI wire streams raw JSON
  fragments, so `llmclient.parse_partial_json` reassembles them into the same
  growing dicts. That is what keeps the Composer's page appearing as it is
  written and the sandbox agent's code filling in live, rather than landing
  in one blob at the end.

Extended thinking is a declared opt-in (`CI_LLM_THINKING_MODELS`) rather than
a blanket flag because a model that doesn't support it rejects the whole
request instead of ignoring the parameter — the exact failure mode that used
to 400 Haiku with "adaptive thinking is not supported on this model". It maps
to Anthropic's adaptive thinking on one wire and `reasoning_effort` on the
other.

What does *not* change per provider: every tool schema, every system prompt,
and — most importantly — every re-validation. A proposal from any model on
any endpoint is still unvalidated text until `nlq.resolve()` re-checks it
against the live semantic model, `sanitize_notebook_html()` re-checks a
composed page, and `app/sandbox.py` re-checks proposed cells. Swapping
providers cannot widen what the platform will execute.

**What leaves the deployment, and to whom, when enabled:** every question
sends the question text and a catalog of the declared model/dimension/
measure names and descriptions to the configured LLM endpoint over HTTPS, so
it can propose a query or ask a clarifying question. A name and description
alone are often not enough to pick the right measure (e.g. an unweighted
average vs. a revenue-weighted one, or a measure with no description at
all) — so the catalog also includes each non-framed measure's DSL formula
(e.g. `sum(unit_price * quantity)`), the same text already visible to any
authenticated user via `GET /api/models`/the modelling workspace. A formula
may name a raw source column that's otherwise never sent (dimensions,
filters, and sort only ever use declared names) — this is schema text, not
row data, and a raw column named in a formula still can't be used anywhere
in a proposal (the existing re-validation rejects it), but it is a
deliberate, documented widening of what reaches the third party. Framed
measures (the rarer `frame:`-based ones) are exempt: their DSL fragment
isn't self-contained without that frame's context, so only their name/
description is sent, same as before. The catalog also sends up to 200 of a
categorical dimension's actual distinct stored values (omitted above that
cardinality, or if its source can't be reached) — unlike everything else in
the catalog, this is real row data, not schema text, sent so the assistant
can match a question's wording (case, code system, phrasing) to what's
genuinely on file; a dimension with more distinct values than that, or one
typed `time`/`numeric`, never has its values sent this way. Once a proposal
is validated and run,
the resulting *result rows* (capped at `MAX_ROWS`, same cap the query
builder uses) are also sent, so the assistant can generate the
natural-language answer text. Nothing is sent to any third party unless
`CI_LLM_API_KEY` is configured — there is no separate feature flag to
forget, the key's presence is the flag.

## Agents & MCP server

specs/017-agent-skills-mcp-server/ generalizes conversational analytics'
tool-calling pattern into two reusable concepts, and exposes them to
external MCP clients (Claude Desktop, Claude Code, or any other MCP-capable
agent host) — not just the browser's own CHAT surface.

- **Skill** (`app/skills.py`): a named, typed, role-gated capability with a
  handler — `min_role` (viewer/author/admin), a JSON Schema input/output
  shape, and whether it's rate-limited. Every skill call goes through one
  dispatch path, `invoke_skill()`: role check → rate limit (if applicable)
  → the handler → an audit log entry (`mcp_skill:<name>`, or
  `:denied`/`:rate_limited` for a blocked attempt) — so a future skill gets
  those guarantees without reimplementing them.
- **Agent** (`app/agents.py`, `agents/*.yaml`): a named, described bundle of
  skill names, declared in YAML the same way a model is declared in
  `models/*.yaml`. An agent carries no privilege of its own — it's purely a
  discoverable grouping; every skill call is still gated by that skill's
  own `min_role` against the caller's real role, and a skill only counts as
  *exposed* while some loaded agent currently references it. Editing an
  agent's `skills:` list and reloading changes what's exposed immediately,
  with no code change.

The shipped **analytics agent** (`agents/analytics.yaml`) exposes two
skills, both **viewer**-tier:

| Skill | Rate-limited | What it does |
|---|---|---|
| `ask_question` | yes | Ask a business question in plain language; wraps the exact same question → resolve → execute → persist → audit path conversational analytics' CHAT surface uses (`app/nlq.py`) — a call and a browser chat turn against the same `conversation_id` share one history. |
| `list_models` | no | The same models/dimensions/measures catalog `ask_question` is grounded on — discovery before asking. |

**Connecting**: the MCP server is mounted at `/mcp` (Streamable HTTP,
stateless) alongside the REST API, in the same deployment — not a separate
process. It authenticates exactly like `/api`: a session cookie or, for a
non-browser MCP client, a per-user `Authorization: Bearer cipat_...` token
(**Account → tokens**, the same mechanism scripts already use — see
"Authoring model measures" above). There is no anonymous MCP handshake —
even `initialize` requires valid credentials — and `tools/list` only ever
returns the skills a connection's authenticated role can actually invoke
(a viewer never sees an author/admin-only tool at all, not merely gets
refused calling one).

**Rate limiting**: `ask_question` calls the same LLM backend conversational
analytics does, so it's gated by a per-identity, in-process limit —
`CI_MCP_RATE_LIMIT_PER_MIN` (default `20`). A caller past the limit gets
`outcome: "rate_limited"` immediately, no LLM call made.

**Scope, deliberately**: this feature is read/query-only — `ask_question`
and `list_models` never save, trigger, or author anything — and the
sandbox coding agent (admin-only, unsandboxed code execution) does not
join the MCP surface at all. Extending either would be a new feature that
explicitly reopens the constitution's trusted-config boundary principle,
not a side effect of this one.

## Notebooks & the Composer

A **notebook** is a freeform HTML narrative page — not a dashboard grid.
The stored `html` is a body fragment written in a small fixed vocabulary
that the client (`static/js/notebook.js`) brings to life after render:

- `<div class="nb-visual" data-visual-id="…">` — a saved visual, re-executed
  live (add class `compact` for a short stat-height tile)
- `<div class="nb-dashboard" data-dashboard-id="…" data-view="N">` — a whole
  dashboard embedded at one of its saved views
- `<div class="nb-tabs">…` — tab groups; `<details class="nb-collapsible">` —
  depth-on-demand sections
- `<aside class="nb-explainer" data-title="…" data-tone="info|method|warn">` —
  explainer windows: callouts that teach the reader how to read a chart,
  define a term, or flag a caveat
- `<div class="nb-split">` — a claim | proof diptych row (prose one side,
  chart the other)

Pages can be written by hand through the API — or chatted into existence in
the **COMPOSER** (home → notebooks panel → *+ compose a page*, or ✎ COMPOSER
on any open notebook). The composer is a split workspace: the *script* on
the left (pick a template — executive report / tabbed explorer / long-form
narrative / one-page brief / freeform — paste your narrative, tick the
saved visuals and dashboards to build around, then issue instructions), and
the *proof* on the right — the draft page itself, hydrated live, typing
itself out as the model streams. Iterate conversationally ("make the funnel
section tabs instead", "add an explainer beside the trend chart", "tighten
the intro") — each turn revises the whole page and re-renders the proof.
Nothing persists until you hit SAVE, which writes through the ordinary
notebooks CRUD.

Trust model (same seam discipline as conversational analytics): the LLM's
output is *unvalidated* until `app/composer.py`'s `sanitize_notebook_html`
re-checks it — allowed tags/attributes only (no scripts, handlers, inline
styles, or external resources; disallowed markup is stripped and reported),
and every embedded visual/dashboard id is re-verified against the live
registry. A page referencing an id that doesn't exist fails the turn
outright rather than saving dead embeds. The composer's prompt also forbids
invented numbers: the live charts carry the figures, the narrative only
frames them. 503 unless `CI_LLM_API_KEY` is configured, like chat. What is
sent to the third party per turn: the instruction/narrative/history you
typed, the catalog of saved visual/dashboard names + their declared query
fields, and the current draft html — never result rows.

## Frontend notes

Charts are hand-rolled SVG (no chart library): bar / line / **scatter** /
**sankey** / **ribbon** / **geo bubble map** / stat tiles / table. AUTO picks
from the query shape; the exotic types are explicit choices in DISPLAY:

- **scatter** — ≥1 dimension + 2 measures (x, y); a second dimension colors the
  points, with a distinct marker shape per series (color alone fails all-pairs
  colorblind checks, shapes are the secondary encoding).
- **sankey** — ≥2 dimensions as flow stages, first measure = link width.
- **ribbon** — time dimension + categories; bands re-rank at every x, so lead
  changes read as crossings.
- **geo** — needs a map-enabled dimension: give it `geo: {lat, lon}` in the
  model yaml (see `models/marketing.yaml`) and the engine carries mean
  coordinates alongside the measures. Bubbles size by the first measure over a
  vendored world outline (`static/world.geo.json`, no external tiles).

**Dashboard interactions** (all ephemeral — never saved, a refresh resets them):

- **cross-filtering** — click a categorical mark (bar, scatter point, sankey
  node, ribbon band, map bubble) and every other tile whose model has that
  dimension filters to the clicked value; the source tile glows pink and a chip
  in the view bar shows the active cross-filter. Click the same mark (or the
  chip) to clear.
- **focus mode** — ⤢ on a tile expands it full-screen with its own ad-hoc
  filter bar; nothing there touches the saved visual or dashboard.
- **grain override** — the GRAIN select re-buckets every tile's time dimensions
  (day → year) regardless of each visual's saved grain.

### Instant mode (round-trip-free interaction)

Tick **INSTANT** in a dashboard's view bar and each tile fetches its data once,
as an Arrow extract, then answers every subsequent **cross-filter, view-filter
change, coarser grain change and focus-mode open** *in the browser* — zero
network calls until the page reloads or something the extract genuinely can't
answer changes. It is opt-in per dashboard and persisted with it; every
dashboard defaults to off and behaves exactly as it always has.

The aggregation engine is [Perspective](https://perspective.finos.org/) (FINOS,
Apache-2.0), used **headless**: `Table`/`View` only, no `perspective-viewer`,
no rendering. Charts stay the same hand-rolled SVG renderers, reading the same
`{columns, rows}` they get from `/api/query`. It is vendored under
`app/static/vendor/` (no CDN, see `app/static/vendor/README.md`) and pulled in
by a dynamic `import()` that runs only for a dashboard with instant mode on —
no other view loads a byte of it.

**What an extract contains.** Alongside the tile's own dimensions it carries
every *other* tile's dimensions that this tile's model also has, so a
cross-filter originating elsewhere can be applied without asking the server.
That is the trade: a wider result set (still fully pushed down, still capped)
in exchange for no round trips afterwards. Time dimensions are the exception —
no renderer emits a cross-filter from a time mark, so another tile's dates are
never carried, which is what keeps the union cheap. It also carries precomputed
coarser time buckets for the tile's *own* dates, so a GRAIN change to a coarser
bucket is answered locally from values polars truncated — the browser never
does date arithmetic of its own. Weeks don't nest inside months, so a
week-grained extract offers nothing coarser; a *finer* grain than was fetched
re-queries for that interaction.

**The view bar's filters are carried, not baked in.** A dashboard filter is
the thing viewers actually poke at, so its dimension rides along as a column
and the extract holds every value of it — changing the filter is then a
re-slice, not a re-query. A field another tile already displays costs nothing
extra; a new one costs its cardinality, which is what the cap is for. If
hoisting pushes a tile over the cap, the filters go back into the pushdown and
the tile *stays* instant — it just re-fetches when a filter changes, which is
what it did before. Two filters are never hoisted, because the browser cannot
reproduce them exactly:

- **time filters** — the engine filters the raw date column, while the extract
  holds it truncated to the tile's grain, so `>= 2024-06-15` would keep all of
  June locally instead of half of it.
- **`contains`** — the engine runs it as a case-insensitive *regex* over the
  column cast to string, which no client-side substring match reproduces.

Changing either still works; it just re-fetches that tile's extract, exactly
as a parameter change does.

**Measures are decomposed, not re-averaged.** Re-aggregating an already
aggregated extract is only sound for measures that decompose, so each one is
split server-side into additive components and recomputed from them after the
roll-up (`measure_dsl.rollup_plan`):

| measure | components fetched | recomputed as |
|---|---|---|
| `sum(revenue)` | `sum(revenue)` | itself |
| `count()` | `count()` | itself |
| `mean(fare_amount)` | `sum(fare)`, `count(fare)` | sum ÷ count |
| `sum(tip) / sum(fare)` | `sum(tip)`, `sum(fare)` | the ratio, after totalling |

Dividing *once, at the end* is what makes means and ratios exact rather than
approximate. A measure with no such decomposition — `count_distinct`, `median`,
`std`/`var`, `first`/`last`, a window measure (`running_total`/`lag`), or one
over an intermediary frame — cannot be re-aggregated without changing its
value, so that tile silently stays on the live path instead.

**Per-tile fallback.** Instant vs. live is decided per tile, never per
dashboard, and a dashboard routinely ends up with a mix. A tile falls back
when its measures don't decompose, when a dimension fans a row across periods
(a time spine or a `how: between` calendar join would double-count on
roll-up), when its extract trips the size cap, or when anything at all goes
wrong fetching or loading it. Fallback is silent, permanent for the session,
and visible: every tile on an instant dashboard carries a `⚡ instant` or
`live` badge whose tooltip gives the extract's row count and size, or the
reason it fell back. Portal viewers see the same badges. If Perspective itself
fails to load, the whole dashboard runs live for the session.

**Size cap.** `CI_EXTRACT_MAX_ROWS` (default 150,000) and
`CI_EXTRACT_MAX_BYTES` (default 25MB), checked per tile against the real
response — either one tripping sends that tile live. Measured against the
13M-row taxi model:

| tile | extract | outcome |
|---|---|---|
| monthly trend × payment type × vendor | 72 rows / 0.01 MB | instant |
| daily trend × payment × passengers × vendor | 17,819 rows / 4.1 MB | instant |
| pickup zone × dropoff zone × payment type | >150,000 rows | **live** (row cap) |
| pickup zone × dropoff zone × day | >150,000 rows | **live** (row cap) |

Nothing about instant mode is persisted beyond the flag itself: extracts live
in memory, are rebuilt on every page load, and are discarded the moment a
change arrives that they can't answer.

**Dashboards**
are grids of saved visuals: create one in the sidebar, `+ ADD` saved visuals as
tiles, toggle each tile between half and full width — layout auto-saves.
**Views** are named filter sets on a dashboard: filters in the view bar are
pushed down to every tile whose model has that dimension (matched by name, so
one `region` filter can drive tiles from different models; a `⧩` badge marks
affected tiles). Filter edits auto-save into the active view; `+ VIEW` snapshots
the current filters under a new name and the dropdown switches between them. Each
theme's categorical palette is independently validated against that theme's
surface (lightness band, chroma floor, colorblind-safe adjacent separation,
≥3:1 contrast) — open `/?validate` and check the browser console to re-run
the checks against whichever theme is currently active. Series colors follow
entities, not ranks; more than 8 series folds the tail into "Other".

## Themes

**ACCOUNT → Appearance** switches between 4 pre-packed visual themes:
**Cyberpunk** (the original dark-neon look, still the default), **Paddock
Light** (light — heritage-motorsport papaya and racing-green ink on aged
cream cardstock, for bright environments), **Canopy** (a muted professional
dark — forest-green surfaces with copper and moss accents, no neon), and
**Paddock** (heritage motorsport — enamel papaya against oiled gunmetal, a
racing-green second accent). Switching is instant —
no reload — and re-skins everything, chart colors included. (The three
alternatives' internal ids — `daylight`, `slate`, `contrast` — predate their
current designs and are frozen in stored preferences; only the looks and
labels changed.)

A theme choice is remembered on the browser it was picked in (`localStorage`)
and, for signed-in users, also synced to their account
(`GET`/`PUT /api/users/me/theme`) so it follows them to another browser or
device. If the two ever disagree — e.g. a choice made offline on one device,
then a different choice made elsewhere — whichever was picked most recently
wins on next login/boot, and gets written back to the side that was behind.

All of this is built on `app/static/js/theme.js`, which owns the 4-theme
catalog (CSS custom-property overrides live in `style.css`'s
`[data-theme="..."]` blocks; each theme's categorical chart palette lives
alongside it, run through `validate_palette.js` before being accepted).
Creating or uploading a custom theme is intentionally out of scope for now —
picking among the 4 shipped ones is all that's wired up
(`specs/013-theme-presets/`).
