# Feature Specification: Common Dimensional Models — Shared, Reusable Dimensions Across Fact Models

**Feature Branch**: `feature/common-dimensions`

**Created**: 2026-07-10

**Status**: Draft

**Input**: User description (two parts, from separate sessions): *"The next
feature is to support a 'common dimension' model. We've built this app to
be nicely modular with different semantic models, but one thing that will
make this successful is if we can support common dimensional imports to
multiple models, and then you just maintain the common model for dimensions
and the separate models for different types of facts and their measures."*
— followed by, when picked back up: *"To be clear about what I want, I am
imagining common dimensions which might span multiple tables. For example,
in a sales context, you might have accounts, opportunities, and products
which are common across models. In a clinical operations context you might
have studies, study countries, and study sites - all of these would be
common across many models. You should be able to specify these common
dimensions, including the joins between them."*

## Provenance

This is the first feature to go through the full spec-kit workflow
(`/speckit-specify` → `/speckit-plan` → `/speckit-tasks` → `/speckit-implement`)
rather than being backfilled retroactively — see
[001](../001-core-bi-platform/spec.md) through
[005](../005-measure-lab/spec.md) for the prior, already-shipped work this
builds on, and `.specify/memory/constitution.md` for the governing
principles (Principle I in particular: "the semantic layer is the only
contract").

**Clarified during specification**: the open design question was how far a
single import should reach when the imported thing itself joins to other
shared entities (e.g. importing "opportunities," which itself joins to
"accounts" — does a fact model get "accounts" attributes for free?). Resolved
by the user: common dimensions are organized as named **bundles** (e.g. "sales
dimensions" containing the accounts, opportunities, and products datasets,
with the joins between those datasets defined once, inside the bundle). A
fact model imports a bundle by name — by default getting every dataset in
it — with the option to restrict the import to a chosen subset of the
bundle's datasets. Fact models choose a *bundle* ("sales dimensions"), not
an individual dataset ("opportunities"), as the unit of import.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Define a common dimensional model once (Priority: P1)

A BI developer declares a named, reusable **common dimensional model** —
independent of any fact model — containing one or more **datasets** (e.g.
`accounts`, `opportunities`, `products`), each with its own source and
dimensions, plus the joins between those datasets (e.g. `opportunities`
joins to `accounts` on an account key).

**Why this priority**: Nothing else in this feature is possible without a
place to define shared dimensions once — this is the foundation the rest of
the feature imports from.

**Independent Test**: Declare a common dimensional model with two datasets
and a join between them, and confirm it loads without error and its
datasets' dimensions and cross-dataset attributes resolve correctly when
queried on their own (independent of any fact model importing it yet).

**Acceptance Scenarios**:

1. **Given** a common dimensional model declaring two or more datasets,
   **When** it loads, **Then** each dataset's source, dimensions, and any
   joins between datasets in the same bundle are parsed and validated the
   same way a fact model's own source/dimensions/joins are today.
2. **Given** a join declared between two datasets in the same bundle (e.g.
   `opportunities` → `accounts`), **When** the bundle loads, **Then**
   attributes from the joined-to dataset (`accounts`) become part of the
   joined-from dataset's (`opportunities`) available attributes, lazily,
   with the same pushdown behavior as any other join.
3. **Given** an invalid bundle (bad join key, unknown dataset reference, or
   malformed dimension), **When** it loads, **Then** the error names the
   specific bundle and dataset at fault, the same way model-loading errors
   do today.
4. **Given** a common dimensional model contains only dimensions (no
   measures are declared on it), **When** it is authored, **Then** the
   system does not require or expose a measures section for it — common
   dimensional models are dimension-only by design.

---

### User Story 2 - Import a common dimensional model into a fact model (Priority: P1)

A fact model (e.g. `sales`, or a new `bookings` model) imports a common
dimensional model by name, declaring how its own base source connects
("anchors") into one or more datasets in that bundle. By default, importing
a bundle exposes **every** dataset in it as dimensions on the fact model —
including datasets reachable only through the bundle's own internal joins,
not just the one the fact model directly anchors to — with no need to
redeclare sources or join keys the bundle already defines.

**Why this priority**: This is the actual payoff — a fact model gaining
shared dimensions without re-declaring them is the entire point of the
feature, and is what proves User Story 1's definitions are actually reusable.

**Independent Test**: Import a two-dataset bundle (where dataset B joins to
dataset A) into a fact model, anchoring only on dataset B, and confirm both
dataset A's and dataset B's attributes are queryable as dimensions on the
fact model — without the fact model declaring any join to dataset A itself.

**Acceptance Scenarios**:

1. **Given** a fact model importing a common dimensional model with a
   declared anchor (the fact model's own column(s) mapped to a key on one
   dataset in the bundle), **When** the model loads, **Then** every
   dataset in the bundle becomes available as dimensions on the fact
   model, by default.
2. **Given** a bundle where dataset B joins to dataset A internally, and a
   fact model that anchors only to dataset B, **When** a query uses a
   dimension that lives on dataset A, **Then** it resolves correctly via
   the bundle's own internal join — the fact model does not need to know
   dataset A exists independently of the bundle.
3. **Given** dimensions imported from a common dimensional model, **When**
   they appear in the query builder, **Then** they are indistinguishable
   in behavior from the fact model's own natively-declared dimensions
   (selectable, filterable, groupable, usable in saved visuals/dashboards).
4. **Given** a fact model, **When** it imports more than one distinct
   common dimensional model, **Then** dimensions from every imported
   bundle are available together, each anchored independently.
5. **Given** a fact row whose anchor key has no matching row in the
   imported bundle, **When** it is queried, **Then** it behaves like any
   other unmatched left join today (imported attributes are null for that
   row) rather than dropping the row, unless the fact model explicitly
   declares the anchor as an inner join.

---

### User Story 3 - Import only a subset of a bundle's datasets (Priority: P2)

Instead of importing every dataset in a common dimensional model, a fact
model can restrict the import to an explicit subset of the bundle's
datasets.

**Why this priority**: The default (whole-bundle) import covers the common
case; subsetting is a deliberate scope-narrowing tool for when a fact model
genuinely shouldn't expose everything in a bundle — valuable, but secondary
to the default path working correctly.

**Independent Test**: Import a three-dataset bundle into a fact model with
an explicit subset naming two of the three datasets, and confirm only
those two datasets' dimensions appear on the fact model — the third is not
selectable anywhere.

**Acceptance Scenarios**:

1. **Given** a fact model's import of a common dimensional model, **When**
   it declares an explicit subset of dataset names, **Then** only those
   datasets' dimensions are exposed on the fact model — the rest of the
   bundle is not loaded into that fact model's dimension set.
2. **Given** a subset that names a dataset not present in the bundle,
   **When** the fact model loads, **Then** it fails validation naming the
   unknown dataset.
3. **Given** no subset is declared, **When** a fact model imports a bundle,
   **Then** the behavior is identical to User Story 2 (the whole bundle).

---

### User Story 4 - Author and import common dimensional models in the app (Priority: P1)

A developer creates and edits common dimensional models from within the
application — not only by hand-editing files on disk — and, while creating or
editing a fact model, discovers the available common dimensional models and
imports one into the model being edited without having to already know the
bundle's name or the import YAML syntax by heart.

**Why this priority**: Elevated from an earlier out-of-scope assumption after
first use — the backend mechanism is inert to a developer who can't discover
or wire it up from where they already do their modeling. A shared-dimension
system nobody can find the on-ramp to delivers none of its value, so the
authoring/discovery surface is as load-bearing as the resolution engine
itself. (Originally deferred; the deferral was wrong and this spec now
supersedes it.)

**Independent Test**: Starting only from the running app (no shell, no file
editor), create a new common dimensional model, then create or edit a fact
model and import that common dimensional model into it, and confirm the fact
model gains the shared dimensions — all through the UI.

**Acceptance Scenarios**:

1. **Given** the application's model-authoring surface, **When** a developer
   chooses to create a new common dimensional model, **Then** they get an
   authoring surface distinct from the fact-model editor, with live
   validation reporting the same parse/collision/cycle errors the loader
   would raise, and a save that hot-reloads it into the registry.
2. **Given** an existing common dimensional model, **When** a developer opens
   it in the app, **Then** they can edit it and save, and every fact model
   that imports it reflects the change with no edits to those fact models.
3. **Given** a developer creating or editing a fact model, **When** they view
   the available common dimensional models, **Then** each is presented with
   its datasets, and a single action adds an import of it into the model
   being edited — pre-filled well enough that the common case validates
   immediately.
4. **Given** an import added through that action, **When** validation runs,
   **Then** the resolved result (including the dimensions gained from the
   import, transitively) is reflected in the same live-validation feedback
   used for the rest of the model.
5. **Given** the application's list of things a visual can be built on,
   **When** a developer browses models to query, **Then** common dimensional
   models do **not** appear there — they are dimension providers, not
   queryable fact models, and are only reachable through the authoring and
   import surfaces.
6. **Given** a common dimensional model that at least one fact model
   currently imports, **When** a developer attempts to delete it from the
   app, **Then** the deletion is refused with a message naming the importing
   model(s), rather than silently breaking every importer on the next
   reload.

### Edge Cases

- What happens when a bundle's internal joins form a cycle (dataset A joins
  to B, B joins to A)? Must be rejected at bundle-load time with a clear
  error, not silently loop or infinite-recurse.
- What happens when two datasets within the same imported bundle each
  declare a dimension with the same name (e.g. both `accounts` and
  `opportunities` have an `owner`)? Must fail validation at bundle-load
  time, naming both datasets and the colliding dimension name — the bundle
  author must rename or alias one, since there is no reasonable
  automatic precedence between two equally-shared datasets.
- What happens when a fact model imports two different bundles that each
  expose a dimension with the same name? Must fail validation at
  fact-model-load time, naming both bundles and the colliding dimension —
  the fact model must subset one side (User Story 3) to resolve it.
- What happens when an imported dimension's name collides with a dimension
  declared natively on the importing fact model itself? The fact model's
  native dimension takes precedence (shadows the imported one), consistent
  with the existing precedent of inline measures shadowing model measures
  ([005](../005-measure-lab/spec.md)).
- What happens when a common dimensional model is edited (a dataset's
  source, a dimension, or an inter-dataset join changes)? Every fact model
  that imports it must reflect the change on the next hot-reload, with no
  edits required to the importing fact models' own YAML.
- What happens when a fact model's anchor join key type doesn't match the
  target dataset's key type? Must fail the same class of validation error
  as an existing mismatched join today.
- What happens when a dataset excluded by a subset import (User Story 3)
  was itself the sole path to another dataset in the bundle (e.g. excluding
  `opportunities` also cuts off the only route to `accounts`)? The excluded
  chain's attributes are simply unavailable to that fact model — this is
  not an error, since the subset was explicit.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST let a developer declare a common dimensional
  model as a named, reusable unit, independent of any fact model,
  containing one or more datasets.
- **FR-002**: Each dataset in a common dimensional model MUST declare its
  own source (path/format) and dimensions, using the same dimension
  declaration shape (name, column, label, type, spine, geo) available to a
  fact model's own dimensions today.
- **FR-003**: A common dimensional model MUST support declaring joins
  between its own datasets, using the same join semantics (join key(s),
  how: left/inner) as a fact model's existing joins to lookup sources.
- **FR-004**: A common dimensional model MUST NOT declare measures —
  measures remain the sole responsibility of fact models.
- **FR-005**: A fact model MUST be able to import a common dimensional
  model by name, declaring an anchor: how the fact model's own source
  column(s) map to a key on one (or more) datasets in that bundle.
- **FR-006**: By default, importing a common dimensional model MUST expose
  every dataset in the bundle as dimensions on the importing fact model,
  including datasets reachable only via the bundle's own internal joins
  rather than directly via the fact model's anchor.
- **FR-007**: A fact model MUST be able to restrict an import to an
  explicit subset of the bundle's dataset names, excluding the rest.
- **FR-008**: A fact model MUST be able to import more than one distinct
  common dimensional model at once.
- **FR-009**: Dimensions exposed via an imported common dimensional model
  MUST be queryable identically to natively-declared dimensions — same
  filter operators, same grain handling for time dimensions, same
  eligibility for spine/geo behavior, no distinct API or builder code path.
- **FR-010**: When an imported dimension's name collides with a dimension
  declared natively on the importing fact model, the native dimension MUST
  take precedence.
- **FR-011**: The system MUST reject, at load time, any of: a cyclical join
  between datasets in a bundle, a dimension-name collision between two
  datasets in the same bundle, a dimension-name collision between two
  bundles imported into the same fact model, or a subset import naming a
  dataset that doesn't exist in the bundle — each error MUST name the
  specific bundle/dataset/dimension at fault.
- **FR-012**: Editing a common dimensional model MUST hot-reload and take
  effect for every fact model that imports it, with no changes to those
  fact models' own configuration.
- **FR-013**: All joins introduced by this feature — within a bundle, and
  from a fact model's anchor into a bundle — MUST remain fully lazy
  (projection/predicate pushdown preserved), consistent with constitution
  Principle II.
- **FR-014**: The application MUST let a developer create, edit, and save a
  common dimensional model from within the app, with the same
  live-validation feedback the fact-model editor provides (reporting parse
  errors, cyclical bundle joins, and cross-dataset dimension collisions), and
  hot-reloading on save.
- **FR-015**: While creating or editing a fact model, the application MUST
  surface the available common dimensional models (each with its datasets)
  and provide a single action that adds an import of a chosen one into the
  model being edited, pre-filled well enough that the common case is valid
  without further hand-editing.
- **FR-016**: Common dimensional models MUST NOT appear anywhere a developer
  selects something to build or query a visual on — they are dimension
  providers, not queryable fact models.
- **FR-017**: Deleting a common dimensional model that at least one fact
  model currently imports MUST be refused with an error naming the importing
  model(s); it MUST NOT be possible to delete a bundle out from under its
  importers such that the next registry reload breaks.

### Key Entities

- **Common dimensional model (bundle)**: A named, reusable, dimension-only
  semantic unit, independent of any single fact model, containing one or
  more datasets and the joins between them.
- **Dataset**: A single source + its dimensions within a common dimensional
  model — the shared-dimension equivalent of what a join target is to a
  fact model today (e.g. `accounts`, `opportunities`, `products`; or
  `studies`, `study_countries`, `study_sites`).
- **Anchor**: The declaration, on a fact model, of how its own source
  column(s) connect to a key on a specific dataset within an imported
  bundle — the entry point into the bundle's graph of datasets.
- **Import**: A fact model's reference to a common dimensional model by
  name, with an anchor and an optional dataset subset.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A shared attribute (e.g. adding a new attribute to the
  `accounts` dataset in a bundle) is defined in exactly one place and
  becomes available to every fact model that already imports that bundle,
  with zero edits to those fact models.
- **SC-002**: A fact model anchoring to one dataset in a multi-dataset
  bundle gains access to every other dataset in that bundle reachable via
  the bundle's own joins, from a single import declaration — no per-dataset
  join redeclaration.
- **SC-003**: Two unrelated fact models importing the same bundle expose
  dimensions with identical names and semantics, so existing cross-model
  dashboard behavior (view filters, cross-filtering, matched by dimension
  name) works across them with no additional configuration.
- **SC-004**: Restricting an import to a subset of a bundle's datasets
  results in only those datasets' attributes being selectable in the
  builder for that fact model — verified by absence, not just presence.
- **SC-005**: A previously-duplicated shared join (e.g. today's `products`
  lookup, hand-copied into every model that wants supplier/tier
  attributes) can be expressed once as a bundle dataset and imported by
  every model that needs it, with the models' own YAML shrinking rather
  than growing.
- **SC-006**: A developer who has never opened a terminal or a file editor
  can, from the running app alone, create a common dimensional model and
  import it into a fact model, and see the fact model gain the shared
  dimensions.

## Assumptions

- Common dimensional models are dimension-only (no measures), per the
  original ask: "maintain the common model for dimensions and the separate
  models for different types of facts and their measures."
- Bundles do not nest — a common dimensional model cannot import another
  common dimensional model. Datasets within one bundle may only join to
  other datasets in that same bundle.
- The in-app authoring surface follows the established fact-model editor
  pattern ([001](../001-core-bi-platform/spec.md)): YAML-with-live-assists
  (validation + click-to-insert), not a fully form-driven builder. This keeps
  a single source of truth (the YAML text) and matches how models are already
  authored in this app, rather than introducing bidirectional
  form-and-text sync. **(Supersedes the earlier assumption, now removed, that
  an authoring UI was out of scope — see User Story 4; that deferral did not
  survive first contact with the running app.)**
- Existing single-model joins (a fact model joining directly to a raw
  lookup source, undeclared as a bundle) remain fully supported and
  unchanged — common dimensional models are an additional mechanism, not a
  replacement.
- Bundle datasets are trusted configuration, same as everything else in the
  semantic layer (constitution Principle VI) — this feature does not widen
  that trust boundary.
