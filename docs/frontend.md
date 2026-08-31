# Frontend

**Source:** `app/static/js/*.js` (27 modules, ~10.8k lines) ·
`app/static/js/charts/*.js` (12 modules, ~1k lines) · `app/static/style.css`,
`app/static/tokens.css` · `design.md` (the locked design system)

**No bundler, no framework, no build step** — vanilla ES modules loaded
natively by the browser, `"use strict"` at the top of every file, imported
by `<script type="module">` in `app/static/index.html`. This is a
deliberate simplicity choice recorded in the project constitution, not an
oversight: a new chart type follows the existing renderer +
shared-frame/pivot/dispatch pattern rather than reaching for a framework.
The one exception — [Perspective](#instant-mode-client-side-re-aggregation),
used strictly headless — is documented below and in
[Query Engine](query-engine.md#instant-mode-appextractpy).

## How the pieces avoid circular imports

Two views (`router.js` and every view module) need to call *into* each
other, which would ordinarily mean a two-way import. The codebase resolves
this with a **hooks registry** in `state.js`:

```js
export const hooks = {};   // populated by each view module at import time
```

A view module (e.g. `home.js`) registers `hooks.renderHome = renderHome`
as an import-time side effect; `router.js` calls `hooks.renderHome?.()`
without ever importing `home.js` directly. `main.js` is what actually
imports every view module (many of them for their *side effects alone* —
`import "./home.js"` never uses a named export, just triggers the
registration), which is also why `main.js` is effectively the whole app's
wiring diagram: nearly every `addEventListener` call in the app lives
there, and it's read top-to-bottom as the map of what's interactive.

## Core plumbing

| Module | Role |
|---|---|
| `lib.js` | DOM/fetch/formatting primitives, no app state: `$()`, `el()`, `svgEl()`, `api()` (fetch wrapper — sets the CSRF header, dispatches an `auth-required` event on 401), `apiRaw()`/`apiUpload()` (non-JSON-response and multipart variants), number/date/byte formatters. |
| `state.js` | The one mutable `state` object (current model, dims/measures/filters, chart type, open dashboard, tiles, cross-filter, …), the `hooks` registry, and `showView(view)` — the single place that hides/shows `#<view>-view` panels and derives the header nav's active mode from the view. |
| `router.js` | Maps real URL paths (`/studio/model/sales`, `/modelling/pipeline/x/yaml`, …) to view/entity resolution. `navigate(path)` is the one function every in-app action uses to move around; it runs a `guardLeave()` chain first (unsaved editor/model-form/bundle-form/composer/sandbox edits block the navigation) then calls `resolveRoute()`, which every handler reads off `hooks`. `popstate` and the initial load both resolve through the same function, so a reload or a pasted link always lands in the same state a click would have. |
| `theme.js` | Owns the 4-theme catalog and switching engine — see [Themes](#themes) below. |
| `auth.js` | Session state + the login overlay; presentation only, the server enforces every rule (elements carry `data-role="author"\|"admin"` and are hidden/shown, never disabled-and-trusted). |

## Chart architecture

```
ctx = { model, dims, chartType, result, container, legendBox, rerender, onCross? }
```

Every chart renderer — builder canvas, a dashboard tile, or the focus-mode
modal — is handed the same `ctx` shape and renders into `ctx.container`.
`charts/index.js` is the dispatcher:

```js
export function decideChart(ctx) {
  if (ctx.chartType && ctx.chartType !== "auto") return ctx.chartType;
  const dimCount = (ctx.dims || []).length;
  if (dimCount === 0) return "stat";
  if (dimCount > 2) return "table";
  return ctx.dims.some(hasTimeType) ? "line" : "bar";
}
```

`AUTO` picks a form from the query shape; a handful of chart types
(`scatter`, `sankey`, `ribbon`, `geo`) are explicit-only choices with their
own dimension/measure-count requirements (`vizRequirementError`), since
they don't have an unambiguous "auto" trigger.

| Chart module | Renders |
|---|---|
| `charts/common.js` | Shared constants (`PALETTE`, `MAX_SERIES=8`, `GRAINS`), `ctx` helpers, the shared tooltip singleton, the legend. |
| `charts/frame.js` | Shared plot scaffolding: the SVG frame, scales, axes — every axis-based chart builds on this. `plotSpace()` measures the pane; `plotFrame(box, want)` grows the canvas past it (and hands the pane a scrollbar) when the content needs more room than it offers. |
| `charts/pivot.js` | Pivots long query rows into `{xs, series}` for bar/line/ribbon — the shape every axis chart actually draws from. |
| `charts/bar.js` / `line.js` / `ribbon.js` | Rounded-mark bars grouped by series; 2px lines with crosshair; stacked bands that re-rank at every x (rank 1 on top). |
| `charts/scatter.js` | Color *and* a distinct marker shape per series — color alone fails all-pairs colorblind checks. |
| `charts/sankey.js` | Flow across the query's dimensions in declared order; first measure = link width. |
| `charts/geo.js` | Bubble map over a vendored world outline (`app/static/world.geo.json`) — no external map tiles. |
| `charts/stat.js` | Hero numbers for a dimensionless query. |
| `charts/table.js` | The table renderer — also the accessibility fallback every chart type falls back to. |
| `charts/rolemap.js` | A small readout of which selected dims/measures are driving which encoding (x/legend/y/sort) — otherwise invisible in the builder UI. |

**Panes that scroll.** A chart is laid out into the pane it was given, but
some content simply needs more room than the pane has: a sankey stage with
forty nodes, a bar chart with four hundred categories. `plotFrame(box, want)`
takes the canvas the content actually needs — `charts/sankey.js` asks for a
slot per node and label room per stage, `charts/bar.js` for a band per
category — and when that exceeds the pane it grows the canvas past it, sizes
it in real pixels, and puts it in a `.viz-scroll` pane so the rest is
reachable. Charts that fit are untouched: same fitted 100%/100% SVG, no
scrollbar. Growth is capped at `MAX_CANVAS`, past which a chart compresses
again rather than asking the browser to paint a canvas no one can read.

The same rule covers the non-SVG forms a visual can take: `.table-scroll`
(the table renderer, and the fallback any chart type can drop to) and
`.stat-grid` scroll inside their pane wherever they render — tile, focus
modal, notebook, chat answer.

**The categorical palette** (`PALETTE`, 8 validated slots, assigned in
sequence and never cycled — a 9th series folds into a neutral "Other") is
theme-owned: `theme.js` mutates the same array in place on switch, so every
consumer's live import binding sees the new colors with no import-contract
change. `validate_palette.js` checks each theme's palette against its own
background — lightness band, chroma floor, colorblind-safe adjacent
separation, ≥3:1 contrast — re-run manually via `/?validate` against
whichever theme is active.

## Query builder, dashboards, and the measure lab

| Module | Role |
|---|---|
| `builder.js` (1185 lines) | The query builder itself: model switcher, field rail, query strip, chart toolbar, execution, saved visuals. The largest single view module. |
| `dashboard.js` (921 lines) | Tile grid, named filter-set views, ephemeral cross-filtering, session-only grain override, focus mode. Serves both Studio (editable) and the Portal (view/grain/filter choices stay local — `saveDash()` is a no-op there, so nothing a portal viewer touches ever writes back into the saved view). |
| `filters.js` | The filter-op vocabulary and value-entry widgets, shared by the builder, dashboard views, and focus mode. |
| `measurelab.js` | Author a SQL measure directly on the open visual, with live completion and a live-rendering draft; edits a complex measure's `from:` step too, and has two save paths — onto the visual (`inline_measures`) or promoted to the model YAML. |
| `measureai.js` | The ASK AI bar shared by the measure lab and the modelling form: one SSE turn against `POST /api/measures/write/stream`, the progress it reports (writing → running it against your data → a repair round), and the note that says whether the answer was actually run. Hidden unless the deployment has an LLM configured and the signed-in role can author. |
| `completion.js` | The one shared expression/column completion engine behind both the measure lab and the model YAML editor — one implementation, one vocabulary, no drift between the two surfaces. |
| `instant.js` | Client-side re-aggregation — see below. |

### Instant mode: client-side re-aggregation

`instant.js` is the one frontend module allowed to use
[Perspective](https://perspective.finos.org/) (FINOS, Apache-2.0) — and
strictly as a **headless columnar aggregation engine**: `Table`/`View`
only, never `perspective-viewer` or any of its rendering components. Every
chart stays one of the hand-rolled SVG renderers above, reading the exact
same `{columns, rows}` shape whether the data came from a live `/api/query`
response or a locally re-aggregated Perspective `View`. The library is
vendored under `app/static/vendor/` (no CDN — see
`app/static/vendor/README.md`) and pulled in via a dynamic `import()` gated
on a dashboard's `instant` flag, so a dashboard that never turns it on never
loads a byte of it. The server-side half of this — what goes *into* an
extract, decomposition, the size cap — is
[Query Engine → Instant mode](query-engine.md#instant-mode-appextractpy).

## Modelling & authoring

| Module | Role |
|---|---|
| `modelling.js` (433 lines) | The Modelling workspace shell: bucket dataset tree (left rail), fact models / common models / pipelines management (center), delegating to the forms below for actual editing. |
| `modelform.js` (1472 lines) | The guided fact-model form — the single largest frontend module. A sectioned editor (Overview / Datasets / Relations / Dimensions / Measures / YAML) with free navigation rather than a gated wizard. |
| `bundleform.js` (502 lines) | The equivalent guided form for common (dimension-bundle) models — same architecture as `modelform.js`, minus measures. |
| `formkit.js` (458 lines) | Shared plumbing both guided forms build on: the source-schema cache, the bucket dataset picker, relationship-pair rows, small field builders. Holds no form state of its own. |
| `editor.js` (882 lines) | The raw-YAML editor shared by models, bundles, and pipelines — live validation, a source-column palette, a dataset picker, and expression intellisense, all driven off the server's `/validate` endpoints. |
| `dimlab.js` | Inserts a `dimension_imports:` block into the fact-model editor for a chosen bundle+dataset. |
| `yamlhighlight.js` / `sqlhighlight.js` | Cosmetic, line-based regex tokenizers for the editors' read-only syntax-highlight backdrop — never a source of truth; the server round trip is still the only real arbiter of valid YAML/SQL. |
| `lineagegraph.js` | The read-only, hand-rolled SVG lineage DAG — see [Pipelines → Lineage graph](pipelines.md#traceability-layers-and-lineage). Selection/field-expansion state is ephemeral. |
| `panelchat.js` | The modelling form's ephemeral right-hand chat panel, scoped to the model currently being edited — see [Conversational Analytics](conversational-analytics.md). |

## Sandbox, chat, composer, notebooks, portal, account

| Module | Role |
|---|---|
| `sandbox.js` (594 lines) | Cell editing, RUN, and the bucket-file browser for the scratch SQL notebook surface — see [Sandbox](sandbox.md). |
| `sandboxagent.js` | The coding-agent panel: streamed proposed cells, one-click APPLY/APPLY+RUN, a FIX WITH AGENT button on a failed cell. |
| `chat.js` (553 lines) | The Chat surface — every answer shown alongside the exact query that grounded it. Hidden entirely (nav + view) when the server has no LLM key configured. |
| `composer.js` (335 lines) | The Split-Studio composer UI: script rail (template/narrative/picks/instructions) on one side, the live-typing draft page on the other. |
| `notebook.js` | Hydrates a saved notebook's stored `html` into a live page after render — brings `.nb-visual`/`.nb-dashboard` marker elements to life as real charts/dashboards, `.nb-tabs` into interactive tab groups, native `<details>` into collapsibles. See [Notebooks](#notebooks) below. |
| `portal.js` | The read-only consumption surface: a nested folder tree of published dashboards, breadcrumb-only navigation into subfolders. |
| `admin.js` | ACCOUNT view: personal access tokens + password change for every role; the user-management panel renders admin-only — presentation only, the server enforces the rule. |
| `memories.js` | Admin curation UI for chat-learned model memories. |
| `home.js` | The `/` operator console: a numbered destination index (Studio/Modelling/Portal/Chat) plus admin shortcuts. |

### Notebooks

A notebook's stored `html` is a body fragment in a small, fixed
vocabulary — the same allowlist `app/composer.py`'s sanitizer enforces
server-side (see [Conversational Analytics](conversational-analytics.md#the-composer-appcomposerpy)):

- `<div class="nb-visual" data-visual-id="…">` — a saved visual,
  re-executed live (`compact` for a short stat-height tile).
- `<div class="nb-dashboard" data-dashboard-id="…" data-view="N">` — a
  whole dashboard embedded at one saved view.
- `<div class="nb-tabs">…` — tab groups; `<details class="nb-collapsible">`
  — native, depth-on-demand sections.
- `<aside class="nb-explainer" data-tone="info|method|warn">` — a callout
  that teaches the reader how to read a chart or flags a caveat.
- `<div class="nb-split">` — a claim/proof diptych row.

Pages are authored by hand through the API, or chatted into existence in
the Composer — either way, `notebook.js` is the one renderer that brings
either kind of page to life identically.

## The design system (`design.md`)

`design.md` at the repo root is a **locked** design system — every module
redesign reads it before emitting code, and the file is extended in place
rather than regenerated per module. The highlights, for anyone touching
CSS or markup structure:

- **Genre: modern-minimal.** An operator console used all day by people who
  already know the domain — restraint, precise hierarchy, minimal motion.
  The dark-neon atmospheric *surface* (scanlines, faint corner washes) is
  the house theme and isn't up for redesign; the genre governs structure
  and restraint, not palette.
- **The surface-tier rule** (the core structural move): three containment
  tiers and only three — **Ground** (no border/fill/shadow: rows, lists,
  the default for everything), **Field** (`--panel` fill, interactive
  containers: inputs, editors, cells), **Float** (`1px --line` border +
  `--popover` fill + shadow: only things that leave the flow — popovers,
  menus, modals). **The nesting law**: a tier may never contain a tier of
  equal or higher number — Float holds Field and Ground, nothing holds a
  Float except the viewport. This one rule is what keeps a page from
  becoming card-in-card.
- **Macrostructure families**: Console pages (Home/Portal/Modelling/
  Account) → *Index-First*, a ledger of rows, no card grids. Workbench
  pages (Studio/editors/Sandbox/Dashboards) → rails and panes framing a
  live canvas. Dialogue pages (Chat/Composer/panel chat/sandbox agent) →
  *Split Studio*, a conversation/evidence diptych — the shared
  `.chat-thread`/`.chat-msg` classes mean redesigning them once redesigns
  all four dialogue surfaces at once.
- **Typography**: single-family, monospace, by design (the one sanctioned
  case for a single-font app) — seven named type-scale steps
  (`--text-2xs` through `--text-2xl`, plus `--text-stat` for hero numbers),
  never a raw pixel size.
- **Tokens, not raw values** — color, spacing (4-point scale,
  `--space-3xs`…`--space-3xl`), radius (three tiers tied to surface tier),
  motion durations/easings are all named custom properties in
  `app/static/tokens.css`, imported first by `style.css`.
- **Explicit bans**, enforced by a "slop test" at every module handoff:
  card-in-card, identical `repeat(auto-fill, minmax(…))` tile grids,
  outer `box-shadow` glow on a dark surface, decorative section eyebrows,
  italic headings, `transition: all`, raw px anywhere a token exists,
  celebratory toasts.

### Themes

`theme.js` owns the 4-theme catalog: **Cyberpunk** (default, dark neon),
**Paddock Light** (light, id frozen as `daylight`), **Canopy** (a muted
professional dark, id `slate`), **Paddock** (heritage motorsport, id
`contrast`). Theme *ids* are frozen — they're persisted in `localStorage`
and in `authstore.VALID_THEMES` — independent of the display names/looks,
which have changed since the ids were assigned. Switching is instant (CSS
custom-property blocks keyed by `[data-theme="..."]`, no reload) and
re-skins chart colors too, since `PALETTE`/`OTHER_COLOR` in
`charts/common.js` are mutated in place on switch. A signed-in user's
choice syncs to their account (`GET`/`PUT /api/users/me/theme`) so it
follows them across browsers/devices — whichever side (local vs. account)
was changed most recently wins on next login, and writes back to the side
that was behind.

## Browser verification

The project constitution's Principle IV ("Browser-Verified Before Done")
holds specifically for this layer: a UI change isn't complete when the
code compiles and the (Python) test suite passes — it's complete once
it's been driven end-to-end in a real browser, including the
persistence round-trip (save, cold-reload, confirm) and a zero-console-
errors check. There is no frontend test suite or build step to lean on
instead; the browser itself is the verification step.
