# Feature Specification: Polars Pipeline Module

**Feature Branch**: `014-polars-pipeline-module`

**Created**: 2026-07-17

**Status**: Draft

**Input**: User description: "Lightweight pipeline module: hosted polars python transformation scripts over the existing S3 bucket. Not a low-code builder — users write real polars scripts, the platform hosts, runs, and documents them. Core capabilities: (1) an update/materialization method per pipeline target with modes: full table replace, or upsert (key-based merge) with configurable delete handling (ignore deletes / delete rows missing from source / soft-delete flag / delete by predicate) — targets are Delta tables by default (delta-rs merge), replace mode may also write parquet; (2) source-to-target traceability: users can optionally define layers (e.g. bronze/silver/gold) and organize bucket prefixes accordingly; each pipeline declares field-level lineage (target field -> source fields + human-readable transformation description) which is validated against the actual output schema on every run and written into the target semantic model YAML in a dedicated section, so transformation logic is documented where the model lives; pass-through fields auto-suggested. (3) users can visualise lineage as a graph: datasets/models as nodes across layers, pipelines as edges, expandable to field-level lineage. Execution: manual trigger + run history/status first (no scheduler in v1), runs serialized as background jobs respecting the single-writer deployment. Trust model follows the existing frame: carve-out — pipeline scripts are real Python (eval/exec territory) so authoring/executing them is admin-gated, with provenance/audit like model measures. Fits into the MODELLING workspace UI."

## Constitution Notes

Two principles are deliberately touched by this feature and are called out here
per the governance rule ("where a feature genuinely needs to violate one, say
so explicitly"):

- **Principle VI (trusted-config security boundary)**: pipeline scripts are a
  new eval-capable construct — real Python authored by users. This feature
  **re-opens Principle VI explicitly**: pipeline script authoring, editing, and
  deletion are gated behind the **admin role**, exactly like the existing
  `frame:` measure carve-out, and a pipeline script is never accepted from any
  unauthenticated or lower-trust path. The constitution amendment should be
  recorded when this feature ships.
- **Principle II (lazy evaluation, pushdown by default)**: pipelines
  *materialize* data — that is their purpose, not a violation of the query
  path. The query engine's lazy/pushdown behavior is untouched; pipeline runs
  are explicit, user-triggered write jobs. Scripts still operate on lazy scans
  of their sources, so reads within a run keep pushdown where the script
  allows it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Author and run a hosted transformation (Priority: P1)

An admin (data engineer) writes a transformation script in the Modelling
workspace: the script reads one or more existing datasets in the bucket and
produces a single output table. They declare the pipeline's target (a bucket
location and storage format) with materialization mode **replace**, save the
pipeline, and trigger a run. The platform executes the script as a background
job, writes the output to the target, and shows run status and history
(started, duration, rows written, success/failure with error detail). The new
target dataset is immediately scannable by the existing semantic layer — a
model can point at it like any other bucket object.

**Why this priority**: This is the irreducible core — hosting, running, and
materializing a script. Every other capability (upsert, lineage, graph)
attaches to a pipeline that already exists and runs. On its own it already
delivers real value: data prep moves from invisible external scripts into the
platform, with history and audit.

**Independent Test**: Create a pipeline whose script joins two seeded demo
datasets and aggregates them; run it; verify the target object appears in the
bucket, a model over it queries correctly, and the run history records the
run. Trigger a failing script and verify the failure is captured with its
error and the previous target data is not corrupted.

**Acceptance Scenarios**:

1. **Given** an admin in the Modelling workspace, **When** they create a
   pipeline with a script, declared sources, a target, and mode `replace`,
   then trigger a run, **Then** the run executes in the background, the
   target dataset is (re)written atomically, and the run appears in history
   as succeeded with row count and duration.
2. **Given** a pipeline whose script raises an error, **When** it is run,
   **Then** the run is recorded as failed with the error message, and the
   target dataset remains exactly as it was before the run.
3. **Given** a run in progress, **When** a run of a *different* pipeline is
   triggered, **Then** it is queued and executed after the current run
   completes — never concurrently. **Given** a pipeline that already has a
   run queued or running, **When** it is triggered again, **Then** the new
   trigger is refused (that pipeline already has a pending run) rather than
   queued a second time.
4. **Given** a non-admin user (author or viewer), **When** they attempt to
   create, edit, or run a pipeline, **Then** the action is refused; viewers
   and authors can still see pipeline definitions, run history, and lineage.
5. **Given** any pipeline create/edit/delete or run, **Then** the acting
   account is recorded in the audit trail, consistent with how model measure
   provenance works today.

---

### User Story 2 - Incremental upsert with delete handling (Priority: P2)

An admin configures a pipeline target with materialization mode **upsert**:
they declare the target's key column(s), and choose a delete-handling policy —
**ignore** (rows absent from this run's output are left in place), **sync**
(rows in the target whose keys are absent from this run's output are removed),
**soft-delete** (such rows are kept but flagged in a designated column), or
**predicate** (rows matching a declared condition are removed before merge).
Running the pipeline merges the script's output into the existing target by
key: matched rows are updated, new rows inserted, and the delete policy
applied.

**Why this priority**: Replace-only pipelines force full recomputation and
lose history semantics; upsert is the single most requested "real pipeline"
behavior and is what makes incremental bronze→silver flows viable. It depends
on US1 (a pipeline must exist and run) but nothing else.

**Independent Test**: Seed a target via a replace run; switch to upsert; run
with output containing one changed row, one new row, and one missing row;
verify update/insert occurred and the missing row was handled per each of the
four policies.

**Acceptance Scenarios**:

1. **Given** an upsert pipeline with key `order_id` and policy `ignore`,
   **When** a run's output updates one existing key and adds one new key,
   **Then** the target has the updated row, the new row, and all untouched
   rows intact.
2. **Given** policy `sync`, **When** a run's output omits keys that exist in
   the target, **Then** those rows are removed from the target.
3. **Given** policy `soft-delete` with a declared flag column, **When** a
   run's output omits existing keys, **Then** those rows remain with the flag
   set, and rows re-appearing in a later run have the flag cleared.
4. **Given** policy `predicate`, **When** a run executes, **Then** target rows
   matching the declared predicate are removed before the merge is applied.
5. **Given** an upsert run whose output contains duplicate key values or null
   keys, **Then** the run fails with a clear error before any target
   modification.
6. **Given** an upsert-mode target, **Then** the platform requires a storage
   format that supports transactional merge; `replace` mode remains available
   for simple-file formats.

---

### User Story 3 - Declared field-level lineage documented in the target model (Priority: P3)

An admin declares, on a pipeline, field-level lineage: for each target field,
which source dataset field(s) it derives from and a human-readable description
of the transformation ("net revenue = gross − returns, FX-normalised").
Fields whose name matches a source field are auto-suggested as pass-through so
the declaration burden stays low. On every run, declarations are validated
against the actual output schema — a declared field missing from the output,
or an output field with no declaration, is flagged on the run. When a semantic
model exists over the pipeline's target, the validated lineage (source fields,
transformation descriptions, source layer/dataset) is written into a dedicated,
pipeline-owned section of that model's YAML, so the transformation logic is
documented where the model lives and visible to everyone who reads the model.
Users can also optionally assign each pipeline's sources and target to a named
**layer** (e.g. bronze/silver/gold) to organize datasets.

**Why this priority**: Traceability is the feature's differentiator, but it is
only meaningful once pipelines exist and run (US1/US2). It is deliberately
declaration-based, not inferred from code — validation against the real output
schema on every run is what keeps the docs honest.

**Independent Test**: Declare lineage on a pipeline including one pass-through
and one derived field; run it; verify the target model's YAML gains the
lineage section with those entries. Remove a field from the script's output;
run again; verify the run is flagged with the specific validation mismatch and
the stale entry is not silently kept as valid.

**Acceptance Scenarios**:

1. **Given** a pipeline with declared lineage and a model over its target,
   **When** a run succeeds and validation passes, **Then** the model YAML
   contains a dedicated lineage section listing each target field's source
   field(s) and transformation description, without disturbing any
   hand-authored content elsewhere in the file.
2. **Given** a declared field that the run's output no longer contains,
   **When** the run completes, **Then** data is still written, the run is
   flagged with a lineage validation warning naming the field, and the
   model's lineage section marks that entry as stale rather than presenting
   it as current.
3. **Given** output fields with names matching source fields, **When** the
   admin edits lineage, **Then** those fields are pre-suggested as
   pass-through and can be accepted or overridden individually.
4. **Given** a pipeline whose target has no semantic model yet, **When** it
   runs, **Then** lineage is validated and stored with the pipeline, and is
   written into the model YAML later, as soon as a model over that target
   exists.
5. **Given** layers are defined, **When** a pipeline's sources/target are
   assigned to layers, **Then** those assignments appear wherever the
   dataset is described (data overview, lineage section, graph) — and
   pipelines remain fully usable with no layers defined at all.

---

### User Story 4 - Lineage graph visualization (Priority: P4)

Any signed-in user opens a lineage view in the Modelling workspace and sees a
graph: datasets/models as nodes, organized into columns by layer when layers
are defined, with pipelines drawn as edges from each source to its target.
Selecting a node or edge reveals detail — for a pipeline edge, its
transformation summary and run status; for a dataset node, expanding it shows
field-level lineage: which upstream fields feed each field, following a field
across multiple hops (bronze → silver → gold).

**Why this priority**: The graph is the payoff for the lineage declarations,
but it is a read-only view over data produced by US1–US3 and can ship last
without blocking anything.

**Independent Test**: With two chained pipelines (A→B, B→C) and declared
lineage, open the graph; verify both hops render with correct direction and
layer columns; expand a field on C and verify it traces through B back to A's
source field.

**Acceptance Scenarios**:

1. **Given** pipelines with declared sources and targets, **When** the graph
   is opened, **Then** every pipeline appears as a directed edge from each of
   its sources to its target, and datasets with a layer render grouped by
   layer.
2. **Given** a dataset node is expanded, **When** a field is selected,
   **Then** its upstream sources are highlighted across all hops, with each
   hop's transformation description available.
3. **Given** a pipeline's most recent run failed, **When** the graph is
   viewed, **Then** that pipeline's edge indicates the failure state.
4. **Given** a viewer-role user, **When** they open the graph, **Then** it is
   fully readable but offers no editing or run controls.

---

### Edge Cases

- Script produces an empty output: replace writes an empty target (valid);
  upsert with `sync` would delete everything — the run is halted and flagged
  for confirmation-by-configuration (an explicit per-pipeline "allow empty
  sync" opt-in) rather than silently truncating.
- Script output schema differs from the existing target schema on upsert
  (missing/extra/retyped columns): run fails with a schema diff before any
  modification.
- Two pipelines declare the same target: refused at save time — a target has
  at most one owning pipeline.
- Pipeline deleted: its run history and audit records are retained; the
  model's lineage section is marked as orphaned (pipeline no longer exists),
  not silently removed.
- Target model YAML is hand-edited inside the pipeline-owned lineage section:
  the next successful run rewrites that section; hand edits elsewhere in the
  file are never touched.
- A source dataset is itself another pipeline's target that has never
  successfully run (empty/missing): the run fails with a clear "source
  unavailable" error.
- Run triggered while the app restarts mid-run: the run is recorded as
  interrupted/failed on restart, never left permanently "running".
- Script runs longer than the configured run timeout: the run is terminated
  and recorded as timed out.
- Circular pipeline chains (A→B and B→A): allowed to exist (each run is
  manual and independent), but the graph renders the cycle without hanging.

## Requirements *(mandatory)*

### Functional Requirements

**Pipeline definition & authoring**

- **FR-001**: Admins MUST be able to create, edit, and delete pipelines, each
  consisting of: a name, a transformation script, one or more declared source
  datasets, exactly one target (bucket location + storage format), a
  materialization configuration, optional layer assignments, and optional
  field-level lineage declarations.
- **FR-002**: The transformation script's contract is to *produce a table*;
  the platform — not the script — performs all writes to the target according
  to the materialization configuration. Scripts have no other sanctioned
  write path.
- **FR-003**: Pipeline create/edit/delete and run-trigger MUST require the
  admin role (Principle VI re-opened — see Constitution Notes). All roles MAY
  view pipeline definitions, run history, lineage, and the graph.
- **FR-004**: Every pipeline create/edit/delete and every run MUST be
  recorded in the audit trail with the verified acting account, consistent
  with existing measure provenance.
- **FR-005**: Pipeline authoring MUST live in the Modelling workspace,
  alongside model and common-dimension authoring.

**Materialization**

- **FR-006**: The platform MUST support materialization mode `replace`: the
  target is atomically replaced by the run's output; a failed run leaves the
  previous target intact.
- **FR-007**: The platform MUST support materialization mode `upsert`: the
  run's output is merged into the target by declared key column(s) — matched
  rows updated, unmatched rows inserted.
- **FR-008**: Upsert MUST support four delete-handling policies: `ignore`
  (default), `sync` (delete target rows whose keys are absent from the
  output), `soft-delete` (flag such rows in a declared column, clearing the
  flag if the key reappears), and `predicate` (delete target rows matching a
  declared condition before merging).
- **FR-009**: Upsert mode MUST require a target storage format with
  transactional merge semantics; the default target format for new pipelines
  is the transactional table format the platform already reads (Delta).
  `replace` mode MUST additionally support plain parquet targets.
- **FR-010**: An upsert run whose output contains null or duplicate key
  values MUST fail before modifying the target. An upsert run whose output
  schema is incompatible with the existing target MUST fail with a schema
  diff before modifying the target.
- **FR-011**: An upsert run with policy `sync` and an empty output MUST NOT
  delete the target's rows unless the pipeline explicitly opts in to
  empty-sync.

**Execution & run history**

- **FR-012**: Runs MUST be manually triggered (no scheduler in this feature)
  and executed as background jobs, strictly serialized — at most one pipeline
  run executes at a time platform-wide; a trigger for a different pipeline
  queues behind the current run, while a trigger for a pipeline that already
  has a queued or running run is refused rather than queued again.
- **FR-013**: Each run MUST record: pipeline, triggering account, start/end
  time, status (queued / running / succeeded / failed / timed out /
  interrupted), rows written, delete-policy effects (rows deleted/flagged),
  lineage validation outcome, and full error detail on failure.
- **FR-014**: Run history MUST be viewable per pipeline by all roles, and a
  run in progress MUST report live status.
- **FR-015**: Runs MUST have a configurable timeout; a timed-out or
  app-interrupted run MUST be terminally recorded, never left "running".

**Traceability & lineage**

- **FR-016**: Admins MUST be able to declare, per target field: its source
  field(s) (dataset + field) and a human-readable transformation
  description. Declarations are optional per field.
- **FR-017**: The platform MUST auto-suggest pass-through lineage for output
  fields whose names match a declared source's fields; suggestions are
  accepted or overridden explicitly, never silently persisted.
- **FR-018**: On every run, declared lineage MUST be validated against the
  actual output schema; mismatches (declared-but-absent fields,
  undeclared output fields) MUST be flagged on the run without blocking the
  data write.
- **FR-019**: When a semantic model exists over a pipeline's target, the
  platform MUST write the validated lineage into a dedicated, clearly
  pipeline-owned section of that model's YAML — source fields, transformation
  descriptions, source dataset/layer, and owning pipeline — preserving all
  hand-authored content outside that section. Stale or orphaned entries MUST
  be marked as such, not silently dropped or presented as current.
- **FR-020**: Users MUST be able to define named layers (an ordered list,
  e.g. bronze/silver/gold) and assign datasets to them; every lineage surface
  (data overview, model lineage section, graph) MUST reflect assignments.
  Layers are optional everywhere.

**Lineage graph**

- **FR-021**: The platform MUST render a lineage graph: dataset/model nodes
  (grouped by layer when assigned), pipeline edges from each source to its
  target, with the latest run status visible per pipeline.
- **FR-022**: The graph MUST support field-level expansion: selecting a field
  highlights its upstream lineage across multiple pipeline hops, with each
  hop's transformation description accessible.
- **FR-023**: The graph MUST be viewable by all roles and MUST render cycles
  and disconnected datasets without failure.

### Key Entities

- **Pipeline**: named unit of transformation — script, declared sources,
  single target, materialization config, optional lineage declarations and
  layer assignments. Owned/edited only by admins; at most one pipeline per
  target.
- **Pipeline Run**: one execution record — status lifecycle, timing, row
  counts, delete effects, lineage validation result, error detail, triggering
  account. Append-only history.
- **Materialization Config**: mode (`replace` | `upsert`), key column(s),
  delete policy (`ignore` | `sync` | `soft-delete` | `predicate`) with its
  parameters (flag column / predicate), empty-sync opt-in, target format.
- **Lineage Declaration**: target field → source field(s) + transformation
  description; carries validation state (current / stale) per run.
- **Layer**: ordered named grouping (e.g. bronze/silver/gold) datasets can be
  assigned to; purely organizational.
- **Model Lineage Section**: the pipeline-owned block inside a semantic model
  YAML documenting the target's lineage; regenerated from validated
  declarations, never hand-merged.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A data engineer can move an existing external polars prep
  script into the platform — create the pipeline, declare its target, run it,
  and query the result through a model — in under 15 minutes without touching
  anything outside the app.
- **SC-002**: All four delete-handling policies produce exactly correct
  target states on a seeded update/insert/delete test matrix (100% of the
  policy × change-type combinations).
- **SC-003**: A failed run never corrupts a target: across any induced
  failure (script error, schema mismatch, timeout, restart mid-run), the
  target remains byte-for-byte queryable with its pre-run content.
- **SC-004**: Lineage docs cannot silently drift: 100% of runs whose output
  schema diverges from declarations surface the mismatch on the run and in
  the model's lineage section.
- **SC-005**: Given a three-layer chain of pipelines, a user can trace any
  gold-layer field back to its bronze source(s) in the graph in under 30
  seconds, including reading each hop's transformation description.
- **SC-006**: Zero pipeline capabilities are reachable by non-admin accounts
  for mutation; 100% of pipeline mutations and runs appear in the audit log.

## Assumptions

- **Trust model**: pipeline scripts are real Python and therefore
  application-code trust level; admin-only authoring *and* admin-only run
  triggering in v1 (running executes the script, so triggering is held to the
  same bar). Relaxing run-trigger to the author role is a possible follow-up,
  explicitly out of scope now.
- **Script contract**: a script produces exactly one output table from its
  declared sources; multi-output pipelines are out of scope for v1. Declared
  sources are the documented inputs (used for lineage/graph); the script is
  trusted not to be validated/sandboxed against reading other objects.
- **Scheduling**: out of scope; manual trigger only. Serialized execution
  (one run at a time) is acceptable given the deliberate single-writer
  deployment.
- **Target formats**: new targets default to the transactional table format
  the platform already supports as a source (Delta); upsert requires it;
  replace also supports parquet. CSV targets are out of scope.
- **Lineage is declared, not inferred**: no attempt to parse the script and
  derive lineage automatically; honesty comes from schema validation on every
  run plus pass-through auto-suggestion.
- **Layer definitions are global** (one ordered list for the deployment),
  assigned per dataset; bucket prefix conventions are the user's choice and
  are not enforced.
- **The existing seeded demo data** will gain at least one demo pipeline
  chain so the feature is demonstrable out of the box, consistent with how
  every prior feature seeds a working example.
- **README** is updated as part of this feature (constitution: Development
  Workflow), and the Principle VI amendment is recorded when the feature
  ships.
