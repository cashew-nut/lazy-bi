# Feature Specification: Core BI Platform — Semantic Layer, Query Engine & Builder

**Feature Branch**: none — predates this repository's git history; delivered pre-`git init` and landed whole in `f905a73` ("initial commit")

**Created**: 2026-07-09 (retroactively documented 2026-07-10)

**Status**: Implemented (retroactive spec, written from project history after the fact)

**Input**: Synthesized from project history, not a single verbatim prompt — this
capability was built before the project had git history or a persistent
session transcript covering its genesis. Reconstructed from: the "V2" delivery
summary ("dashboards, semantic-layer joins, and CSV/Delta sources are all
live"), the verbatim follow-up ask *"Could you build a simple UI for editing
the semantic model?"*, and the shipped `README.md`, which documents the
semantic layer, query API, and builder as the product's foundation.

## Provenance

This spec covers everything needed for the product to be a usable BI tool at
all: a YAML semantic layer, a lazy Polars query engine over S3-backed
parquet/csv/Delta sources, a query-builder UI, SQLite-backed persistence for
visuals and dashboards, and an in-app model editor. Later specs
([002](../002-time-spine-dashboard-views/spec.md),
[003](../003-advanced-visuals-cross-filtering/spec.md),
[004](../004-studio-portal-data-explorer/spec.md),
[005](../005-measure-lab/spec.md)) build on top of what this spec describes.
Advanced chart types (scatter/sankey/ribbon/geo), cross-filtering, the
studio/portal split, and inline measure authoring are explicitly **out of
scope here** — they are separate, later specs.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Query a semantic model through a visual builder (Priority: P1)

A BI developer declares a data source as a model — its S3 path, format, and
the dimensions and measures analysts are allowed to query — in a single YAML
file. An analyst then opens the query builder, picks dimensions and measures
from that model (never raw column names), applies filters, and sees the
result rendered as a chart.

**Why this priority**: Without this, there is no product — every other
capability (persistence, dashboards, joins, richer charts) is an extension of
"declare a model, query it, see a chart."

**Independent Test**: Point a model YAML at a seeded parquet source, open the
builder, select one dimension and one measure with no filters, and confirm a
chart renders with correct aggregated values.

**Acceptance Scenarios**:

1. **Given** a model YAML declaring a `region` dimension and a `revenue`
   measure over a parquet source, **When** an analyst selects both in the
   builder with no filters, **Then** the chart shows revenue totals grouped
   by region, matching a manual aggregation of the source data.
2. **Given** a query with a time dimension, **When** the analyst picks a
   grain (day/week/month/quarter/year), **Then** results are bucketed at
   that grain.
3. **Given** a query with a filter (`eq`, `ne`, `gt`, `gte`, `lt`, `lte`,
   `in`, `not_in`, `contains`), **When** the query runs, **Then** only rows
   matching the filter are reflected in the aggregated result.
4. **Given** the query engine executing against the S3-backed source,
   **When** a query requests a subset of declared columns, **Then** only the
   referenced columns' row groups are read from the source (projection/
   predicate pushdown), not the whole file.

---

### User Story 2 - Save visuals and assemble dashboards (Priority: P2)

An analyst saves a query + chart-type combination as a named visual, and can
reopen it later with identical results. Multiple saved visuals can be
arranged as tiles on a named dashboard, with each tile independently sized,
and the layout persists across sessions.

**Why this priority**: A query builder without persistence loses all work on
every refresh — saving and combining visuals is what turns ad-hoc
exploration into a reusable asset.

**Independent Test**: Build and save a visual, reload the application cold,
and confirm the visual reopens with the same query and renders the same
result. Separately, create a dashboard, add two saved visuals as tiles,
reload, and confirm both tiles render in their saved layout.

**Acceptance Scenarios**:

1. **Given** a built query and chosen chart type, **When** the analyst saves
   it as a visual, **Then** it appears in the visual list and can be reopened
   with the exact same dimensions/measures/filters/chart type.
2. **Given** one or more saved visuals, **When** the analyst creates a
   dashboard and adds them as tiles, **Then** each tile can be toggled
   between half-width and full-width, and the arrangement auto-saves.
3. **Given** a dashboard with saved tiles, **When** the application is
   restarted and the dashboard reopened, **Then** every tile re-queries its
   source and renders correctly with the saved layout intact.

---

### User Story 3 - Enrich models with joins and multiple source formats (Priority: P3)

A BI developer declares a `joins` block in a model to pull in columns from a
lookup source (a different S3 object, possibly a different format), and
those joined columns become usable dimensions/measures in the builder exactly
like base columns. Sources may be parquet, CSV, or Delta Lake.

**Why this priority**: Real fact tables need reference/lookup data (e.g.
product or supplier attributes) and real data lakes are rarely
single-format; without this the semantic layer only covers the simplest
case.

**Independent Test**: Add a `joins` entry to a model pointing at a CSV lookup
table, declare a dimension sourced from a joined column, and confirm querying
that dimension produces correctly joined, aggregated results — repeated for
a Delta-backed model.

**Acceptance Scenarios**:

1. **Given** a model with a `joins` entry (`on:` a shared key, or explicit
   `left_on`/`right_on`), **When** a query uses a dimension or measure that
   depends on a joined column, **Then** the result is correct and the join
   remains lazy (no full materialization of either side).
2. **Given** a model whose `source.format` is `csv` or `delta` rather than
   `parquet`, **When** a query runs against it, **Then** results are
   returned with the same correctness and the same lazy-scan behavior as a
   parquet source.
3. **Given** a YAML join declared with a bare `on:` key, **When** the model
   is parsed, **Then** it is read as the join-key field name, not
   YAML-1.1-coerced into the boolean `True`.

---

### User Story 4 - Edit the semantic model from within the app (Priority: P4)

A BI developer opens a model editor in the UI, edits the YAML with live
validation (parse errors and bad measure expressions surfaced per keystroke,
debounced), sees the source's actual columns — including columns arriving
via joins — to click-insert into an expression, and saves without leaving
the app or restarting it.

**Why this priority**: Editing YAML by hand on disk works but is the
highest-friction path; live validation and a real column palette make the
semantic layer approachable without needing a text editor and a restart
loop.

**Independent Test**: Open an existing model in the editor, introduce a
broken measure expression, confirm the exact parse/eval error is shown, fix
it, save, and confirm the query builder immediately reflects the change with
no restart. Separately, create a new model from a template and delete it,
confirming both are reflected on disk.

**Acceptance Scenarios**:

1. **Given** the model editor open on an existing model, **When** the
   developer edits the YAML, **Then** validation re-runs on a debounce and
   reports the same errors the loader would throw (YAML syntax, missing
   keys, or a named measure's invalid expression).
2. **Given** a syntactically valid model, **When** the YAML parses, **Then**
   the editor lists every source column with its dtype — including columns
   that only exist after a join — and clicking one inserts a `pl.col("...")`
   reference at the cursor.
3. **Given** an edited model, **When** the developer saves, **Then** the
   YAML is written to `models/`, the semantic layer hot-reloads, and the
   query builder re-syncs without dropping unrelated in-progress selections.
4. **Given** the editor's create/delete actions, **When** a new model is
   created from the template or an existing one deleted, **Then** the
   corresponding YAML file appears in or is removed from `models/` and the
   model picker reflects it immediately.

### Edge Cases

- What happens when a query requests a dimension or measure the model
  doesn't declare? The API must reject it rather than silently falling back
  to a raw column.
- What happens when a model's YAML fails to parse, or a measure expression
  references a nonexistent column or method? The error must name the
  specific measure and the underlying parse/eval failure, not a generic
  500.
- What happens when a join's source is unreachable or the join key doesn't
  exist on one side? The failure must be attributable to the join, not
  silently drop to an inner-join-of-nothing.
- What happens when the underlying S3 path/glob matches zero objects? The
  model should be loadable but a query against it should fail clearly
  rather than hang or return a false empty success indistinguishable from
  "zero matching rows."
- How does the system behave when two joins would produce a column-name
  collision? Behavior must be deterministic and documented, not
  silently-last-write-wins by accident.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST let a developer declare a model as a single
  YAML file specifying: a name/label, a source (`format`: parquet, csv, or
  delta; `path`: an S3 URI/glob), zero or more `joins`, one or more
  `dimensions`, and one or more `measures`.
- **FR-002**: The system MUST expose to the query builder only the
  dimensions and measures declared in a model — never raw source columns.
- **FR-003**: The system MUST execute every query as a lazy scan against the
  source, applying projection and predicate pushdown so only referenced
  columns and matching row groups are read.
- **FR-004**: The system MUST support `parquet`, `csv`, and `delta` as
  interchangeable source formats with equivalent query semantics.
- **FR-005**: The system MUST support declaring one or more `joins` per
  model, lazily joining lookup sources into the base scan on a specified key
  (`on`, or `left_on`/`right_on`), with joined columns usable in any
  dimension or measure.
- **FR-006**: The system MUST accept a query as {model, dimensions
  (optionally with a time grain), measures, filters, sort, limit} and return
  columns + rows + timing.
- **FR-007**: The system MUST support filter operators: `eq`, `ne`, `gt`,
  `gte`, `lt`, `lte`, `in`, `not_in`, `contains`.
- **FR-008**: Measures MUST be defined as expressions that reduce to one
  value per group (aggregates, ratios of aggregates, conditional
  aggregates), validated at model-load time.
- **FR-009**: The system MUST render query results as at least: bar, line,
  stat tile, and table — with an automatic default chart type chosen from
  the query shape.
- **FR-010**: The system MUST let a user save a query + chart-type
  combination as a named, persisted visual, and reopen it later with
  identical query behavior.
- **FR-011**: The system MUST let a user create a named dashboard, add
  saved visuals to it as tiles, size each tile (at minimum half/full
  width), and persist the layout automatically on change.
- **FR-012**: The system MUST persist visuals and dashboards in a manner
  that survives an application restart (SQLite), independent of the
  underlying data source.
- **FR-013**: The system MUST provide an in-app model editor supporting:
  read, live parse+expression validation (debounced), introspection of the
  source's resolved schema (including post-join columns and dtypes) for
  click-to-insert, save-with-hot-reload, create-from-template, and delete.
- **FR-014**: The system MUST re-sync the query builder's available
  dimensions/measures immediately after any model is created, edited, or
  deleted — no restart required.
- **FR-015**: The system MUST reload all models on startup (or on an
  explicit reload trigger) from the `models/*.yaml` directory.

### Key Entities

- **Model**: A named, YAML-declared semantic unit — a source (format +
  path), optional joins, and the dimensions/measures it exposes. The unit of
  hot-reload and the boundary of what the UI is allowed to query.
- **Dimension**: A groupable field exposed by a model; may be `type: time`
  (gaining grain selection) and may map to a differently-named source
  column.
- **Measure**: A named, formatted (number/currency/percent) polars
  expression that reduces to one value per group.
- **Join**: A declared lookup relationship from a model's base source to
  another source, lazily merged before dimensions/measures are evaluated.
- **Visual**: A persisted {query spec, chart type} pair with a name.
- **Dashboard**: A persisted, named, ordered collection of tiles, each
  referencing a visual and a width.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A new data source becomes queryable in the builder by adding
  one YAML file — zero changes to application code.
- **SC-002**: A grand-totals query against a ~60,000-row source returns in
  under 1 second on a cold start.
- **SC-003**: A saved visual, reopened after an application restart,
  reproduces identical results to when it was saved.
- **SC-004**: A dashboard combining tiles from three different source
  formats (parquet, CSV-joined, Delta) loads and renders every tile
  correctly after a cold reload, with zero console errors.
- **SC-005**: An analyst can go from an empty builder to a correctly
  rendered chart using only the names the model exposes, without needing to
  know underlying column names, file formats, or write any query language.
- **SC-006**: A broken measure expression, introduced through the model
  editor, is reported with the specific measure name and underlying error
  before it can be saved into a state that breaks the builder.

## Assumptions

- Single-user / trusted-developer environment: model YAML is treated as
  trusted configuration (see constitution Principle VI), not
  untrusted user input.
- The embedded S3 emulator (demo mode) or an external S3-compatible
  endpoint (MinIO, real S3) are the only supported source backends; no
  non-S3-protocol source is in scope.
- Authentication/authorization for multiple distinct users is out of scope
  for this spec — that boundary is revisited in
  [004](../004-studio-portal-data-explorer/spec.md) for read-only
  consumption, but general multi-tenant access control remains unaddressed.
- Chart types beyond bar/line/stat/table, cross-filtering, and dashboard
  interactions are deliberately deferred to later specs, not omissions from
  this one.
