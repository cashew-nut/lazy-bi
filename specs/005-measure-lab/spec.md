# Feature Specification: Measure Lab — Author Measures Live on the Visual

**Feature Branch**: `feature/measure-lab` (merged via [PR #1](https://github.com/cashew-nut/lazy-bi/pull/1), commit `29cba5f`)

**Created**: 2026-07-10

**Status**: Implemented (retroactive spec, written from project history after the fact — this is the one feature in this backfill with a real commit, PR, and this repository's own git history to cite directly)

**Input**: Verbatim user request: *"The next thing I'd like you to work on
is a fantastic measure creation experience. Ofc users can write the yaml -
but it's a much better experience if they can write a measure (ideally with
some intellisense) directly on the visual to see it resolve in action. If
they like the measure, they should be able to save it either to the visual
or the model. Everything is set up in github btw - can each of these
developments be feature branches?"*

## Provenance

The first feature built after `git init` — and the one that established the
project's now-standard feature-branch-per-development workflow (constitution
Principle VII). Builds on the semantic layer and model editor from
[001](../001-core-bi-platform/spec.md), and on the visual/dashboard/portal
plumbing from [001](../001-core-bi-platform/spec.md),
[003](../003-advanced-visuals-cross-filtering/spec.md), and
[004](../004-studio-portal-data-explorer/spec.md) — this spec is what let a
measure defined once keep working everywhere a visual already worked, "with
no extra plumbing."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Author and preview a measure live on the visual (Priority: P1)

From the builder's measure list, "+ new measure" opens an inline editor
docked on the current visual. As the user types a Polars-style expression,
context-aware completions appear (expression starters after `pl.`, the
query's actual resolved columns — including joined ones, with dtypes —
after `pl.col("`, and aggregation/transform methods after a trailing `.`),
and every keystroke (debounced) re-runs the current query with the draft
measure included, rendering it live in the chart.

**Why this priority**: This is the actual ask — "write a measure... to see
it resolve in action" — everything else (saving) only matters if authoring
itself is fast and trustworthy.

**Independent Test**: Open the measure lab on an existing visual, type an
expression using at least one chained completion (e.g. `pl.` → `col("`  →
a real column → `)` → `.` → an aggregation), and confirm the chart updates
to reflect the draft measure without saving anything.

**Acceptance Scenarios**:

1. **Given** the measure lab open on a visual, **When** the user types
   `pl.`, **Then** expression-starter completions appear (e.g. `col("")`,
   `len()`, `when().then().otherwise()`).
2. **Given** the user has typed `pl.col("`, **When** completions are
   requested, **Then** they list the query's actual resolved source
   columns — including columns available only via a join — each with its
   dtype.
3. **Given** a completion is accepted (e.g. `col("")`), **When** it is
   inserted, **Then** the cursor lands between the quotes and, for a
   column-name completion, immediately offers the next relevant completion
   set (chaining).
4. **Given** the user has typed a complete expression followed by `.`,
   **When** completions are requested, **Then** aggregation/transform
   methods appropriate to a polars expression are offered.
5. **Given** completions are visible, **When** the user navigates with
   arrow keys and accepts with Enter/Tab (or Esc to dismiss), **Then** the
   editor behaves correctly — mouse selection MUST also work as an
   alternative.
6. **Given** a syntactically valid, resolvable draft expression, **When**
   the user pauses typing (debounce), **Then** the current query re-runs
   with the draft measure included and the chart re-renders to include it —
   or, when the query has no dimensions, the status line shows the
   formatted scalar value.
7. **Given** a draft expression that fails to evaluate (bad method name,
   wrong type, etc.), **When** the debounced re-run happens, **Then** the
   error is shown inline, naming the measure and the underlying engine
   error verbatim (e.g. `measure 'discount_rate': 'Expr' object has no
   attribute 'su'`) — the rest of the visual keeps rendering.

---

### User Story 2 - Save a measure to the visual (Priority: P2)

Once satisfied with a draft measure, the user can save it scoped to the
current visual only. It becomes part of that visual's persisted query spec
and continues to resolve correctly everywhere that visual is rendered —
dashboard tiles, focus mode, the portal — without any additional setup.

**Why this priority**: The lighter-weight of the two save paths, and the
one that lets a user keep a useful one-off measure without touching shared
model configuration.

**Independent Test**: Save a drafted measure to a visual, place that visual
on a dashboard, open it in focus mode, and (if published) view it in the
portal — confirm the measure resolves identically in all three contexts
with no extra configuration in any of them.

**Acceptance Scenarios**:

1. **Given** a valid draft measure, **When** SAVE TO VISUAL is used,
   **Then** the measure is embedded in the visual's persisted query spec as
   an `inline_measure` and the visual reopens with it intact after a reload.
2. **Given** a visual with a saved inline measure, **When** it is rendered
   as a dashboard tile, in focus mode, or in the portal, **Then** the
   measure resolves correctly in every context with no per-context
   configuration.
3. **Given** a saved inline measure, **When** it is displayed, **Then** it
   renders as a visually distinct chip (marked as visual-scoped, not a
   model measure) offering edit and remove.
4. **Given** an inline measure with the same name as an existing model
   measure, **When** the query runs, **Then** the inline measure shadows
   the model measure for that query only — the model measure itself is
   unaffected.

---

### User Story 3 - Promote a measure to the model (Priority: P3)

Instead of (or after) saving to a visual, the user can save a draft measure
directly into the model's YAML — appending it as a properly-quoted entry
without disturbing existing comments or formatting — after which it hot-
reloads and behaves as an ordinary, shared model measure available to every
visual against that model.

**Why this priority**: The natural "graduate this from one-off to shared"
path, valuable but sequentially after proving the measure out via the other
two stories.

**Independent Test**: Save a draft measure to the model, confirm the YAML
file on disk gained exactly the new measure with existing content
untouched, and confirm a *different, unrelated* visual against the same
model can immediately select the newly promoted measure.

**Acceptance Scenarios**:

1. **Given** a valid draft measure, **When** SAVE TO MODEL is used,
   **Then** it is appended to the model's YAML file as a new measure entry,
   and every other line of the file (including comments) is left
   byte-for-byte unchanged.
2. **Given** a measure just promoted to the model, **When** the semantic
   layer hot-reloads, **Then** it is immediately available, by name, to any
   visual against that model — not only the one it was drafted on.
3. **Given** a promoted measure, **When** its originating chip is shown,
   **Then** it now displays as a regular model measure, not a visual-scoped
   one.

### Edge Cases

- What happens when the user types a column-completion that would produce a
  duplicate closing `")"` because one is already present? Regression-tested
  behavior: the editor must not double the closing quote/paren.
- What happens when the measure lab is open and the underlying visual's
  model or query shape changes out from under it (e.g. a dimension is
  removed)? The draft's live-resolution error path must degrade
  gracefully, not crash the builder.
- What happens when SAVE TO MODEL targets a measure name that already
  exists in that model's YAML? Behavior is unresolved by this
  retroactive spec — worth a real answer (reject, overwrite, or auto-rename)
  before this path sees heavier use.
- What happens when completions are requested outside any recognizable
  context (not after `pl.`, not after `pl.col("`, not after a trailing
  `.`)? No completions should be offered rather than incorrect ones.
- What happens to an unsaved draft if the user navigates away from the
  visual (switches models, closes the builder) before saving? Not
  explicitly verified in this feature's history — assume the draft is
  discarded, but this should be confirmed intentionally rather than by
  accident.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The builder MUST offer a "+ new measure" control that opens
  an inline measure editor docked on the current visual.
- **FR-002**: The editor MUST offer context-aware completions: expression
  starters after `pl.`; the query's resolved source columns (including
  post-join columns), each with its dtype, after `pl.col("`; and
  aggregation/transform methods after a trailing `.` on a valid expression.
- **FR-003**: Completions MUST be navigable and acceptable via keyboard
  (arrows, Enter, Tab, Esc) and via mouse, and MUST chain — accepting a
  completion that introduces a new context (e.g. an empty-string column
  slot) MUST immediately offer the next relevant completion set.
- **FR-004**: Every debounced keystroke on a syntactically progressable
  draft MUST re-run the visual's current query with the draft measure
  included and update the rendered chart — or, with no dimensions in the
  query, update a status-line scalar value — live.
- **FR-005**: A draft expression that fails to evaluate MUST surface an
  inline error naming the measure and the underlying engine error verbatim,
  without interrupting the rest of the visual's rendering.
- **FR-006**: The query engine MUST accept ad-hoc `inline_measures` scoped
  to a single query request, each carrying its own label/format metadata,
  and each MUST shadow any model measure of the same name for that query
  only.
- **FR-007**: SAVE TO VISUAL MUST embed the measure as an `inline_measure`
  in the visual's persisted query spec, such that it resolves correctly
  wherever that visual is subsequently rendered (dashboard tile, focus
  mode, portal) with no additional configuration.
- **FR-008**: SAVE TO MODEL MUST append the measure to the model's YAML
  file via a comment-preserving text edit (not a full rewrite/re-serialize)
  and MUST trigger a semantic-layer hot-reload.
- **FR-009**: A visual-scoped (unsaved-to-model) measure MUST render as a
  visually distinct chip offering edit and remove, clearly differentiated
  from an ordinary model measure.
- **FR-010**: The backend MUST expose an endpoint returning a model's
  resolved post-join schema (columns + dtypes) for the editor's column
  completions to consume.
- **FR-011**: The measure-authoring surface and its persistence paths MUST
  be covered by automated tests, including: inline-measure query behavior,
  name-shadowing of a model measure, the comment-preserving YAML append,
  and the relevant API endpoints.

### Key Entities

- **Inline measure**: An ad-hoc, query-scoped measure — an expression plus
  label/format metadata — that either lives only inside one visual's saved
  query spec, or is promoted into a model's YAML.
- **Draft measure**: The transient, unsaved state of the measure lab editor
  before either save path is used.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user can go from an empty measure editor to a chart
  reflecting a working custom expression without leaving the visual, using
  intellisense rather than needing to know the schema in advance.
- **SC-002**: Live resolution of a draft measure against a ~60,000-row model
  completes in well under 200ms, so editing feels live rather than
  batch-updated.
- **SC-003**: A measure saved to a visual renders correctly, with zero
  additional configuration, when that visual subsequently appears on a
  dashboard tile, in focus mode, and in the portal.
- **SC-004**: A measure saved to the model preserves 100% of the model
  file's pre-existing content (comments, formatting) outside the new
  entry, and is immediately usable by other visuals against that model.
- **SC-005**: An invalid draft expression is diagnosable from the inline
  error alone, without consulting source code or documentation.

## Assumptions

- Completions are a static, hand-maintained list (expression starters and
  ~20 aggregation/transform methods) rather than derived from each column's
  dtype — e.g. `.dt.year()` is offered even on non-date columns. This is a
  known, deliberately deferred refinement (noted at delivery time), not an
  oversight.
- Measure expressions continue to be `eval`'d with `pl` in scope, at the
  same trust level as application code (constitution Principle VI) — the
  measure lab does not change or widen that trust boundary, it only adds an
  authoring surface on top of it.
- "Save to visual" and "save to model" are the only two persistence
  targets; there is no draft/staging state persisted between browser
  sessions.
