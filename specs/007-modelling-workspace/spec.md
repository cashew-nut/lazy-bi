# Feature Specification: Modelling Workspace — Delightful Model Creation & Editing

**Feature Branch**: `feature/modelling-workspace`

**Created**: 2026-07-10

**Status**: Draft

**Input**: User description: "Rework the model creation and editing experience. It is currently all done via a plain yaml text editor. Editing via plain text should be supported, but the workflow should be delightful — users can select from available datasets, import common models, and write powerful expressions with intellisense. As part of this, move 'edit yaml', 'new model', and 'new common model' out of the STUDIO component and into the DATA component, which should be renamed 'MODELLING'."

## Clarifications

### Session 2026-07-10

- Q: In the dataset picker, what unit does the author choose from — the bucket holds individual objects but models source globs? → A: Grouped, drillable — objects are grouped into datasets by common prefix/partition and offered as a glob source (e.g. `path/*.parquet`), and a group can be expanded to pick a single exact object.
- Q: How do the guided affordances relate to the raw YAML editor? → A: Augment the text editor — a single YAML editor stays the source of truth; the dataset picker, import, and intellisense insert or patch YAML in place. No separate form/wizard surface; text and guided views cannot diverge because there is only one document.
- Q: What happens to unsaved model edits when the author navigates away from the editor? → A: Warn before leaving — navigating away with unsaved changes prompts to confirm discarding them; nothing in-progress is persisted unless the user saves.
- Q: Which contexts get expression intellisense + live validation during authoring? → A: Anywhere in the YAML — completion fires wherever the caret is in a polars-expression context within the editor (measures, expression-based/typed dimensions, filters), with measure-style "does it resolve" feedback where a full expression is expected.

### Session 2026-07-11 — Redesign: form-first authoring

The delivered yaml-editor-first experience was underwhelming; the 2026-07-10
"augment the text editor" decision is REVERSED. This is a redesign of this
feature, not a new feature.

- Q: Should model authoring lead with a yaml editor or a structured form? → A: Form-first — creating/editing a fact model opens a guided, stepped form (source dataset → joined datasets → common models → dimensions & measures → review) whose output is server-generated YAML. The raw YAML editor remains one click away ("edit yaml directly") for anything the form does not surface.
- Q: What is the source of truth when a form exists alongside the text editor? → A: The structured spec while the form is open; the yaml file on disk always. The form's spec renders to canonical YAML server-side (`POST /api/models/generate`) and an existing file re-opens into the form via `GET /api/models/{name}/spec`. Round-trips are semantically lossless; comments/hand-formatting are not preserved by a form save, and the review step says so.
- Q: Does form-first authoring cover common (shared-dimension) models too? → A: Yes — creating/editing a common model opens its own guided form (name & datasets → relationships between the bundle's datasets → per-dataset dimensions → review & save) with the same server-generated-YAML contract and the same raw-yaml escape hatch.
- Q: How are relationships (joins, common-model imports) expressed in the form? → A: As explicit column pairs — this model's column ⇄ the other dataset's column — chosen from each side's real (introspected) schema, so the two sides do NOT need to share a name (`left_on`/`right_on`; collapsed to `on` in the yaml only when they match). Sides degrade to free-text when a schema is unreachable.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Model authoring lives in a dedicated Modelling workspace (Priority: P1)

An analyst who has been building visuals in Studio wants to add or change a semantic model. Instead of hunting for model controls scattered in the Studio sidebar, they switch to a top-level **Modelling** workspace (the mode formerly labelled "Data"). There they see everything about the semantic layer in one place: the datasets available in the bucket, the fact models and common (shared-dimension) models that already exist, and clear entry points to create a new model, create a new common model, or edit any existing one. Studio no longer carries model-authoring controls; it is purely for building visuals against whatever models exist.

**Why this priority**: This is the information-architecture change the whole feature hangs on. Without a home for model management, the "delightful" affordances have nowhere to live, and the request to move the three authoring actions out of Studio is unmet. It is also independently valuable on its own: even with no other change, consolidating model management into one workspace and de-cluttering Studio is a usability win.

**Independent Test**: Open the app, confirm Studio's sidebar no longer shows "edit yaml", "+ new model", or "+ new common model"; switch to the renamed **Modelling** mode and confirm all three actions (edit existing, new model, new common model) are reachable there alongside the dataset/model overview; confirm building a visual in Studio against an existing model still works unchanged.

**Acceptance Scenarios**:

1. **Given** the app is open on Studio, **When** the analyst looks at the Studio sidebar, **Then** there are no model-creation or YAML-editing controls there, and the sidebar is focused on selecting a model and building a visual.
2. **Given** the analyst clicks the **Modelling** nav item, **When** the workspace opens, **Then** they see the available datasets, the existing fact models, the existing common models, and controls to create a new model, create a new common model, and edit any listed model.
3. **Given** the analyst opens an existing model for editing from Modelling, **When** they save a valid change, **Then** the model hot-reloads and the change is reflected the next time they build a visual in Studio (persistence round-trip survives a page reload).
4. **Given** a model was selected in Studio, **When** the analyst edits and saves that model in Modelling and returns to Studio, **Then** Studio reflects the updated model without requiring a manual reload of the page.

---

### User Story 2 - Pick a source from available datasets instead of typing S3 paths (Priority: P2)

When creating a new model (or repointing an existing one), the author does not want to remember and hand-type `s3://…/*.parquet` paths and formats. From the Modelling workspace they browse the datasets already present in the bucket, pick one, and have the model's `source` (path + format) filled in for them. Once a source is chosen, the columns that source actually exposes are surfaced so the author can turn them into dimensions and measures without guessing column names.

**Why this priority**: Typing exact object paths and formats is the single most error-prone step in authoring a model today, and the bucket contents are already known to the system. Removing that friction is the highest-leverage "delight" improvement after the workspace itself exists. It depends on US1 (the workspace) but delivers value on its own once datasets are pickable.

**Independent Test**: Start a new model in Modelling, choose a dataset from the browsable list, and confirm the model's source path and format are populated correctly and its real columns become available to reference — without the author typing any path.

**Acceptance Scenarios**:

1. **Given** a new-model flow in Modelling, **When** the author opens the dataset picker, **Then** they see the datasets/objects known in the bucket (with enough context — path, format, and whether any model already reads them — to choose confidently).
2. **Given** the author selects a dataset, **When** the selection is applied, **Then** the model's source path and format are set to that dataset and the author does not have to type them.
3. **Given** a source has been chosen and is reachable, **When** the author proceeds to define dimensions/measures, **Then** the actual columns of that source (with their data types) are shown as insertable references.
4. **Given** a chosen source is unreachable or malformed, **When** the author tries to use it, **Then** they get a clear, non-blocking message explaining the source could not be read, and can still continue editing.

---

### User Story 3 - Import a common model into a fact model without hand-writing YAML (Priority: P2)

An author building a fact model wants to reuse a shared dimension set (e.g. geography) that already exists as a common model, rather than redefining those dimensions. From the model editor they pick a common model and the dataset within it, indicate which of their own columns matches the shared key, and the import is wired up for them. The affordance is prominent and guided rather than a commented-out YAML hint.

**Why this priority**: Common (conformed) dimensions already exist in the system, but importing them today means understanding the `dimension_imports` YAML shape. Making import a first-class, guided action multiplies the value of common models. It is independent of US2 and can be tested on its own against an existing common model.

**Independent Test**: With at least one common model present, edit a fact model, use the import affordance to attach a common model's dataset on a chosen join key, and confirm the imported dimensions become usable on that model — without the author hand-writing the import block.

**Acceptance Scenarios**:

1. **Given** a fact model open in the editor and at least one common model exists, **When** the author opens the import affordance, **Then** they can choose a common model and one of its datasets to import.
2. **Given** the author selects a common model dataset and specifies the matching key column on their model, **When** they apply the import, **Then** the model gains the shared dimensions and validates successfully (or reports a clear error if the key does not match).
3. **Given** no common models exist yet, **When** the author looks for the import affordance, **Then** they see a clear empty state pointing them to create a common model first.

---

### User Story 4 - Write dimension and measure expressions with intellisense (Priority: P2)

While authoring a model, the author writes expressions (measures, and computed/typed dimensions) with the same assisted completion the Measure Lab already offers: typing `pl.`, `.`, or `pl.col("` surfaces suggestions drawn from the source's real columns and the supported expression methods, with live validation telling them whether the expression resolves. This assistance is available in the model authoring flow, not only when adding a measure to a single visual.

**Why this priority**: Expressions are where authoring goes wrong most subtly (wrong column name, unsupported method, silent typo). Bringing the existing completion + live-validation experience into model authoring closes the loop between "pick a dataset" and "write correct logic against it". It builds on US2 (real columns must be known) but is independently demonstrable.

**Independent Test**: In the model authoring flow, type a partial expression and confirm suggestions appear for the source's real columns and for expression methods, that accepting a suggestion inserts correctly, and that an invalid expression is reported as invalid while a valid one is reported as valid — before the model is saved.

**Acceptance Scenarios**:

1. **Given** a model source with known columns, **When** the author types `pl.col("` in an expression field, **Then** they are offered that source's real column names, filtered by what they have typed.
2. **Given** the author has referenced a column, **When** they type `.`, **Then** they are offered supported expression methods (aggregations, casts, string/date helpers, etc.).
3. **Given** the author writes an expression, **When** they pause, **Then** they are told whether it is valid/resolvable, and an invalid expression cannot be silently saved into the model.
4. **Given** the author accepts a suggestion, **When** it is inserted, **Then** the caret lands in a sensible position (e.g. inside the parentheses of a call) so they can keep typing.

---

### User Story 5 - Plain-text YAML editing remains first-class (Priority: P1)

A power user who already thinks in the model YAML wants to edit the raw file directly — paste a whole model, restructure it, add fields the guided UI does not surface — and have it validated and saved exactly as today. The guided affordances (dataset picker, import, intellisense) augment this raw editing surface rather than replacing it; nothing the guided flow can do is a dead end that forces the user away from the text.

**Why this priority**: This is an explicit hard requirement from the request ("Editing via plain text should be supported"). Regressing raw YAML editing would break existing authors and violate the semantic-layer-as-contract principle. It is P1 because it is a non-negotiable constraint on every other story, and must remain continuously testable.

**Independent Test**: Open any model's raw YAML, make a valid textual edit, confirm live validation and save/hot-reload behave exactly as before; make an invalid edit and confirm it is reported and not silently persisted.

**Acceptance Scenarios**:

1. **Given** an existing model, **When** the author opens it, **Then** the full raw YAML is available to edit directly.
2. **Given** the author edits the raw YAML to something valid, **When** they save, **Then** it is persisted and hot-reloaded exactly as in the current editor.
3. **Given** the author edits the raw YAML to something invalid, **When** validation runs, **Then** the error is shown and the invalid content is not silently accepted.
4. **Given** the author uses a guided affordance (dataset pick, import, suggestion), **When** the action completes, **Then** the resulting change is visible and editable in the raw YAML — the two views stay consistent.

---

### Edge Cases

- **Unreachable / malformed source**: choosing or referencing a dataset the engine cannot read must degrade gracefully — the author is told the source is not reachable but is not blocked from continuing to edit text.
- **Empty bucket / no datasets**: the dataset picker must show a sensible empty state rather than an empty control.
- **No common models yet**: the import affordance must guide the author to create one rather than presenting an empty picker.
- **Name collisions**: creating a model/common model whose name already exists must be refused with a clear message (consistent with existing behaviour), and the guided flow must surface that refusal.
- **Deleting a common model still imported by a fact model**: must remain refused with a clear message identifying the importers.
- **Deleting a fact model**: authors must be warned that saved visuals pointing at it will stop working.
- **Guided edit conflicting with hand-edited YAML**: if the author has hand-edited the YAML into a state a guided action assumes is different (e.g. the `source` block is missing), the guided action must fail safe (clear message) rather than corrupt the document.
- **Switching workspaces mid-edit**: leaving the editor (to another workspace or a different model) with unsaved edits must warn and require confirmation before the edits are discarded; it must never silently lose or silently persist them.
- **A dataset that is unmapped (no model reads it)** and a dataset already backing a model must both be selectable as a source; the picker informs but does not forbid.

## Requirements *(mandatory)*

### Functional Requirements

**Information architecture**

- **FR-001**: The application MUST present a top-level workspace named **Modelling** in place of the current "Data" mode, and this workspace MUST be the home for browsing datasets and managing the semantic layer.
- **FR-002**: The Studio workspace MUST NOT contain model-creation or YAML-editing controls; the "edit yaml", "new model", and "new common model" actions MUST be removed from Studio and available only from Modelling.
- **FR-003**: Studio MUST continue to let users select an existing model and build/save visuals against it, unchanged in capability.
- **FR-004**: The Modelling workspace MUST expose, as first-class actions: create a new fact model, create a new common (shared-dimension) model, and edit any existing fact model or common model.
- **FR-005**: The Modelling workspace MUST retain the existing data-overview capability (which bucket objects exist and which models read them) — the rename MUST NOT drop the current Data Explorer capability.

**Dataset selection**

- **FR-006**: When authoring a model, users MUST be able to choose the model's source by selecting from the datasets known in the bucket, without hand-typing a path. Bucket objects MUST be grouped into datasets by common prefix/partition and offered as a glob source (e.g. `path/*.parquet`); a dataset group MUST be expandable so the author can instead pick a single exact object as the source.
- **FR-007**: Selecting a dataset (grouped glob) or an individual object MUST populate the model's source path and format from that selection.
- **FR-008**: After a source is chosen and is reachable, the system MUST surface that source's real columns and their data types as insertable references for dimensions and measures.
- **FR-009**: The dataset picker MUST present enough context per dataset (path/glob, format, object count, and whether a model already reads it) for the author to choose confidently, and MUST handle the empty-bucket case gracefully.

**Common-model import**

- **FR-010**: Users MUST be able to import a common model's dataset into a fact model through a guided affordance, selecting the common model, the dataset, and the matching key column on their own model — without hand-writing the import block.
- **FR-011**: Applying an import MUST result in the fact model gaining the shared dimensions and validating, or a clear error if the key/shape is invalid.
- **FR-012**: When no common models exist, the import affordance MUST show a guiding empty state rather than an empty picker.

**Expression intellisense**

- **FR-013**: The model authoring editor MUST offer assisted completion for expressions anywhere the caret sits in a polars-expression context within the YAML (measures, expression-based/typed dimensions, filters), triggered by `pl.`, `.`, and `pl.col("`, drawing column suggestions from the chosen source's real columns and method suggestions from the supported expression methods.
- **FR-014**: Accepting a suggestion MUST insert it correctly and place the caret in a sensible position for continued typing.
- **FR-015**: The system MUST give live validity feedback on an expression before the model is saved, and MUST NOT silently save an invalid expression into a model.

**Raw YAML editing (must-keep)**

- **FR-016**: Users MUST be able to view and edit the complete raw YAML of any fact model or common model directly, with the same validation and save/hot-reload behaviour available today.
- **FR-017** *(revised 2026-07-11)*: ~~There MUST NOT be a separate structured-form~~ Fact-model authoring MUST lead with a guided, stepped form; the raw YAML editor remains first-class and reachable in one click from the form and from every model card. The form MUST NOT hand-assemble YAML text: it edits a structured spec that the server renders to canonical YAML, and an existing model's yaml MUST re-open into the form losslessly (comments/formatting excepted, with the loss stated at save time).
- **FR-018**: Invalid raw YAML MUST be reported and MUST NOT be silently persisted; valid YAML MUST persist and hot-reload as today.

**Cross-cutting**

- **FR-019**: Creating a model or common model whose name collides with an existing one MUST be refused with a clear message surfaced in the guided flow.
- **FR-020**: Deleting a fact model MUST warn that dependent saved visuals will break; deleting a common model still imported by a fact model MUST be refused with a message naming the importers.
- **FR-021**: When the author navigates away from the editor with unsaved changes, the system MUST warn and require confirmation before discarding them. In-progress editing state MUST NOT be persisted unless the author saves, MUST NOT silently leak into model files, and saved changes MUST survive a page reload.
- **FR-022**: All model and common-model changes MUST continue to flow exclusively through model/common-model files as the semantic-layer contract — no authoring path may let the query builder reference source columns that were never declared in a model.


#### Guided form (redesign, 2026-07-11)

- **FR-023**: Creating or editing a fact model MUST open a guided form that walks the author through ordered steps: (1) name + source dataset, (2) joined datasets, (3) common-model imports, (4) dimensions & measures, (5) review & save. Steps MUST be revisitable, and a step with incomplete required input MUST block NEXT with a stated reason rather than failing later.
- **FR-024**: Every relationship the form captures (source⇄join, model⇄common-model anchor) MUST be defined as one or more column pairs whose two sides are chosen independently from each side's introspected schema — columns that do not share a name MUST be first-class, not an escape hatch. When a side's schema is unreachable the picker MUST degrade to free text without blocking.
- **FR-025**: The dimensions step MUST offer the post-join scan's real columns as toggleable dimensions (with label and type), MUST mark columns already provided by an imported common model as such, and MUST NOT lose dimension attributes the form does not surface (spine, geo, description) when re-saving an existing model.
- **FR-026**: The review step MUST show the exact YAML that saving will persist, together with its validation verdict; SAVE MUST be disabled while the document is invalid.
- **FR-027**: From anywhere in the form the author MUST be able to hand the current state to the raw YAML editor ("edit yaml directly") and continue there; the handed-over text arrives as an unsaved edit with revert-to-disk still available.
- **FR-028**: Leaving the form with in-progress edits MUST warn before discarding (same guarantee as FR-021); nothing is persisted until SAVE writes the generated yaml.
- **FR-029**: Common (shared-dimension) models MUST get the same form-first treatment: a guided form covering the bundle's datasets, the relationships between them (as column pairs per FR-024), and each dataset's dimensions — with FR-025's attribute preservation, FR-026's review gate, FR-027's yaml hand-over, and FR-028's leave guard all applying equally.

### Key Entities *(include if feature involves data)*

- **Fact model**: A queryable semantic model (source + dimensions + measures, optionally importing shared dimensions). The primary artifact authored in this feature.
- **Common model (dimension bundle)**: A reusable set of datasets (source + dimensions, no measures) that fact models import to share conformed dimensions. Authored and edited in the same workspace.
- **Dataset / bucket object**: A physical object (or glob of objects) in the storage bucket, with a path and format, that can back a model's source. Enumerated for the picker; may or may not already be read by a model.
- **Source column**: A real column (name + data type) exposed by a model's resolved source, surfaced to drive dimension/measure authoring and expression completion.
- **Dimension**: A declared attribute of a model (categorical, time, or otherwise) usable in the query builder.
- **Measure**: A declared expression on a model (an aggregation/calculation) usable in the query builder.
- **Dimension import**: A binding from a fact model to a common model's dataset on a key column, producing shared dimensions on the fact model.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user can create a working new model — from choosing a dataset to a valid, saved, queryable model — without typing any storage path by hand.
- **SC-002**: Studio contains zero model-authoring controls; 100% of model create/edit/delete actions are performed from the Modelling workspace.
- **SC-003**: A user can attach an existing common model to a fact model in a guided flow without hand-writing the import block, and immediately use the imported dimensions.
- **SC-004**: When writing an expression against a chosen source, at least the source's real columns and the supported expression methods are offered as suggestions, and an invalid expression is flagged before save 100% of the time.
- **SC-005**: Every change made through a guided affordance is representable and editable in the raw YAML view, and raw-YAML editing retains full parity with today's editor (validate, save, hot-reload, delete, revert).
- **SC-006**: Saved model changes survive a page reload, and no in-progress guided-edit state is ever written to a model file unless the user saves.
- **SC-007**: Time to create a first working model for a new user is meaningfully reduced versus the current hand-typed-YAML flow (target: a new model backed by a real dataset created in under 2 minutes without prior knowledge of the bucket layout).

## Assumptions

- The rename is of the existing "Data" mode to "Modelling"; the current Data Explorer capability (bucket-object-to-model mapping) is absorbed into Modelling rather than removed or duplicated elsewhere.
- The set of "available datasets" is derived from the objects already enumerable in the configured bucket (the same source the current Data Explorer uses); no new external catalog is introduced.
- Model YAML remains the single editable contract; guided affordances are conveniences that read/write that YAML, not an alternative persistence format. No database-backed model store is introduced.
- The expression-completion vocabulary (supported methods) and the trusted-config eval boundary are inherited from the existing Measure Lab; this feature surfaces the same capability in a new place and does not widen who may author expressions (models remain single-user, developer-authored config).
- Both fact models (`models/*.yaml`) and common models (`dimensions/*.yaml`) are in scope for the guided authoring experience.
- The existing no-bundler, vanilla-ES-module, hand-rolled-UI frontend approach and the FastAPI + Polars backend are retained; this feature does not introduce a build step or framework.
- "Delightful" is scoped to the three named affordances (dataset selection, common-model import, expression intellisense) plus the workspace consolidation; a full visual form-builder that hides YAML entirely is explicitly out of scope for this iteration.
- Existing safeguards (name-collision refusal, delete guards, live validation, hot-reload) are reused, not reinvented.
