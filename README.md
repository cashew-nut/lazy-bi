# CASH_INTELLIGENCE

Lightweight BI over data files in S3. Polars scans the files **lazily** — only
the columns and row-groups a query needs leave the bucket — aggregates them, and
returns results to a cyberpunk query-builder UI. A YAML **semantic layer**
defines the sources (**parquet / csv / Delta Lake**), **joins**, dimensions and
measures the builder works with; saved visuals and **dashboards** persist in
SQLite.

```
browser (query builder + dashboards + SVG charts)
   │  POST /api/query {dimensions, measures, filters, sort, limit}
   ▼
FastAPI ──► semantic layer (models/*.yaml) ──► polars LazyFrame scan (+ lazy joins)
   │                                              │ predicate/projection pushdown
   ▼                                              ▼
SQLite (visuals + dashboards)             S3 (moto emulator in demo mode)
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

To point at a real bucket or an external emulator (MinIO, LocalStack), set
`CI_S3_ENDPOINT` (this also disables the embedded moto server) plus the usual
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, and `CI_BUCKET`.

Set `CI_API_KEY` to enable saving/editing/deleting model measures (unset =
those mutations are always rejected — see "Authoring model measures" below).

## Project layout

```
app/
  config.py            env-driven settings (endpoints, paths, bucket)
  main.py              app factory + lifecycle (emulator, seed, registry)
  registry.py          runtime state: loaded models + store
  semantic.py          semantic layer: yaml -> Model/Dimension/Measure/Join/Spine/Geo/
                       DimensionBundle/Import
  engine.py            query engine: semantic query -> polars lazy scan
  store.py             sqlite persistence: visuals, dashboards, publications
  emulator.py, s3.py, seed.py, load_taxi.py
  api/                 one router per resource: models, dimensions, datasets, query,
                       visuals, dashboards (+publish/portal), explorer (+health)
  static/js/           ES modules: lib, state, filters, builder, dashboard,
                       portal, modelling, editor, completion, measurelab, main
  static/js/charts/    one renderer per chart + shared frame/pivot/dispatch
models/*.yaml          semantic models (the editable contract)
dimensions/*.yaml      dimension bundles shared across models (see below)
tests/                 pytest: semantic, engine, store, API
Dockerfile, docker-compose.yml
```

## The semantic model

One YAML file per model in `models/`. The query builder only exposes what the
model declares — the UI never touches raw columns directly.

```yaml
name: sales
label: Sales Orders
source:
  format: parquet                      # parquet | csv | delta
  path: s3://cash-intel/sales/*.parquet  # any glob polars can scan (delta: table root)

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

#### Visual parameters: a viewer-toggleable `lag()` offset

A visual can declare a named parameter — a fixed list of allowed integer
values plus a default — and a `lag()` measure on that same visual can
reference it instead of a literal period count:

```
period_list = [1, 2, 3, 4], default 1
revenue_lag = lag(revenue, param('period_list'))
```

Whoever is viewing the visual gets a control listing `period_list`'s
declared values; picking one re-runs the query with that shift amount,
with no expression editing involved. `param('name')` is legal in exactly
one place — `lag()`'s second argument — nothing else in the DSL accepts
it, so this stays fully inside the same allowlisting compiler as every
other measure (see "The safe measure DSL" above): the server only ever
substitutes one of the parameter's own declared values, never an
arbitrary one, the same way `partition_by`/`order_by` are threaded in from
query context today. Because a parameter is visual-scoped context a
shared model measure never has, a measure referencing one can only be
**SAVE TO VISUAL**'d, never promoted to the model — see "The measure lab"
below.

On a dashboard, a parameter's current selection is saved per named view,
alongside its filters. If two tiles' visuals declare a parameter with the
same name *and* an identical definition (same values, same default), the
dashboard shows one shared control that drives both; if the definitions
differ, the dashboard refuses to let both visuals sit on it together
(add-tile and every dashboard save both enforce this — see
`specs/009-visual-parameters/`).

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
the authenticated model-measure save endpoint** (`X-API-Key` + `X-Author`,
see below) — never as an inline/visual-scoped measure, regardless of
credentials. A `frame` submitted inline on `/api/query` is rejected outright.

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

See `median_months_to_75pct_randomised` in `models/clinical_ops_recruitment.yaml`
for a live example (median months for a study's cumulative randomisations to
cross 75% of its total, bucketed on timelines by each study's crossing month).
Inline/visual-scoped measures on the query API cannot use `frame`/`frame_emits` —
that construct is authenticated-model-measure-only (see above).

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
each row counts in every bucket it was active for — semantics are "active as of
the bucket start". Range filters on the spine (`>=`, `<=`, `=`) bound the
timeline window; `=` gives a single-date snapshot even with no grouping.
Buckets with zero active rows are omitted. One spine dimension per query.
See `models/subscriptions.yaml` for a working example (active customers, MRR,
ARPU over a 30-month timeline).

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
declaration. Imported dimensions behave exactly like native ones everywhere
(builder, filters, dashboards, cross-filtering by name); a same-named
dimension declared natively on the fact model always wins over an imported
one. See `dimensions/geography.yaml`, imported by both `models/sales.yaml`
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
(post-join, with dtypes); `param('` — legal only as `lag()`'s periods
argument — offers this visual's declared parameters, each hinting its
values and default. Every keystroke re-runs the current query with the
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

Creating, updating, or deleting a saved model measure requires a shared
secret: set `CI_API_KEY` in the environment (unset = every mutation is
rejected with 401 — fail closed by default) and send it as `X-API-Key`,
alongside a self-declared `X-Author` label recorded on the change. This is a
minimal placeholder for real auth, not a claim of strong per-user identity —
swap it for something stronger when the app grows beyond a single shared
secret. Reading/querying a saved measure never requires it.

| Route | Auth | What it does |
|---|---|---|
| `POST /api/models/{m}/measures` | required | create a measure (validated, then appended to the yaml) |
| `PUT /api/models/{m}/measures/{name}` | required | update a measure in place |
| `DELETE /api/models/{m}/measures/{name}` | required | remove a measure |
| `GET /api/models/{m}/measures/{name}/history` | — | append-only provenance: author, version, expression snapshot per save |

Every create/update is validated (the safe DSL, or `validate_frame` for a
`frame:` measure) before anything is written — an invalid measure is refused,
never partially saved. Provenance is recorded in a separate SQLite table
(`measure_provenance`, in `cash_intel.db`) alongside the yaml write; the yaml
file remains the sole executable source of truth, the table is the audit log.

## API

| Route | What it does |
|---|---|
| `GET /api/models` | models with their dimensions + measures |
| `POST /api/models/reload` | re-read `models/*.yaml` |
| `GET /api/models/{m}/dimensions/{d}/values` | distinct values (filter pickers) |
| `GET/PUT /api/models/{m}/yaml` | read / save a model's YAML (save validates + hot-reloads) |
| `POST /api/models/validate` | parse-check YAML + introspect source columns |
| `POST /api/models`, `DELETE /api/models/{m}` | create a model file / delete one |
| `GET /api/datasets` | bucket objects grouped into pickable datasets (source picker) |
| `POST/PUT/DELETE /api/models/{m}/measures[/{name}]` | create/update/delete a model measure (**requires `X-API-Key` + `X-Author`** — see "Authoring model measures" above) |
| `GET /api/models/{m}/measures/{name}/history` | append-only provenance for a saved measure |
| `POST /api/query` | run a semantic query, returns columns + rows + timing |
| `GET/POST /api/visuals`, `PUT/DELETE /api/visuals/{id}` | saved visuals (SQLite: `cash_intel.db`) |
| `GET/POST /api/dashboards`, `GET/PUT/DELETE /api/dashboards/{id}` | dashboards — ordered tiles `{visual_id, w:1\|2}`; GET by id resolves tile visuals; create/update reject a tile set where two visuals declare a same-named, differently-defined parameter (see "Visual parameters" above) |

Query shape:

```json
{
  "model": "sales",
  "dimensions": [{"name": "order_date", "grain": "1mo"}, "region"],
  "measures": ["revenue", "margin_pct"],
  "filters": [{"field": "segment", "op": "in", "values": ["corpo", "solo"]}],
  "sort": {"by": "revenue", "desc": true},
  "limit": 1000,
  "parameters": [{"name": "period_list", "values": [1, 2, 3, 4], "default": 1}],
  "parameter_values": {"period_list": 2}
}
```

Filter ops: `eq ne gt gte lt lte in not_in contains`. `parameters` declares
a visual's parameters (travels with the query the same way `inline_measures`
does); `parameter_values` is the caller's current pick per parameter —
missing a name falls back to that parameter's own default, and any value
outside its declared list rejects the whole query before anything runs. A
dashboard view's saved `parameters: {name: value}` map (alongside its
`filters`) is what a dashboard tile's query pulls this from.

## Studio, Modelling, Portal

The header nav splits the app into three surfaces:

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
  rail manages every fact model and common model — *edit yaml*, *build ►*, and
  *+ MODEL* / *+ COMMON MODEL* — and the right pane is the data overview: every
  object in the bucket with size and modified date, matched against each model's
  source and join globs (Delta table internals map to their model too). Clicking
  a model chip jumps to it in the builder; files no model reads are flagged as
  unmapped. This is where authoring — the dataset picker, guided common-model
  import, and expression intellisense described above — lives.

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

**Dashboards**
are grids of saved visuals: create one in the sidebar, `+ ADD` saved visuals as
tiles, toggle each tile between half and full width — layout auto-saves.
**Views** are named filter sets on a dashboard: filters in the view bar are
pushed down to every tile whose model has that dimension (matched by name, so
one `region` filter can drive tiles from different models; a `⧩` badge marks
affected tiles). Filter edits auto-save into the active view; `+ VIEW` snapshots
the current filters under a new name and the dropdown switches between them. The categorical
palette is validated for the dark surface (lightness band, chroma floor,
colorblind-safe adjacent separation, ≥3:1 contrast) — open `/?validate` and
check the browser console to re-run the checks. Series colors follow entities,
not ranks; more than 8 series folds the tail into "Other".
