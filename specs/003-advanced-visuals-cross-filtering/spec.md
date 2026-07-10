# Feature Specification: Advanced Visualizations & Dashboard Interactions

**Feature Branch**: none — predates this repository's git history; delivered pre-`git init` and landed whole in `f905a73` ("initial commit")

**Created**: 2026-07-09 (retroactively documented 2026-07-10)

**Status**: Implemented (retroactive spec, written from project history after the fact)

**Input**: Verbatim user request: *"Ok this is awesome. I'm off to bed but
while I'm asleep could you 1) add a few more visual types? Sankey chart,
scatter plot, ribbon chart, something geo spatial 2) It would be great to
have visual interactions, so if I click on a specific plan category other
visuals using it would also be filtered (these interactions shouldn't
persist or be cached) 3) a view visual option - a way to expand a visual
from a dashboard and apply some filters which would just be for viewing and
wouldn't be saved to the visual itself 4) an option at the dashboard level
to set the date grain and have this pushed down to all the visuals, even if
these visuals are themselves at different grains. Refreshing the page
should reset to the default (whatever view was selected)"*

## Provenance

Builds on the query builder, chart rendering, and dashboards from
[001](../001-core-bi-platform/spec.md), and the persisted dashboard views
from [002](../002-time-spine-dashboard-views/spec.md). Every interaction
introduced here is explicitly ephemeral (constitution Principle V) — this is
the spec that establishes that pattern as a first-class product concept, in
contrast to 002's explicitly *persisted* views.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Four new chart types (Priority: P1)

Alongside bar/line/stat/table, the builder and dashboards can render
scatter, sankey, ribbon, and geo bubble-map charts, chosen explicitly from
the display options when the query shape supports them.

**Why this priority**: The other three items in this spec are interactions
*on* tiles — they need a richer set of chart types to be worth building
against, and the user listed this first.

**Independent Test**: For each new type, build a query matching its shape
requirement, select it explicitly in DISPLAY, and confirm it renders
correctly against real (non-trivial) data.

**Acceptance Scenarios**:

1. **Given** a query with at least one dimension and two measures, **When**
   scatter is selected, **Then** the two measures plot as x/y, and if a
   second dimension is present it colors the points *and* assigns each
   series a distinct marker shape (not color alone), so the encoding
   remains distinguishable under all-pairs colorblind comparison.
2. **Given** a query with at least two dimensions, **When** sankey is
   selected, **Then** the dimensions become ordered flow stages and the
   first measure sets link width.
3. **Given** a query with a time dimension and a categorical dimension,
   **When** ribbon is selected, **Then** stacked bands render over time and
   visibly re-rank at each bucket where the underlying order changes (lead
   changes read as crossings).
4. **Given** a model dimension configured with `geo: {lat, lon}`, **When**
   geo is selected, **Then** bubbles render at the aggregated coordinates,
   sized by the first measure, over a local (non-network) map outline.
5. **Given** any of the four new types rendered in a container, **When** the
   container is resized (window resize or panel resize), **Then** the chart
   redraws to fit rather than stretching or clipping.

---

### User Story 2 - Ephemeral cross-filtering between tiles (Priority: P2)

Clicking a categorical mark on any tile (a bar, a scatter point, a sankey
node, a ribbon band, a map bubble) filters every other tile on the dashboard
whose model shares that dimension name to the clicked value. Clicking the
same mark again — or a chip showing the active cross-filter — clears it.
None of this is saved.

**Why this priority**: The single richest interaction requested, and the
one with the strongest explicit constraint ("shouldn't persist or be
cached").

**Independent Test**: On a dashboard with tiles from different models
sharing a dimension, click a mark on one tile, confirm every tile whose
model has that dimension filters to the clicked value while unrelated tiles
are untouched, then clear it via the chip and confirm all tiles return to
their pre-click state — then reload and confirm no trace of the cross-filter
remains anywhere persisted.

**Acceptance Scenarios**:

1. **Given** a categorical mark on a tile, **When** it is clicked, **Then**
   every other tile whose model declares a dimension with the same name
   filters to that value; the source tile is visibly marked as the origin
   and affected tiles are visibly marked as targets.
2. **Given** an active cross-filter, **When** the same mark or its chip is
   clicked again, **Then** the cross-filter clears and all tiles return to
   their prior (view-filtered) state.
3. **Given** an active cross-filter, **When** the page is reloaded, **Then**
   no cross-filter is active and no record of it exists in persisted
   dashboard/view state.
4. **Given** a dashboard whose tiles' models share no dimension with the
   clicked one, **When** the mark is clicked, **Then** unrelated tiles are
   left unfiltered rather than erroring.

---

### User Story 3 - Focus mode with throwaway ad-hoc filters (Priority: P3)

Expanding a tile (⤢) opens it full-screen with its own filter bar, starting
from exactly what the tile currently shows (its view filters and any active
grain override). Anything changed inside focus mode is discarded when it
closes.

**Why this priority**: Lets a user drill into one visual without the risk of
accidentally mutating the saved dashboard — valuable, but secondary to
having the chart types and cross-filtering to drill into in the first
place.

**Independent Test**: Expand a tile, change its ad-hoc filters, confirm the
expanded view updates accordingly, close it, and confirm the dashboard tile
and its saved visual are completely unchanged.

**Acceptance Scenarios**:

1. **Given** a dashboard tile, **When** its expand control is used, **Then**
   it opens full-screen pre-populated with the tile's current effective
   filters and grain.
2. **Given** focus mode is open, **When** the user adds or changes a filter
   there, **Then** only the expanded view updates — the underlying tile and
   its saved visual are untouched.
3. **Given** changes made in focus mode, **When** it is closed, **Then**
   nothing from it persists anywhere — reopening the same tile in focus
   mode starts fresh from the tile's real state again.

---

### User Story 4 - Dashboard-level grain override (Priority: P4)

A single GRAIN control in the dashboard's view bar re-buckets every tile's
time dimension(s) at once, regardless of each visual's individually saved
grain, for the duration of the session.

**Why this priority**: A convenience layered on top of the other three —
valuable for comparing tiles at a consistent grain, but the least
structurally complex of the four asks.

**Independent Test**: On a dashboard with tiles saved at different grains,
change the GRAIN control from monthly to quarterly, confirm every
time-bucketed tile re-buckets to quarterly, reload the page, and confirm
every tile reverts to its own saved grain (the view's default), not
quarterly.

**Acceptance Scenarios**:

1. **Given** a dashboard with tiles at mixed saved grains, **When** the
   GRAIN control is changed, **Then** every tile with a time dimension
   re-buckets to the selected grain in one move.
2. **Given** an active grain override, **When** the dashboard state is
   saved or a tile's layout is changed, **Then** the override is excluded
   from whatever gets persisted.
3. **Given** an active grain override, **When** the page is reloaded,
   **Then** every tile reverts to whatever grain its active view specifies
   — the override does not survive a refresh.

### Edge Cases

- What happens when scatter is selected but the query has fewer than two
  measures, or sankey with fewer than two dimensions? The option should be
  unavailable or clearly rejected, not silently render an empty/broken
  chart.
- What happens when a geo dimension has null or missing coordinates on some
  rows? Those rows must be excluded from the map rather than breaking the
  render.
- What happens when focus mode is opened while a dashboard-level
  cross-filter or grain override is already active? The expanded view's
  starting state must reflect both correctly, not just one.
- What happens when a cross-filter is active and the user also changes the
  grain override? Both must apply together without one clobbering the
  other, and both must remain fully ephemeral.
- What happens when more than 8 series would appear in a single chart
  (scatter/ribbon/sankey with a high-cardinality coloring dimension)? The
  tail must fold into an "Other" bucket rather than exhausting the palette
  or becoming unreadable.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST support scatter charts requiring ≥1 dimension
  and exactly 2 measures (x, y), with a second dimension (if present)
  encoding both point color and a distinct marker shape per series.
- **FR-002**: The system MUST support sankey charts where ≥2 dimensions
  become ordered flow stages and the first measure sets link width.
- **FR-003**: The system MUST support ribbon charts (time dimension +
  category) as stacked bands that re-rank at each time bucket.
- **FR-004**: The system MUST support geo bubble-map charts for dimensions
  configured with `geo: {lat, lon}` in the model, sized by the first
  measure, rendered without external map-tile network requests.
- **FR-005**: All chart renderers MUST redraw to fit their container on
  resize (window or panel), not only at initial render.
- **FR-006**: Clicking a categorical mark on any tile MUST filter every
  other tile whose model shares a dimension of the same name to the clicked
  value, and MUST visually distinguish the origin tile from affected target
  tiles.
- **FR-007**: Clicking the same mark again, or its cross-filter chip, MUST
  clear the cross-filter and restore all tiles to their pre-cross-filter
  state.
- **FR-008**: Cross-filter state MUST be held only in memory for the
  current session — it MUST NOT be written to any persisted visual,
  dashboard, or view record.
- **FR-009**: Expanding a tile MUST open a full-screen view seeded from
  that tile's current effective filters (view filters + any active grain
  override) with its own ad-hoc filter bar.
- **FR-010**: Changes made inside focus mode MUST NOT alter the underlying
  tile, its saved visual, or the dashboard in any way, and MUST NOT persist
  after focus mode is closed.
- **FR-011**: A dashboard-level grain control MUST re-bucket every tile's
  time dimension(s) to the selected grain, overriding each tile's
  individually saved grain for the current session only.
- **FR-012**: Grain overrides MUST be excluded from any persisted dashboard
  or view payload, and MUST reset to the active view's saved grain on
  reload.
- **FR-013**: Any chart with more than 8 distinguishable series MUST fold
  the excess into a single "Other" series rather than rendering all of
  them.

### Key Entities

- **Cross-filter**: An in-memory-only {dimension name, value, origin tile}
  triple applied to every tile whose model shares that dimension.
- **Focus session**: A transient, per-tile ad-hoc filter context that
  exists only while a tile is expanded.
- **Grain override**: A transient, dashboard-scoped grain selection that
  overrides (without replacing) each tile's saved grain for the session.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All four new chart types render correctly against real,
  non-trivial data (verified in-browser, not just unit-tested).
- **SC-002**: A cross-filter set-then-clear round trip leaves every tile's
  displayed values identical to before the interaction began.
- **SC-003**: After exercising cross-filter, focus mode, and grain override
  together in a single session, a page reload leaves zero trace of any of
  the three — saved views, filters, and layout are exactly as they were
  before the session's ephemeral interactions.
- **SC-004**: A full regression pass across every tile type (old and new)
  on one dashboard completes with zero browser console errors.

## Assumptions

- "Visual interactions" is scoped to categorical marks; continuous-value
  interactions (e.g. brushing a range on an axis) are out of scope.
- The vendored world outline used for geo charts is a fixed, local asset —
  keeping the map fully local (no external tile server) is a deliberate
  constraint, not a temporary shortcut.
- Grain override and cross-filter are dashboard-session-scoped, not
  per-tile-scoped independently — they apply dashboard-wide by design.
