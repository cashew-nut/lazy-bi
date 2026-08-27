# Semantic Layer

**Source:** `app/semantic.py` (1737 lines) · YAML in `models/*.yaml` and
`dimensions/*.yaml`

The semantic layer is the one contract everything downstream depends on:
the query builder, dashboards, the chat assistant, and the MCP server never
touch a raw source column. Every dimension and measure they can use is
declared in a model YAML file first. `app/semantic.py` is the whole of that
contract — parsing YAML into typed objects, validating it (including
compiling every measure expression through the [SQL grammar](query-engine.md#the-sql-grammar)
at load time), resolving cross-file dimension imports, and rendering
objects back to YAML for the guided authoring forms.

New data sources are onboarded by writing or editing a model — never by
adding a special case to the query engine.

## The building blocks

```
Source ── one file/table: {path, format}         format ∈ parquet|csv|delta|iceberg
Join ───── model → raw Source, with a join key
Dimension  {name, column, label, type, description, spine?, geo?, grain?, synonyms}
Measure ── {name, label, expr_source, format, from_source?, emits, synonyms}
Dataset ── a Source + its Dimensions/Measures + DatasetJoins to sibling datasets
DimensionBundle ── a named, reusable set of Datasets (dimensions/*.yaml)
Import ─── a Model's reference to a DimensionBundle (dimension_imports:)
ModelPart ── one connected component of a model's dataset graph — a fact table
Model ───── the whole thing: parts, merged dimensions/measures, imports, lineage
```

`Dataset` is the unit both a `Model` and a `DimensionBundle` are built from —
a source, the dimensions/measures it declares, and its relations
(`DatasetJoin`) to sibling datasets. A `DimensionBundle`'s datasets never
declare measures (a common dimensional model has none); a `Model`'s may, and
declaring one is what turns a dataset into a *fact table* rather than a
lookup one.

## The two YAML shapes (and why there's only one internally)

```yaml
# terse — one fact table
name: sales
source: { format: parquet, path: s3://cash-intel/sales/*.parquet }
joins:
  - name: products
    source: { format: csv, path: s3://cash-intel/ref/products.csv }
    on: product
dimensions: [...]
measures: [...]
```

```yaml
# general — every table the model reads, plus the relations between them
name: commercial_overview
datasets:
  - name: orders
    source: { format: parquet, path: s3://cash-intel/sales/*.parquet }
    dimensions: [...]
    measures: [...]
  - name: spend            # related to nothing above it — a second fact table
    source: { format: parquet, path: s3://cash-intel/marketing/*.parquet }
    dimensions: [...]
    measures: [...]
```

`_model_datasets()` desugars the terse `source:`/`joins:` spelling into the
general `datasets:` shape at parse time (one dataset named after the model,
one `DatasetJoin` per join entry) — **everything downstream only ever sees
`Model.datasets`**. A model written either way parses into the same objects
and behaves identically; `model_to_spec()`/`spec_to_yaml()` (used by the
guided form) always round-trip through the general shape.

## Parse → split → resolve

Loading a model is a three-stage pipeline, run by `_parse_model()` and
`resolve_model()`:

1. **Parse** (`_parse_model` → `_model_datasets`, `_parse_dataset`,
   `_parse_dimensions`, `_parse_measures`). Every measure's `expr:` (and
   `from:`, if present) is compiled through `sqlgrammar.compile_expression`/
   `compile_relation` right here — **a model that fails to load is a model
   with an invalid measure**, not a runtime surprise. `_check_dataset_graph`
   rejects an unknown join target or a cycle in the relation graph.

2. **Split into parts** (`_split_parts` → `_components`, `_build_part`).
   A model's datasets don't have to be related to each other. Each connected
   component of the relation graph becomes a `ModelPart` — a synthetic
   single-source `Model` the engine can scan on its own. A single-part model
   *is* its part (`model.source`/`model.dimensions`/`model.measures` are the
   part's own, unchanged from before this concept existed);
   `model.is_composite` is `True` only when there's more than one. See
   [Several fact tables in one model](#several-fact-tables-in-one-model)
   below.

3. **Resolve imports** (`resolve_model` → `_resolve_part_imports`). Each
   part's `dimension_imports:` entries are merged in against the currently
   loaded `dimension_bundles` — this is the step that needs to look outside
   the file, so it's a separate call the registry re-runs on every reload
   (`registry.reload_all()`), not part of parsing itself. It's idempotent, so
   re-resolving an already-registered model is safe.

`app/registry.py` (see [Storage & Runtime](storage-and-runtime.md)) is what
calls all of this: `load_dimension_bundles()` before `load_models()` (a
model's imports validate against already-loaded bundles), then
`resolve_model()` per model.

## Dimensions

```yaml
dimensions:
  - name: order_date
    type: time              # categorical (default) | time | numeric
  - name: category
    column: cat_code        # differs from the semantic name
    label: Category
    synonyms: [product line]  # advisory vocabulary — see conversational-analytics.md
```

A `time`-typed dimension gets day/week/month/quarter/year grains in the
builder (`engine.GRAIN_PART`/`TIME_GRAINS`). `synonyms` are read only by the
chat catalog (`app/nlq.py`) — `Model.dimension()` still resolves purely by
`name`; a synonym is never a second valid identifier anywhere else.

### Point-in-time: spines and calendar imports

Two mechanisms answer "count this row in every period it was active for,"
and both share one predicate (`semantic.match_predicate` /
`engine.match_predicate`, with the same `MATCH_MODES`: `overlap` (default),
`period_start`, `period_end`):

- **`spine:`** on a dimension marks it as a *generated* timeline: the engine
  builds a `range()` CTE at the query's grain and interval-joins it against
  the dimension's `start`/`end` columns. No table to create or maintain —
  see `Spine` and `engine._spine_cte`/`_spine_bounds`.
  ```yaml
  - name: active_at
    type: time
    spine: { start: start_date, end: end_date, match: overlap }
  ```
- **`dimension_imports: [{..., how: between}]`** answers the same question
  from a real, standalone date table instead (declared as a bundle dataset
  with no join to anything — see below), which is the way to get real
  calendar attributes (quarter labels, business-day flags) rather than bare
  generated dates. `Import.is_interval` is `True` for `how: between`; a
  `grain:` on a bundle's own dimension (e.g. `calendar_quarter`) tells the
  engine that column is constant across that bucket, so it doesn't need to
  come from the query's grain picker.

### Geo

```yaml
- name: region
  geo: { lat: region_lat, lon: region_lon }
```

`Geo` (lat/lon column pair) makes a dimension map-eligible — the engine
carries `avg(lat)`/`avg(lon)` alongside the measures when grouping by it
(`engine._geo_select`), which is what the geo bubble-map chart reads.

## Measures — always SQL, always the same shape

```
SELECT <expr>   FROM <the fact scan, or a from: block>   GROUP BY <the query's dimensions>
```

`Measure.expr_source` is a SQL aggregate; `Measure.sql()` compiles it through
`sqlgrammar.compile_expression` (raising `ModelError` on failure — never a
bare `SqlCompileError` past this module's boundary). Whether a measure is
"simple," "complex" (`from:`), or a **window measure** is decided
structurally, not declared:

- **Plain**: `expr:` alone → `SUM(unit_price * quantity)`.
- **Window** (`Measure.is_window`, backed by `sqlgrammar.is_window_expr`):
  contains a window function → `SUM(revenue) OVER w`. Bare names inside it
  mean sibling measures, computed after the group-by.
- **Complex** (`from_source` set): `expr:` aggregates a `SELECT` block
  instead of the fact scan — `{model}`/`{dims}` placeholders are expanded by
  `render_from_block()` before the block is validated under the *relation*
  profile (`sqlgrammar.compile_relation`, `MODEL_RELATION = "__model"`,
  no table function, no base table but `{model}` and its own CTEs). `emits:`
  names dimensions the block computes itself (a per-entity milestone date),
  withheld from `{dims}` during the step and grouped on the block's output
  afterward.

The full grammar — what's allowed inside an `expr:`, the function allowlist,
window measures, `param()` visual parameters — is documented in
[Query Engine → The SQL grammar](query-engine.md#the-sql-grammar),
since it's `app/sqlgrammar.py`'s contract, shared identically by model
measures, inline (visual-scoped) measures, and chat-proposed ones.

## Dimension bundles (shared dimensions)

A bundle (`dimensions/*.yaml`) is a `DimensionBundle`: a named set of
`Dataset`s (no measures) plus the joins between them, declared once and
imported by name into any fact model:

```yaml
# dimensions/geography.yaml
name: geography
datasets:
  - name: regions
    source: { format: csv, path: s3://cash-intel/ref/regions.csv }
    dimensions: [{ name: region, geo: {lat: region_lat, lon: region_lon} }, { name: territory }]
    joins: [{ to: territories, on: territory }]
  - name: territories
    source: { format: csv, path: s3://cash-intel/ref/territories.csv }
    dimensions: [{ name: territory_name, column: name }]
```

```yaml
# models/sales.yaml
dimension_imports:
  - bundle: geography
    anchor_dataset: regions
    on: region             # sales.region = geography.regions.region
    # datasets: [regions]  # omit for the whole bundle (default)
```

`_resolve_part_imports()` walks the bundle from the import's `anchor_dataset`
by BFS (`_bfs_reachable`) to decide `ImportBinding.included_datasets` — by
default the *whole* reachable bundle, so importing `regions` above also
pulls in `territory_name` from `territories` with no separate declaration.
Naming an *unreachable* dataset explicitly under `datasets:` is a load-time
`ModelError` rather than a silent omission. A native dimension always
shadows a same-named imported one; two different imports offering the same
name is a load-time error (subset one of them).

`Import.from_dataset` says which of the model's own datasets the bundle
relates to — required once a model has more than one fact table
(`_assign_imports`), since that's what decides which fact table's measures
the imported dimensions can be grouped alongside. Importing the *same*
bundle into two different fact tables is exactly what conforms them (next
section).

A `how: left` import is only joined into a query that actually reads one of
its dimensions (it can only add columns); `how: inner` also filters the
model's rows, so it's always applied. See `engine.scan()` for where that
decision is made.

## Several fact tables in one model

A model's datasets don't have to be related to each other — `commercial_overview.yaml`
reads `orders` and `spend`, which share nothing but a common calendar
import, and are never joined to each other. `_components()` finds each
connected component of the dataset relation graph; `_split_parts()` turns
each into a `ModelPart`. This is the only correct shape: joining unrelated
fact tables directly would pair every row of one with every matching row of
the other and inflate both sides' measures. Instead each part is scanned
**separately** (`engine._build_parts`) and the aggregated *results* are
merged on the dimensions they share.

Rules enforced at load time (`_split_parts`, `_check_shared_types`,
`_check_unique_measures`):

- **Measure names are the model's public namespace** — unique across every
  part, since a query names a measure without saying which fact table it
  came from.
- **Dimension names are the conformance mechanism** — repeating one across
  parts (natively, or via importing the same bundle) is exactly what lets
  them be grouped together. A repeat *within* one part is a clash.
- **Two parts sharing a dimension name must agree on its type**
  (`_check_shared_types`) — checked pairwise, since a query reading a subset
  of parts only needs that subset's intersection to agree.
- A dataset related to nothing and declaring no measures is a load-time
  error: it's a fact table with nothing to measure.

`Model.dimensions` on a composite model holds the **all-parts intersection**
(`shared_dimensions()`) — the catalog that's safe regardless of what a query
goes on to ask for. `engine._build_parts` narrows that further, per query,
to the intersection of only the parts a query's *requested measures*
actually touch — asking for `revenue` (orders) and `ad_spend` (spend) can
group by anything both offer, even if a third part (`subs`) lacks it,
because that query never reads `subs` at all.

## Provenance, locking, and text-preserving edits

`Model.locked` / `DimensionBundle.locked` / (in `app/pipelines.py`)
`Pipeline.locked` distinguish the **built-in catalog** (loaded from the
git-tracked `models/`/`dimensions/`/`pipelines/` directories, `origin` set
to a `Path`) from **locally-authored** objects created through the API
(persisted in SQLite via `app/localmodelstore.py`/`localbundlestore.py`,
`origin` is `None`). A locked object's structure can't be changed through
the API (create/rename/delete all 403); a locked *model*'s measures can
still be edited in place — see [Storage & Runtime](storage-and-runtime.md#locked-vs-local-objects)
for how the registry merges the two.

Saving a single measure (the measure lab's "SAVE TO MODEL") doesn't rewrite
the whole file: `append_measure_yaml()` / `replace_measure_yaml()` /
`remove_measure_yaml()` do targeted text surgery on just the `measures:`
block's relevant entry, so hand-written comments elsewhere in the file
survive byte-for-byte. `replace_lineage_yaml()` does the same for the
pipeline-owned `pipeline_lineage:` section (see [Pipelines](pipelines.md#traceability-layers-and-lineage)) —
idempotently regenerated after every successful pipeline run, with a
`# ── managed by pipeline … do not hand-edit ──` banner, and every other
byte of the file left untouched.

## The guided-form round trip

`model_to_spec()` / `spec_to_yaml()` (and the bundle equivalents
`bundle_to_spec()`/`bundle_spec_to_yaml()`) convert between a parsed
(unresolved) `Model` and the plain-dict "spec" shape the guided Modelling
form edits directly — one key per YAML concept, always the general
`datasets:` shape on the way out (so a hand-written terse-form file opens in
the form as the datasets it always was, and saving rewrites it in the
general shape, with defaults omitted for terseness). Round-trips are
semantically lossless; hand-formatting and comments are not preserved on a
form save (only the measure-lab and lineage paths above preserve those, via
targeted text surgery instead of a full re-render).

## Dataset discovery helpers

A handful of pure functions (no S3 access themselves) support the
Modelling workspace's source picker and the data explorer, layered under a
bucket walk by `app/api/datasets.py`/`app/api/explorer.py`:

- `infer_format(keys)` — guess a source format from a group of object keys'
  extensions.
- `group_objects(objects, bucket)` — collapse a flat object listing into
  pickable datasets: a Delta table (anything under `_delta_log/`) or an
  Iceberg table (`metadata/<version>-*.metadata.json`) collapses to one
  entry rooted at the table directory; everything else groups by directory
  prefix into a format-inferred glob.
- `model_source_matchers(models, bucket)` — `(model_name, role, match_fn)`
  triples over every model's source/join/import globs, used to tag which
  bucket objects feed which models.
- `per_model_stats(objects, matchers, model_names)` — per-model file-count +
  byte totals over a listing, deduplicated so an object matching a model via
  two roles (its source *and* a join) still counts once.

## Reference: `Model.to_public()`

The dict shape every model-facing API returns (`GET /api/models`, and what
the chat catalog and MCP's `list_models` skill are ultimately built from):
`name`, `label`, `description`, `kind` (`fact`|`composite`), `locked`,
`path`/`format` (single-part only), `parts` (name/label/datasets/path/measures
per part), `joins`, `imports`, `dimensions` (with `spine`/`geo`/`synonyms`
flags and which dataset owns each), `measures` (with `expr`/`from`/`emits`/
`synonyms`), and `pipeline_lineage` when a pipeline targets this model's
source.
