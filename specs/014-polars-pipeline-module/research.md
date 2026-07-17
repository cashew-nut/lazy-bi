# Research: Polars Pipeline Module

Decisions resolving every open technical choice in the plan. No NEEDS
CLARIFICATION markers remained in the spec; these records ground the design
in the existing codebase.

## R1 — Pipeline definitions are YAML files in `pipelines/`, runs live in SQLite

**Decision**: A pipeline is one YAML file in a new top-level `pipelines/`
directory (host-mountable like `models/` and `dimensions/`), with the
transformation script embedded as a literal block scalar (`script: |`).
Loaded and hot-reloaded by the registry exactly like models. Run history and
audit live in SQLite (`cash_intel.db`, new `pipeline_runs` table + existing
`audit_events`).

**Rationale**: Mirrors the platform's established design language: files are
the editable, hot-reloadable contract (Constitution I, "models are onboarded
by writing YAML"); SQLite is state/audit, never a source of truth for
executable config. Reuses the whole model-editor UI machinery (live
validation, YAML editor, reload endpoint) nearly verbatim. `frame:` measures
already embed multi-line Python in YAML via the literal-block dumper
(`semantic.py`'s SafeDumper subclass), so the format precedent exists.

**Alternatives considered**: SQLite-stored definitions (breaks the
file-as-contract pattern, loses host-mount editing, complicates the eval
trust story — a DB row is less auditable than a file in git); separate `.py`
files next to a YAML manifest (splits one logical unit across two files,
doubles the editor surface).

## R2 — Execution: in-process FIFO worker thread; each run in a killable subprocess

**Decision**: The app owns a single daemon worker thread draining a FIFO
queue (strict platform-wide serialization, FR-012). For each run the worker
spawns a subprocess (`python -m app.pipeline_runner`), passing a JSON job
spec (pipeline definition + storage config) on stdin; the child executes the
script, performs the materialization write itself, and reports a single JSON
result line (row counts, output schema, error) on stdout. The parent
enforces the timeout by killing the process, and is the **only** writer of
run records to SQLite. On startup, any run row left `queued`/`running` is
terminally marked `interrupted` (FR-015).

**Rationale**: A plain thread cannot be killed in Python, so FR-015's hard
timeout requires a process boundary; a crashed or OOM'd script also cannot
take the app down. The subprocess reaches the embedded moto emulator over
HTTP (127.0.0.1:9600) like any client. Keeping all SQLite writes in the
parent preserves the deliberate single-writer posture. One-at-a-time
execution makes the Delta `AWS_S3_ALLOW_UNSAFE_RENAME` posture (already used
by seeding) safe for pipeline writes too.

**Alternatives considered**: thread-only execution (no enforceable timeout,
no crash isolation — fails FR-015); celery/arq/an external queue (new infra,
violates the single-image, no-moving-parts packaging constraint); FastAPI
`BackgroundTasks` (tied to request lifecycle, no queue semantics, dies with
the request cycle).

## R3 — Script contract: `sources` in, `output` out; the platform writes

**Decision**: The script executes via `exec()` in a namespace containing
`sources` (dict of declared source name → `pl.LazyFrame`, built with the same
`storage_options()` scans the engine uses), `pl`, and nothing else sanctioned.
It must assign `output` — a `pl.LazyFrame` or `pl.DataFrame`. The platform
collects the frame and performs the write per the materialization config; the
script has no sanctioned write path (spec FR-002).

**Rationale**: Follows `compile_frame()`'s exact pattern (namespace-injected
exec, result variable by convention, trusted-config level). Handing the
script LazyFrames keeps reads lazy/pushdown-capable (Constitution II).
Centralizing the write is what makes replace/upsert/delete policies
enforceable and failed runs non-corrupting.

**Alternatives considered**: script performs its own writes (unenforceable
materialization semantics, corruption on partial failure); a sandboxed DSL
(explicitly rejected by the feature premise — "not a low-code solution");
AST-allowlisting like the measure DSL (real pipelines need loops, functions,
multi-step logic — that's why `frame:` exists as an eval carve-out already).

## R4 — Materialization via `deltalake` 1.6.1 (already a dependency)

**Decision**:
- **replace + delta**: `write_deltalake(..., mode="overwrite",
  schema_mode="overwrite")` — a single transaction; readers see old or new,
  never partial.
- **replace + parquet**: buffer + single `put_object` (same as `seed.py`) —
  a single-object PUT is atomic on S3.
- **upsert (delta only)**: `DeltaTable.merge()` keyed on the declared key
  column(s): `when_matched_update_all` + `when_not_matched_insert_all`. A
  first upsert run against a target that doesn't exist yet creates it (an
  initial write, equivalent to `replace` for that one run). Delete policies
  map to: `ignore` → nothing extra; `sync` →
  `when_not_matched_by_source_delete()`; `soft_delete` →
  `when_not_matched_by_source_update({flag: true})`, with the merge input
  frame carrying a literal `flag = false` column (the flag is
  platform-managed and never present in the script's own output, so
  `update_all` needs it added explicitly to clear the flag on a reappearing
  key); `predicate` → `DeltaTable.delete(predicate)` executed before the
  merge.
- Pre-merge guards run on the collected output before any write: null/dup
  key check, schema compatibility diff vs. the existing target, and the
  empty-output + `sync` halt unless `allow_empty_sync` (FR-010, FR-011).

**Rationale**: `deltalake` 1.6.1 ships all four merge clauses used above;
this is the entire reason Delta is the default/required target format for
upsert — parquet upserts would mean read-modify-rewrite of whole files with
no transactional boundary. CSV targets stay out of scope.

**Alternatives considered**: hand-rolled parquet merge (no atomicity, full
rewrite cost); requiring replace-only in v1 (guts the feature's stated core
requirement #1).

## R5 — Lineage: declared in pipeline YAML, regenerated into a pipeline-owned model section

**Decision**: Lineage lives in the pipeline YAML (`lineage:` list — target
field, source fields as `source_name.field`, transform description). After a
successful run, the parent validates declarations against the runner-reported
output schema, flags mismatches on the run (never blocking the write), and
regenerates a single top-level `pipeline_lineage:` section in the matching
model's YAML — a comment-delimited, wholly pipeline-owned block written via a
new `semantic.replace_lineage_yaml()` (same comment-preserving text-surgery
family as `append_measure_yaml`/`replace_measure_yaml`). Stale/orphaned
entries are marked, not dropped. Target→model matching: exact path match for
delta roots; `fnmatch` of the model's source glob against the target key for
parquet. Pass-through suggestions come from comparing declared source schemas
with the target/last-run output schema — a suggestion endpoint, accepted
explicitly in the editor, never silently persisted (FR-017).

**Rationale**: The model YAML is where readers already look ("documented
where the model lives"); the existing measure-block text surgery proves the
comment-preserving single-section rewrite approach works. Validation on
every run is the drift guarantee the spec demands; hand-edits inside the
owned section being overwritten is declared behavior (spec edge case).

**Alternatives considered**: inferring lineage from the script's polars plan
(brittle against arbitrary Python — rejected in the spec itself); storing
lineage only in SQLite (docs would not live with the model, defeating
requirement #2); per-field YAML comments on dimensions (unownable —
impossible to regenerate safely next to hand edits).

## R6 — Graph: one aggregation endpoint + a hand-rolled SVG DAG renderer

**Decision**: `GET /api/lineage/graph` assembles nodes (datasets/models,
with layer), edges (pipeline source→target, with latest run status), and
per-node field lineage from loaded pipelines + models + run store. The
frontend renders a layered DAG in hand-rolled SVG (new `lineagegraph.js`):
columns by layer when layers exist, otherwise topological rank; field
expansion highlights upstream paths across hops client-side from the same
payload. Cycles are rendered by breaking rank ties (no layout hang, FR-023).

**Rationale**: Matches the no-framework, hand-rolled-SVG constitution
constraint; the sankey renderer already proves column-and-link layout is
tractable in this codebase. One payload keeps the graph read-only and
role-free (viewable by all).

**Alternatives considered**: a JS graph library (violates no-dependency
frontend rule); server-side layout (needless coupling — the client already
lays out sankeys).

## R7 — Layers: optional ordered list in `pipelines/layers.yaml`

**Decision**: A single optional file `pipelines/layers.yaml` declares the
deployment's ordered layer list (name + label). Pipelines tag their target
(and optionally each source) with a layer name; referencing an undeclared
layer is a load-time validation error. Everything works with the file absent
(FR-020). Editable via `GET/PUT /api/lineage/layers` (PUT admin) and the
Modelling UI.

**Rationale**: Global-ordered-list semantics (spec assumption) fit one small
file in the same hot-reload family; keeping assignment on the pipeline —
rather than inventing a dataset entity — avoids a new registry concept for
purely organizational metadata.

**Alternatives considered**: SQLite-stored layers (config in the wrong
store); per-dataset assignment registry (new entity for no additional
capability); hardcoded bronze/silver/gold (spec says user-defined).

## R8 — Authorization & audit

**Decision**: All pipeline mutations (create/edit/delete, layers PUT) and
run triggering require `require_role("admin")` — the same gate as `frame:`
saves and raw model-YAML writes (spec Constitution Notes; Principle VI
re-opened, amendment recorded at ship time). Reads (list, yaml GET, runs,
graph, layers GET) require any authenticated role; parse-only validation
endpoints likewise (they never execute anything). Every mutation and every
run trigger calls `authstore.record_audit(...)`; run rows additionally carry
the verified triggering account. `tests/test_role_matrix.py` grows the new
routes.

**Rationale**: Direct application of the existing three-way trust boundary;
no new mechanism needed.

## R9 — Demo content

**Decision**: Ship two demo pipelines in `pipelines/` forming a
bronze→silver→gold chain over the seeded sales data (e.g. raw sales →
cleaned/enriched silver orders via upsert, → gold daily revenue summary via
replace), plus a `layers.yaml`, plus a `pipeline_lineage` section landing in
a demo model — so US1–US4 are all demonstrable out of the box, consistent
with every prior feature's seeded example.
