# Feature Specification: Time-Spine Analytics, Dashboard Views & Scale Validation

**Feature Branch**: none — predates this repository's git history; delivered pre-`git init` and landed whole in `f905a73` ("initial commit")

**Created**: 2026-07-09 (retroactively documented 2026-07-10)

**Status**: Implemented (retroactive spec, written from project history after the fact)

**Input**: Verbatim user request (three numbered roadmap items, tackled
together): *"1) complex expressions - are we currently able to handle
something like an 'active customer' where the table the measure needs to
compute from has a start and end date and you're counting all the records
within a window of time that fall between those dates - and visualising
along a timeline? ... 2) ability to save a list of features to a dashboard as
a 'view'. The user could select between multiple saved views. 3) I am
curious what performance is like over quite large fact tables (>10million
records). Are there any public datasets that we could test this against?"*
— followed by a clarifying correction on item 2: *"I don't want each view to
be its own tile list - I want to just be able to save a set of filters to a
dashboard which get pushed down to each visual."*

## Provenance

Builds directly on the semantic layer and query builder from
[001](../001-core-bi-platform/spec.md). All three items shipped together in
one pass. Item 2's initial interpretation (a view = its own tile list) was
corrected by the user mid-flight to "a view = a saved filter set pushed down
to existing tiles" — the acceptance criteria below reflect the corrected,
shipped behavior only.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Point-in-time "active" measures via a time spine (Priority: P1)

A BI developer marks a time dimension as a **spine** (declaring its start and
end columns) on a model where each row represents an interval (e.g. a
subscription's active period). An analyst then groups by that dimension at
any grain and sees a real timeline: each row contributes to *every* bucket
it was active for, not just the bucket its start date falls in.

**Why this priority**: This was previously impossible — a plain group-by
puts each row in exactly one bucket — and was the most technically novel of
the three items. It unblocks an entire class of question ("active
customers," "concurrent X," "MRR over time") that interval data can't answer
without it.

**Independent Test**: Seed a dataset where rows have `start_date` and
nullable `end_date`, mark the timeline dimension as a spine, group a
count-distinct measure by it at monthly grain, and confirm a row with
`start=Jan, end=Mar` appears in the Jan, Feb, and Mar buckets, not just Jan.

**Acceptance Scenarios**:

1. **Given** a time dimension declared with `spine: {start: start_date, end:
   end_date}`, **When** an analyst groups a measure by that dimension at a
   chosen grain, **Then** the engine generates a timeline at that grain and
   interval-joins it against `[start_date, end_date]`, so each source row
   counts in every bucket it was active for.
2. **Given** a row whose `end_date` is null, **When** it is evaluated
   against the spine, **Then** it is treated as still active in every bucket
   from its `start_date` through the latest generated bucket.
3. **Given** a spine dimension also split by a categorical dimension (e.g.
   plan), **When** the query runs, **Then** results support multiple
   series (one line per category) over the same timeline.
4. **Given** a range filter (`>=`, `<=`) on the spine dimension, **When** the
   query runs, **Then** the filter bounds the generated timeline window
   rather than filtering rows by their raw start/end columns.
5. **Given** an equality filter (`=`) on the spine dimension with no other
   grouping, **When** the query runs, **Then** it returns a single
   point-in-time snapshot count as of that date.
6. **Given** a bucket with zero active rows, **When** results are returned,
   **Then** that bucket is omitted rather than returned as a zero-value
   point.

---

### User Story 2 - Save a dashboard-level filter set as a named view (Priority: P2)

A user sets filters in a dashboard's view bar; those filters push down into
every tile on the dashboard whose underlying model has a matching dimension
(matched by dimension name, so one filter can drive tiles from different
models at once). The user can snapshot the current filters as a new named
view and switch between saved views from a dropdown.

**Why this priority**: Turns a dashboard from "here are some tiles" into
"here's the same dashboard, sliced the way different audiences need it" —
without duplicating tiles per audience.

**Independent Test**: On a dashboard with tiles from two different models
that share a `region` dimension, set a `region = X` filter in the view bar,
confirm both tiles' totals change accordingly, save it as a new view, switch
to a different (or the default, unfiltered) view, and confirm the tiles
revert — then reload the page and confirm both views and the active
selection persisted.

**Acceptance Scenarios**:

1. **Given** a dashboard with tiles from models that share a dimension name,
   **When** a filter for that dimension is set in the view bar, **Then**
   every tile whose model has that dimension is filtered accordingly, and
   affected tiles are visibly marked.
2. **Given** an edited filter set, **When** the change is made, **Then** it
   auto-saves into the currently active view (no explicit save step
   required to not lose the edit).
3. **Given** a satisfactory filter set, **When** the user chooses "new
   view" and names it, **Then** the current filters are snapshotted under
   that name and the view dropdown offers it going forward.
4. **Given** multiple saved views on a dashboard, **When** the user switches
   between them, **Then** the tiles' filters update to match instantly.
5. **Given** an existing dashboard predating this feature, **When** it is
   opened after this feature ships, **Then** it is migrated to a single
   default view with no loss of its existing tile layout.
6. **Given** saved views and an active selection, **When** the application
   is restarted and the dashboard reopened, **Then** the same views exist
   and the same view is active.

---

### User Story 3 - Validate query performance at real fact-table scale (Priority: P3)

A BI developer loads a large, real-world fact table (not synthetic toy data)
through the normal source pipeline and confirms representative queries
(grand totals, a trend, a filtered breakdown) stay comfortably interactive.

**Why this priority**: Confidence that the lazy/pushdown architecture holds
up outside small demo datasets is a prerequisite for trusting the tool with
real fact tables — without it, every other feature is unproven at scale.

**Independent Test**: Load a >10M-row public dataset through the standard
model/source pipeline (no special-cased fast path), run the same query
shapes used elsewhere in the product against it, and record cold/warm
timings.

**Acceptance Scenarios**:

1. **Given** a >10M-row parquet dataset loaded as an ordinary model source,
   **When** a grand-totals query runs against it, **Then** it completes in
   low-single-digit seconds cold and faster warm, using the same query path
   as any other model.
2. **Given** the same dataset, **When** a monthly-trend and a
   filtered-daily-trend query run, **Then** both complete interactively
   (sub-3-second) without any dataset-specific engine changes.
3. **Given** the loaded dataset contains real-world data-quality noise (e.g.
   out-of-range dates), **When** a filter is applied to clean the range,
   **Then** the filter behaves identically to any other filter and the
   noise is excluded.

### Edge Cases

- What happens when a spine dimension is combined with another spine
  dimension in the same query? Only one spine dimension is supported per
  query — this must be rejected or clearly constrained, not silently
  produce a cross-join.
- What happens when the spine's `end` column value is before its `start`
  value (bad data)? Behavior should be defined (e.g. treated as never
  active) rather than crashing the query.
- What happens when a view's filter references a dimension that only some
  dashboard tiles' models have? Only the tiles whose model has that
  dimension are affected; others are left unfiltered, not errored.
- What happens when two users (or two browser tabs) edit the same
  dashboard's active view concurrently? Last-write-wins is acceptable but
  must not corrupt the views list.
- What happens when the large-dataset load is interrupted partway (partial
  download/seed)? Restart must not leave the model half-seeded and
  silently wrong.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A time dimension MUST be declarable as a spine via `spine:
  {start: <column>, end: <column>}`, where a null end means "still active."
- **FR-002**: Grouping by a spine dimension at a given grain MUST generate a
  timeline at that grain and interval-join it against each row's
  `[start, end)` window, so a row is counted in every bucket it was active
  for.
- **FR-003**: Spine semantics MUST be "active as of bucket start," and
  buckets with zero active rows MUST be omitted from results.
- **FR-004**: A spine dimension MUST compose with an additional categorical
  grouping dimension to produce multiple series over the same timeline.
- **FR-005**: Range filters (`>=`, `<=`) on a spine dimension MUST bound the
  generated timeline window; an equality filter (`=`) with no other
  grouping MUST return a single point-in-time snapshot.
- **FR-006**: The system MUST support at most one spine dimension per
  query.
- **FR-007**: A dashboard MUST support one or more named **views**, where a
  view is a saved set of filters (not a saved tile list).
- **FR-008**: View filters MUST push down to every tile on the dashboard
  whose underlying model declares a dimension with a matching name;
  affected tiles MUST be visibly indicated.
- **FR-009**: Editing the active view's filters MUST auto-save into that
  view; creating a new view MUST snapshot the current filters under a new
  name without altering other saved views.
- **FR-010**: Switching the active view MUST update all affected tiles
  immediately and MUST persist the choice of active view across reloads.
- **FR-011**: Dashboards created before this feature MUST migrate
  automatically to a single default view, preserving their existing tile
  layout.
- **FR-012**: The system MUST provide a reproducible way to load a
  >10-million-row public dataset through the ordinary model/source pipeline
  (no bespoke fast path) for performance validation.
- **FR-013**: Representative query shapes (grand totals, trend, filtered
  breakdown) run against the large dataset MUST be measured and the timings
  recorded for both cold and warm execution.

### Key Entities

- **Spine**: A start/end column pair attached to a time dimension, defining
  the interval-join behavior used to generate timeline buckets.
- **View**: A named, persisted filter set attached to a dashboard; exactly
  one view is active at a time.
- **Benchmark dataset**: A large, real-world fact table loaded through the
  standard source pipeline for the sole purpose of validating performance
  claims — not a product-facing entity.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An "active customers" style question — count of distinct
  entities active in each period of a timeline — is answerable by declaring
  a spine, with no bespoke query code.
- **SC-002**: Changing a dashboard's active view changes every affected
  tile's displayed values correctly, and switching back restores the
  original values exactly.
- **SC-003**: Saved views and the active selection survive an application
  restart with no loss or corruption.
- **SC-004**: A grand-totals query against a real fact table of at least 10
  million rows returns in under 1 second cold.
- **SC-005**: A filtered trend query against the same dataset stays under 3
  seconds cold, without any dataset-specific optimization.

## Assumptions

- "Large fact table" validation uses a public dataset (NYC TLC yellow-taxi
  trip records) as a stand-in for a real customer fact table; no
  customer data is involved.
- Spine timelines are generated in-memory per query; pre-materialized/
  cached timelines are out of scope.
- View filters use the same operators and matching-by-dimension-name
  behavior as [001](../001-core-bi-platform/spec.md)'s query filters — no
  new filter semantics are introduced.
- Ephemeral, non-persisted dashboard interactions (cross-filter, focus mode,
  grain override) are a separate concern, covered in
  [003](../003-advanced-visuals-cross-filtering/spec.md); views in this spec
  are explicitly **persisted**, per constitution Principle V.
