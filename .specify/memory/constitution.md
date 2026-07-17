# Cash Intelligence (lazy-bi) Constitution

## Core Principles

### I. The Semantic Layer Is the Only Contract
The query builder, dashboards, and any future client NEVER touch raw source
columns directly — every dimension and measure the UI can use must be
declared in a model YAML file first. Models are the editable, hot-reloadable
contract between raw S3 data (parquet/csv/Delta) and everything downstream.
New data sources are onboarded by writing or editing a model, not by adding
special cases to the query engine.

### II. Lazy Evaluation, Pushdown by Default (NON-NEGOTIABLE)
Every query path scans data lazily (Polars `LazyFrame`) so only the columns
and row-groups a query actually needs leave the bucket — this is the product's
reason for existing. Any change to the engine, joins, or spine logic must
preserve predicate/projection pushdown; a feature that forces a full-table
materialization needs a documented reason and a benchmark showing the cost.
Performance claims are validated against a large real dataset (the 13M-row
NYC taxi benchmark), not synthetic toy data.

### III. Every Feature Ships With Tests
No feature is done without pytest coverage added alongside it (semantic
parsing, engine behavior against a real moto-emulated bucket, store CRUD, and
the API surface via TestClient). Bug fixes found during manual verification
get a regression test, not just a code fix — see the sankey NUL-character and
404-vs-400 fixes, both of which landed as tests.

### IV. Browser-Verified Before "Done"
A feature is not complete when the code compiles and tests pass — it is
complete when it has been driven end-to-end in a real browser: the golden
path, the persistence round-trip (save, cold-reload, confirm), and a zero
console-errors check. Screenshots and explicit before/after checks are the
default way of proving a UI change works, not a substitute for asking the
user to check manually.

### V. Ephemeral vs. Persisted State Is a Deliberate Choice
Every feature that adds interactive state must explicitly decide, up front,
whether that state is saved or throwaway — and a page refresh must prove it.
Cross-filtering, focus mode, and the dashboard grain override are
intentionally session-only and reset on reload; saved visuals, dashboards,
and named views are intentionally persisted to SQLite. Do not let ephemeral
interaction state leak into saved payloads, and do not silently persist
something meant to be a throwaway view.

### VI. Trusted-Config Security Boundary Is Explicit, Never Silently Widened
**Amended by the safe-measure-compilation feature** (see
`specs/008-safe-measure-compilation/`) — this principle's own rule ("any
change that lets a less-trusted actor influence measure expressions must
re-open this principle explicitly") is why this amendment exists. The
boundary is now three-way, not one flat trust level:

- **Scalar measure expressions — model or inline, no distinction** compile
  through an allowlisting AST compiler (`app/measure_dsl.py`) that never
  calls `eval`/`exec`/`compile`. This is no longer "trusted config, same
  level as application code" for either kind — it's structurally incapable
  of running arbitrary code, so an inline/query-time measure supplied by an
  unauthenticated caller is exactly as safe as a saved one. Saving a model
  measure grants governance (provenance, auth), not extra language power.
- **The one narrower exception**: a model measure's `frame:` block (a
  multi-step derived-frame Python snippet — see "Measures over an
  intermediary frame" in the README) keeps the pre-existing `eval`/`exec`
  path, at application-code trust level, exactly as this principle
  originally described. **Amended by the session-auth feature** (see
  `specs/011-session-auth-rbac/`): the gate is no longer the spec-008
  shared secret (`X-API-Key` + `X-Author`, now retired) but the **admin
  role of the real identity system** — a `frame:` save requires an
  authenticated admin account, whether it arrives via the measure endpoint
  or a raw model-YAML write. Never inline, never from a query-time request
  body, regardless of credentials.
- **Model YAML itself** (dimensions, joins, structure — everything but a
  measure's scalar expression) remains trusted, developer-authored
  configuration — but "trusted" is now enforced, not assumed: the raw
  model/dimension YAML routes, which spec 008 left open and which could
  smuggle a `frame:` block past the measure gate, require the admin role
  (spec 011 closed this gap). The set of actors who can influence
  eval-capable configuration therefore *narrowed* with this amendment.

Any future change widening who may reach the `frame:` path, or introducing
a new eval-based construct, must re-open this principle explicitly, exactly
as before.

**Amended by the polars-pipeline-module feature** (see
`specs/014-polars-pipeline-module/`) — a genuinely new eval-capable
construct, not a variant of `frame:`: a pipeline's `script:` is a whole,
unsandboxed Python script (`app/pipeline_runner.py` execs it directly),
application-code trust level, exactly like the `frame:` carve-out above.
Creating, editing, deleting, **and triggering a run of** a pipeline all
require the admin role — running executes the script, so triggering a run
is held to the same bar as authoring one, not the lower bar a measure save
gets. Never inline, never from an unauthenticated or lower-trust path,
regardless of credentials. Process isolation (each run in its own killable
subprocess) is a crash-safety and timeout-enforcement measure, not the trust
boundary — the admin gate is.

### VII. Feature Branches, One Development Per Branch
Every development effort — a feature, a refactor, a fix worth its own
history — happens on its own `feature/*` branch and merges via PR. Work is
not committed directly to `main`.

## Technology Constraints

- **Backend**: FastAPI + Polars, one router per resource under `app/api/`,
  runtime state centralized in `app/registry.py`. SQLite is the persistence
  store for visuals/dashboards/publications — not a data source.
- **Frontend**: hand-rolled SVG charts and vanilla ES modules loaded natively
  by the browser. No bundler, no framework, no build step — this is a
  deliberate simplicity choice, not an oversight. New chart types follow the
  existing renderer + shared frame/pivot/dispatch pattern.
- **Packaging**: a single Docker image (`python:3.12-slim`), state kept
  outside the image (SQLite volume, host-mounted `models/`), one uvicorn
  worker by design (in-process emulator + SQLite both want a single writer).
  Scaling out is only supported against an external S3 endpoint.
- **Data formats**: parquet, csv, and Delta Lake sources must all be
  first-class in the engine, not parquet-with-afterthoughts.

## Development Workflow

- Describe the feature (`/speckit-specify`), resolve ambiguity before
  planning (`/speckit-clarify` when the ask is non-trivial), plan
  (`/speckit-plan`), break into tasks (`/speckit-tasks`), then implement
  (`/speckit-implement`) and verify in the browser per Principle IV.
- Update `README.md` as part of the feature, not as a follow-up — the README
  is the living description of the running system and has been kept current
  with every shipped feature so far.
- A feature is reported done with what changed, what was verified, and any
  known rough edges — not just "implemented."

## Governance

This constitution reflects practices already proven out over this project's
history; it does not invent new process for its own sake. Amendments should
be grounded the same way — in a real decision or a real incident, recorded
here with the reasoning kept alongside the rule. Specs and plans must stay
consistent with these principles; where a feature genuinely needs to violate
one (e.g. Principle II for a use case that cannot be pushed down), say so
explicitly in that feature's spec rather than quietly drifting.

**Version**: 1.3.0 | **Ratified**: 2026-07-10 | **Last Amended**: 2026-07-17
