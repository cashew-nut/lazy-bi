# Feature Specification: Instant Cross-Filter (Client-Side Re-Aggregation)

**Feature Branch**: `016-instant-cross-filter`

**Created**: 2026-07-31

**Status**: Draft

**Input**: User description: "Bring MotherDuck-style instant, round-trip-free
cross-filtering to dashboards: when a dashboard is marked 'instant', each
tile's initial load fetches its full grain-appropriate extract through the
existing semantic-layer /query contract (server decides what leaves, exactly
as today — no new data-access surface), transported as Arrow IPC instead of
JSON, and every subsequent cross-filter click, grain toggle, or focus change
on that dashboard re-aggregates client-side using Perspective's WASM engine —
zero /query calls until the dashboard reloads or a static filter/parameter
actually changes. Falls back automatically, per tile, to today's
round-trip-per-interaction behavior when a tile's extract would exceed a size
cap, or for anything outside saved dashboards (builder, explorer, notebook
cells, sandbox). Perspective supplies aggregation only — all rendering stays
on the existing hand-rolled SVG chart renderers; no perspective-viewer, no new
chart library. This is the project's first external frontend dependency, so
it is vendored (not CDN-loaded) and lazy-loaded only by dashboard views that
opt in."

## Constitution Notes

This feature deliberately touches one Core Principle and the frontend
Technology Constraint, and both are called out here per the governance rule
("where a feature genuinely needs to violate one, say so explicitly").

- **Technology Constraints — Frontend (re-opened)**: the constitution states
  the frontend is "hand-rolled SVG charts and vanilla ES modules... no
  bundler, no framework, no build step — a deliberate simplicity choice, not
  an oversight." This feature adds the project's **first external frontend
  dependency**: Perspective (`@finos/perspective`, FINOS/Apache-2.0), used
  strictly as a headless columnar aggregation engine. It does **not** violate
  the spirit of the constraint the way a framework or bundler would: no
  build step is introduced (Perspective ships importable ES modules + a
  `.wasm` binary, loaded natively by the browser exactly like every other
  module in `app/static/js/`), no rendering responsibility moves off the
  existing hand-rolled chart renderers, and the dependency is vendored into
  `app/static/vendor/` rather than fetched from a CDN, preserving the
  single-Docker-image, no-external-calls packaging posture every other
  feature has maintained. It is also lazy-loaded: only a dashboard with
  `instant: true` ever fetches the module. The constitution's Technology
  Constraints section should be amended to record this exception when the
  feature ships, the same way Principle VI has been amended twice before.
- **Principle II (lazy evaluation, pushdown by default)**: touched, not
  violated, and worth stating precisely. An instant tile's initial fetch is
  *coarser* than today's per-render aggregate — it must include, as columns,
  every dimension used anywhere on the dashboard for cross-filtering (see
  FR-006), not just the tile's own displayed dimensions — in exchange for
  zero re-aggregation round-trips afterward. This is still a semantic-layer
  query with full predicate/projection pushdown against the bucket (nothing
  about `scan()` changes); the trade is a wider *result set*, not a
  full-table materialization, and it is capped and benchmarked, not assumed
  free (FR-009, SC-002). Per this principle's own evidentiary bar, the cap
  default proposed here must be validated against the existing 13M-row NYC
  taxi benchmark before shipping (see `research.md` R5).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Cross-filter a dashboard with zero round-trips (Priority: P1)

A dashboard author opens an existing dashboard's settings and turns on
**Instant mode**. On load, each tile below the size cap fetches its extract
once. The author then clicks a bar on one tile to cross-filter the rest of
the dashboard, the way they already can today — but every other instant tile
updates immediately, with no network request and no loading state, because
the new value is re-aggregated from data already sitting in the browser.
Toggling the cross-filter off, changing the session-only grain override, and
opening focus mode all behave identically to today from the user's point of
view — just without the wait.

**Why this priority**: This is the entire value proposition. Every other
capability in this feature (fallback, visibility into mode) exists to make
this safe and honest, not to add new behavior of its own.

**Independent Test**: Enable instant mode on a dashboard with at least three
tiles sharing a common dimension. Open the browser network panel. Click a
bar to set a cross-filter; verify the other tiles' values update and that no
`/query` (or the new extract endpoint) request fires. Clear the cross-filter,
click a different value, repeat. Reload the page and confirm the dashboard
returns to its un-filtered state with fresh extracts (cache is not persisted
across loads).

**Acceptance Scenarios**:

1. **Given** a dashboard with `instant: true` and all tiles under the size
   cap, **When** the dashboard is opened, **Then** each tile issues exactly
   one extract request on load and renders from it.
2. **Given** an instant dashboard already loaded, **When** the user clicks a
   value to set a cross-filter, **Then** every other tile's display updates
   to the filtered result within one interaction frame and issues zero
   network requests.
3. **Given** an active cross-filter, **When** the user clicks the same
   value again (or the chip's clear control), **Then** all tiles revert to
   their unfiltered extract locally, with zero network requests.
4. **Given** an instant dashboard, **When** the user changes the session-only
   dashboard grain override to a *coarser* grain than the extract was
   fetched at, **Then** tiles re-aggregate locally; **when** they choose a
   *finer* grain than the extract holds, **Then** the affected tile(s) fall
   back to a live re-fetch at the new grain for that interaction only (see
   FR-007).
5. **Given** a static (non-cross) filter or a visual parameter is changed on
   an instant tile, **When** the change is applied, **Then** the tile
   re-fetches its extract (the cached data can no longer answer the
   question) and the fresh extract becomes the new local cache.

---

### User Story 2 - Silent, per-tile fallback keeps every dashboard safe (Priority: P2)

A dashboard mixes a handful of small lookup-style tiles with one tile over a
large, high-cardinality fact. Instant mode is on for the dashboard. The small
tiles get the instant treatment; the large tile's extract comes back over the
configured size cap, so that tile is silently kept in today's live,
round-trip-per-interaction mode — cross-filtering it, or being cross-filtered
by a click that originates elsewhere, works exactly as it does today, with no
error, no broken state, and no visible difference except the lack of the
instant speed-up.

**Why this priority**: This is what makes the feature safe to turn on by
default expectation rather than something an author has to reason carefully
about per tile. Without a bulletproof fallback, instant mode is a footgun on
any dashboard an author didn't build with cache size in mind.

**Independent Test**: Build a dashboard with one tile over the 13M-row taxi
benchmark at a high-cardinality grain (expected to exceed the cap) alongside
two small tiles. Enable instant mode. Verify the small tiles report "instant"
status and the large tile reports "live" status (US3), that cross-filtering
from a small tile still updates the large tile via a real `/query` call, and
that the large tile never causes a fetch failure, oversized payload, or
frozen tab.

**Acceptance Scenarios**:

1. **Given** a tile whose extract would exceed the configured row/byte cap,
   **When** the dashboard loads, **Then** that tile is not fetched as an
   extract at all — it runs its existing live query path unchanged.
2. **Given** a mixed dashboard (some tiles instant, some live), **When** a
   cross-filter is set from any tile, **Then** instant tiles re-aggregate
   locally and live tiles issue their existing `/query` round-trip, with the
   same filter applied to both.
3. **Given** instant mode is on, **When** the extract endpoint itself errors
   or times out for a tile, **Then** that tile falls back to the live query
   path for the remainder of the session rather than retrying the extract
   fetch repeatedly or showing a broken tile.
4. **Given** a dashboard with `instant: false` (the default), **When** it is
   opened, **Then** behavior is byte-for-byte identical to the platform
   today — no extract requests, no Perspective module loaded.

---

### User Story 3 - Author can see and trust which mode each tile is in (Priority: P3)

While instant mode is on, a dashboard author (or any viewer) can tell at a
glance which tiles are running instant versus live, and roughly how much data
an instant tile pulled — so a slow-feeling tile is explainable, and an author
deciding whether instant mode is worth it for a given dashboard has real
numbers instead of a guess.

**Why this priority**: Builds on US1/US2; it is a trust and debuggability
layer, not new query behavior, so it can ship last without blocking the
core win.

**Independent Test**: With the mixed dashboard from US2, verify a per-tile
badge or tooltip distinguishes instant (with row count / extract size) from
live tiles, and that this is visible to a read-only portal viewer, not just
the authoring view.

**Acceptance Scenarios**:

1. **Given** an instant tile, **When** its badge/tooltip is inspected,
   **Then** it shows "instant" plus the extract's row count and approximate
   size.
2. **Given** a live (fallen-back) tile on an instant dashboard, **When** its
   badge/tooltip is inspected, **Then** it shows "live" and, where the
   fallback was cap-triggered, the cap that was exceeded.
3. **Given** a portal (published, read-only) viewer, **When** they view an
   instant dashboard, **Then** the same per-tile mode indicators are visible
   without any authoring controls.

---

### Edge Cases

- A cross-filter's `field` does not exist on a given tile's model (today's
  existing multi-model dashboard behavior: the filter is a silent no-op for
  that tile). Instant mode preserves this exactly — a tile's extract simply
  has no column to filter on, so nothing changes for that tile.
- A composite (multi-fact) model tile: the extract fetch goes through the
  existing composite merge path server-side (`_run_composite`) unchanged;
  the client only ever receives the already-merged result, identically to
  the JSON path today — no new merge logic on the client.
- Two or more cross-filters could theoretically compound (click a value on
  tile A, then a different field's value on tile B) — this already matches
  existing `state.crossFilter` semantics (single active cross-filter,
  keyed by source tile) and instant mode does not change that model; it is
  explicitly out of scope to introduce multi-field compounded cross-filters
  as part of this feature.
- Dashboard reloaded mid-interaction (browser refresh while a cross-filter is
  active): cross-filter state is already ephemeral and resets on reload
  (Principle V); instant extracts reset with it — nothing new persists.
- A tile's extract fetch succeeds but returns zero rows (e.g., a filter that
  matches nothing): the tile renders its existing empty-result state locally,
  no fallback triggered.
- The Perspective module fails to load (network hiccup on first vendor asset
  fetch, unsupported browser): the dashboard degrades to fully live mode for
  the session — every tile behaves as if `instant: false` — rather than
  failing to render.
- An author turns instant mode on for a dashboard that is also published to
  the portal: published/portal viewers get the same instant behavior; there
  is no separate portal-only mode.

## Requirements *(mandatory)*

### Functional Requirements

**Wire contract & data access**

- **FR-001**: A new endpoint MUST accept the same query shape as the existing
  `POST /query` (`QueryRequest`) and return the result as an Arrow IPC stream
  instead of JSON, using the identical model-resolution, authorization, and
  engine execution path (`engine.run_query`'s underlying `_run_single` /
  `_run_composite`) as `/query` today. `POST /query` itself MUST NOT change
  for any existing caller (builder, explorer, notebook, sandbox, chat).
- **FR-002**: The extract endpoint MUST enforce the identical semantic-layer
  boundary as `/query` today (Principle I) — it resolves a model and returns
  only declared dimensions/measures already permitted through that model; it
  introduces no new access to raw source columns or bucket objects.
- **FR-003**: The extract endpoint MUST require the same authentication/role
  checks as `/query`. Instant mode grants no additional data access to any
  role.

**Client-side engine & rendering**

- **FR-004**: The client MUST use Perspective (`@finos/perspective`) purely
  as a headless aggregation engine (`Table` + `View`), never
  `perspective-viewer` or any Perspective-supplied rendering/UI component.
- **FR-005**: The adapter that reads a Perspective `View`'s output MUST
  produce data in the same `{columns, rows}` shape the existing chart
  renderers (`app/static/js/charts/*.js`) already consume from `/query`
  today, so no chart renderer requires any code change.
- **FR-006**: An instant tile's extract MUST be projected on: the tile's own
  configured dimensions and measures, **union** every dimension used by any
  *other* tile on the same dashboard that exists on this tile's model (so
  any cross-filter originating elsewhere can be applied locally) — but not
  other tiles' measures. Dimensions absent from this tile's model are
  skipped, matching today's no-op cross-filter behavior for mismatched
  models.
- **FR-007**: A cross-filter, focus-mode change, or grain-override change
  that can be answered from an already-fetched extract (same or coarser
  grain, filter values that are a subset of what was fetched) MUST be
  answered locally with zero network calls. A change that cannot — a finer
  grain, a new static filter, or a changed visual parameter — MUST trigger a
  real re-fetch of that tile's extract, replacing the local cache.

**Fallback & mode**

- **FR-008**: Instant mode MUST be an explicit, persisted, opt-in flag on a
  dashboard (default `false`); dashboards without it enabled MUST behave
  identically to the platform today, including never loading the Perspective
  module.
- **FR-009**: Fallback from instant to live MUST be evaluated **per tile**,
  not per dashboard, against a configured row-count and/or byte-size cap
  measured on the actual extract response; a tile whose extract exceeds
  either cap is discarded and that tile runs the existing live `/query` path
  for the remainder of the session. A dashboard MAY end up with a mix of
  instant and live tiles simultaneously.
- **FR-010**: A tile whose extract request errors, times out, or whose
  Perspective table fails to construct MUST fall back to the live path for
  the remainder of the session rather than retrying or rendering a broken
  state.
- **FR-011**: The Perspective module (JS + `.wasm` assets) MUST be
  vendored under `app/static/vendor/` and loaded via a dynamic `import()`
  gated on a dashboard's `instant` flag — never eagerly loaded on app boot,
  never fetched from a CDN.

**Visibility**

- **FR-012**: Every tile on an instant-mode dashboard MUST expose its current
  mode (instant/live), and for instant tiles, the extract's row count and
  approximate byte size, visible to authors and to read-only portal viewers.
- **FR-013**: A tile that fell back due to a size cap MUST indicate which cap
  was exceeded, so an author can distinguish "this tile is just big" from a
  genuine error.

**Scope boundary**

- **FR-014**: This feature applies only to saved Dashboards (`dashboard.js`
  / the dashboard view). The Builder, Explorer, Notebook composer cells, and
  Sandbox notebooks are explicitly out of scope for v1 and MUST NOT load the
  Perspective module or the extract endpoint.
- **FR-015**: No backend Python dependency is introduced. The extract
  endpoint MUST use Polars' native IPC writer (`write_ipc` /
  `write_ipc_stream`); it must not take a hard dependency on `pyarrow` even
  though it is already available transitively via `pyiceberg`.

### Key Entities

- **Extract**: an in-memory, per-tile Arrow table held client-side as a
  Perspective `Table`, built from one extract-endpoint response. Ephemeral —
  never persisted (IndexedDB, localStorage, or otherwise), rebuilt on every
  page load, discarded on any change FR-007 says forces a re-fetch, and
  discarded permanently for a tile that falls back per FR-009/FR-010.
- **Instant flag**: a persisted boolean on a dashboard's saved config
  (alongside its existing tiles/views), defaulting to `false`.
- **Mode**: per-tile, in-memory, session-only derived state — `instant` or
  `live` — never persisted, recomputed on every load per FR-009/FR-010.
- **Size cap**: a configured row-count and/or byte-size threshold applied
  per tile's extract response (see `research.md` R5 for the proposed
  starting default and required benchmark).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On an instant-mode dashboard where all tiles are under the size
  cap, cross-filter, grain-override (coarser), and focus-mode interactions
  after the initial load issue **zero** `/query` or extract-endpoint network
  requests, verified via browser network panel per Principle IV.
- **SC-002**: The proposed size-cap default is validated against the
  existing 13M-row NYC taxi benchmark dataset before shipping: at least one
  realistic high-cardinality tile over that dataset is confirmed to exceed
  the cap and fall back cleanly (US2), and at least one realistic
  lower-cardinality rollup over the same dataset is confirmed to fit and
  deliver the instant experience.
- **SC-003**: Dashboards with `instant: false` (the default and status quo
  for every dashboard that exists before this feature ships) are
  behaviorally and network-trace identical to today — zero regression,
  zero Perspective module load.
- **SC-004**: Local re-aggregation after a cross-filter click completes
  within a single interaction frame (target: comparable to or faster than a
  typical `elapsed_ms` reported by today's `/query` responses for the same
  tiles, measured on the same hardware) — the qualitative win MotherDuck
  demos ("instant" re-slicing) is reproduced, not just claimed.
- **SC-005**: Every existing chart renderer (`bar`, `line`, `pivot`, `table`,
  `scatter`, `sankey`, `geo`, `ribbon`, `stat`, `frame`) renders correctly
  from an instant tile's locally re-aggregated data with **zero** code
  changes to `app/static/js/charts/*.js`.
- **SC-006**: A dashboard author can determine, without opening dev tools,
  which tiles on their dashboard are instant vs. live, and why (US3),
  100% of the time instant mode is on.

## Assumptions

- **Engine choice**: Perspective (FINOS, WASM, Apache-2.0), used headless —
  not a DIY Polars-to-`wasm32` build and not DuckDB-Wasm. See `research.md`
  R1 for the alternatives considered and why.
- **Wire format**: Arrow IPC stream for the new extract endpoint only; the
  existing `/query` JSON contract is untouched for every other caller. See
  `research.md` R2.
- **No `apache-arrow` JS package**: Perspective ingests an Arrow IPC
  `ArrayBuffer` natively; the client does not need the separate
  `apache-arrow` npm package unless a future feature needs to manipulate
  Arrow data outside Perspective. See `research.md` R2.
- **Grain boundary**: an extract is fetched at the dashboard's currently
  configured (default) grain; coarser local roll-ups are answered from
  cache, finer ones force a re-fetch. See `research.md` R3.
- **Vendoring, not CDN**: consistent with the fact that no part of this
  codebase currently loads any asset from a CDN (this would be the first),
  and with the single-Docker-image, self-contained packaging posture. See
  `research.md` R4.
- **Default-off**: instant mode is opt-in per dashboard; nothing about
  existing dashboards' behavior changes unless an author turns it on.
- **Out of scope for v1**: Builder/Explorer ad hoc queries, Notebook
  composer cells, Sandbox notebooks, multi-field compounded cross-filters,
  and any cross-session/cross-user persistence of the client-side cache.
- **README** is updated as part of this feature (Development Workflow), and
  the Technology Constraints exception is recorded in the constitution when
  it ships, consistent with how the pipeline-module and safe-measure-
  compilation features recorded their own amendments.
