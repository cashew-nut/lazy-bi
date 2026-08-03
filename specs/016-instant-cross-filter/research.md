# Research: Instant Cross-Filter

Decisions resolving the open technical choices behind spec.md. These
front-load the architecture discussion so `/speckit-plan` has a settled
foundation rather than re-deriving it from the MotherDuck comparison that
motivated this feature.

## R1 — Client engine is Perspective, headless; not a DIY Polars→WASM build, not DuckDB-Wasm

**Decision**: Use `@finos/perspective`'s `Table`/`View` API purely as a
client-side groupby/aggregate/filter engine. Never load `perspective-viewer`
or any Perspective rendering component — the existing hand-rolled SVG chart
renderers stay the entire rendering layer, unchanged.

**Rationale**: The three realistic options for "run aggregation in the
browser" are (a) compile Polars itself to `wasm32`, (b) adopt DuckDB-Wasm as
a second query engine alongside the server's Polars engine, or (c) adopt an
existing WASM analytics engine purpose-built for exactly this interaction
pattern. Polars' core is Rust, which compiles to WASM well in principle, but
there is no maintained package, hosted SDK, or dual-execution planner —
building one would mean owning a from-scratch WASM build pipeline for an
engine that isn't even the one running server-side query authorization,
for a project that has taken on zero frontend dependencies to date. DuckDB-
Wasm is exactly what MotherDuck itself uses, and is the most mature option
technically, but it means running a second, semantically-independent SQL
engine parallel to the server's Polars engine — the same "two engines to
keep consistent" complexity this project has already rejected once (live DB
connectivity and multi-format semantic model support were deliberately
scoped out of lazy-bi for this reason). Perspective is purpose-built for
"take a columnar extract, let the browser groupby/pivot/filter it
instantly" — it is the FINOS (formerly J.P. Morgan) open-source engine
built for exactly this dashboard-cross-filter pattern, is actively
maintained, ships as native ES modules + a `.wasm` binary with no bundler
requirement, and its `Table`/`View` API is a thin, swappable layer — it
never touches how a tile renders. That keeps the actual footprint of "first
external dependency" as narrow as it can be: one aggregation engine, zero
new rendering surface.

**Alternatives considered**: DIY `polars`-core compiled to `wasm32`
(technically possible — Rust is the best-supported WASM source language —
but no maintained SDK exists; would require patching missing crates for the
`wasm32` target, hosting/versioning the build ourselves, and still writing
the query-routing logic MotherDuck's planner provides for free — a much
larger and more fragile undertaking for a personal project than adopting a
maintained library). DuckDB-Wasm / a MotherDuck-shaped hybrid (rejected —
introduces a second engine with its own semantics to keep in sync with
Polars, which is the same category of complexity already rejected for live
DB connectivity). Arrow.js (`apache-arrow`) alone (rejected as the *engine*
— it is a columnar format/IPC library with some vector operations, not an
aggregation engine; using it directly would mean hand-writing groupby/
aggregate/filter logic against raw Arrow vectors, reinventing what
Perspective already does well — see R2 for Arrow's actual role as the wire
format).

## R2 — Wire format is Arrow IPC for the new extract endpoint only; `apache-arrow` is not a required client dependency

**Decision**: Add one new endpoint that runs the identical query path as
`/query` but serializes the result with Polars' native `write_ipc_stream`
(Arrow IPC streaming format) instead of `df.write_json()`, returned as raw
bytes. The existing `POST /query` endpoint and its JSON contract are
untouched — every other caller (builder, explorer, chat, notebooks,
sandbox) keeps working exactly as today. On the client, the response
`ArrayBuffer` is handed directly to `perspective.table()`, which reads
Arrow IPC natively; the separate `apache-arrow` npm package is not added as
a dependency, since nothing outside Perspective needs to inspect or
manipulate an Arrow object directly — Perspective's `view.to_columns()` /
`to_json()` output is what feeds the FR-005 adapter, and that output is a
plain JS object, not an Arrow structure.

**Rationale**: The performance case for switching wire formats only exists
because instant-mode extracts are deliberately wider than a normal `/query`
response (FR-006's dimension union) — JSON's per-row object overhead and
text-based numeric encoding cost real time to both serialize server-side
and parse client-side at that scale, and Arrow IPC gives a typed, columnar,
near-zero-copy handoff straight into Perspective's WASM heap. This costs
nothing new on the backend: Polars writes Arrow IPC natively (no `pyarrow`
requirement — see FR-015), and `pyarrow` happens to already be present
transitively via `pyiceberg` if it were ever needed, so there is no new
Python dependency either way.

**Alternatives considered**: Keep JSON for the extract endpoint too
(rejected — simplest, zero backend change, but gives up a meaningful chunk
of the latency win this feature exists for once extracts run into the tens
of thousands of rows; also loses the typed-column zero-copy handoff into
Perspective). Add `apache-arrow` as an explicit client dependency (rejected
for v1 — no code outside Perspective needs to construct or walk an Arrow
`Table` object; would be a second dependency solving a problem Perspective's
built-in reader already solves. Revisit only if a future feature needs
client-side Arrow manipulation independent of Perspective, e.g. exporting a
raw extract).

## R3 — Extract grain boundary: fetched at the dashboard's configured grain; coarser answered locally, finer forces a re-fetch

**Decision**: An instant tile's extract is fetched at whatever grain the
dashboard's dimensions are currently configured at (the same grain `/query`
would use today). A session-only `dashGrain` override (the existing
ephemeral state in `state.js`) that requests a *coarser* bucket than the
extract holds is answered by a local Perspective `View` that groups the
cached rows up further. An override requesting a *finer* bucket than what
was fetched cannot be answered from the cache — Polars never sent
sub-grain detail to the browser — and triggers a real re-fetch at the new
grain, replacing the cached extract for that tile going forward this
session.

**Rationale**: This is the one place where "cache the extract" and the
existing `dashGrain` feature (already listed in the constitution as
deliberately ephemeral, session-only state) interact non-trivially. Fetching
every extract at the finest grain the model could ever produce would remove
the finer-grain edge case entirely, but would also blow through the size
cap on every tile, defeating the feature. Fetching at the dashboard's
*current* grain and falling back to a real query only when the user asks
for something genuinely finer keeps the common case (grain unchanged, or
coarsened) instant while keeping extract sizes bounded to what the
dashboard is actually showing.

**Alternatives considered**: Always fetch at the finest configured grain
regardless of current display (rejected — directly undermines the size cap
that makes the feature safe, per Principle II's own "don't materialize more
than needed" ethos). Disallow finer-than-fetched grain changes entirely on
instant dashboards (rejected — would make instant mode change existing
user-visible behavior rather than only changing *how fast* it responds,
which spec.md's User Story 1 explicitly requires it not to do).

## R4 — Vendored, lazy-loaded, not CDN

**Decision**: Commit Perspective's JS + `.wasm` assets under
`app/static/vendor/perspective/`, loaded via a dynamic `import()` that only
executes when a dashboard's `instant` flag is `true`. No `<script src="https://...">`
CDN reference anywhere.

**Rationale**: Every existing frontend module in this codebase is loaded
natively with zero CDN references — a grep across `app/static/` today
returns none. That is consistent with the single-Docker-image, state-
outside-the-image packaging posture (Technology Constraints) and, in a
regulated/clinical-ops-adjacent deployment context, with the ability to run
fully air-gapped. Introducing the project's first-ever CDN dependency at
the same time as its first-ever frontend dependency is two new precedents
where spec.md only asks the constitution to absorb one. Lazy-loading behind
the `instant` flag means the majority of views (builder, explorer,
modelling, sandbox, admin) never pay the WASM payload's weight at all — the
cost is scoped exactly to the dashboards that opted in, matching FR-014's
scope boundary.

**Alternatives considered**: CDN load (rejected — see above; also a live
external dependency at request time, which nothing else in this deployment
model has). Eager load on app boot (rejected — pure cost with no benefit
for the majority of sessions that never open an instant dashboard).

## R5 — Size cap default (needs benchmark before shipping)

**Decision**: Propose a starting default of **150,000 rows or ~25 MB of
Arrow IPC payload per tile, whichever is hit first**, evaluated on the
actual extract response (FR-009) rather than estimated in advance. This
number is a starting point for the benchmark in SC-002, not a settled
constant — Principle II's own bar ("performance claims are validated
against a large real dataset... not synthetic toy data") applies directly
here, and this feature should not ship without running the 13M-row NYC taxi
benchmark through at least one tile expected to exceed the cap and one
expected to comfortably fit, exactly as SC-002 specifies.

**Rationale**: A row-count-only cap is easy to reason about but can hide
genuinely large payloads for wide extracts (many unioned dimension
columns, per FR-006); a byte-size-only cap is a better direct proxy for
"will this be slow to transfer and slow for Perspective to ingest" but is
opaque to an author configuring a dashboard. Using both, with either one
tripping the fallback, catches both failure modes without requiring an
author to reason about which one matters for their particular tile.

**Alternatives considered**: A single global cap enforced per-dashboard
rather than per-tile (rejected in spec.md's FR-009 already — one large
tile would silently disable the win for every other tile on the same
dashboard). No cap, relying on authors to notice a slow dashboard
(rejected — directly contradicts the "safe by default expectation" goal of
User Story 2).
