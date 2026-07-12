# Implementation Plan: Visual Parameters for Measures

**Branch**: `claude/visual-parameter-declarations-6mztd3` | **Date**: 2026-07-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/009-visual-parameters/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Let a dashboard developer declare an enum-style parameter (name + list of
allowed integer values + default) on a visual, reference it from a `lag()`
measure via a new `param('name')` DSL call, and let viewers toggle the
selected value from a control on the visual. Dashboards can save the
current selection into a named view (extending the existing filter-view
mechanism), and when two visuals on one dashboard declare an identically-
defined same-named parameter, the dashboard presents one shared control
and pushes the selection to both — but refuses to let two visuals coexist
if their same-named parameter's definitions differ. The technical
approach threads a pre-resolved `dict[str, int]` into the existing safe
DSL compiler exactly the way `partition_by`/`order_by` already are,
special-cases `param()` recognition to `lag()`'s one argument (so it is
structurally unreachable anywhere else), and reuses the existing
`inline_measures`-style "declarations travel with the query" pattern and
the existing `views[].filters`-style "saved per named view" pattern — no
new persistence concept, no new endpoint, no eval-based construct.

## Technical Context

**Language/Version**: Python 3.12 (backend, `app/`), vanilla ES modules / no build step (frontend, `app/static/js/`) — matches the existing codebase exactly, no new language surface.

**Primary Dependencies**: FastAPI (API layer), Polars 1.42 lazy `LazyFrame`/`Expr` (query engine), stdlib `sqlite3` (persistence), stdlib `ast` (the existing measure DSL compiler, `app/measure_dsl.py`) — no new dependency added.

**Storage**: SQLite (`cash_intel.db`), unchanged schema — parameters live inside the existing free-form JSON `spec`/`items` columns on `visuals`/`dashboards` (see `data-model.md`); no migration.

**Testing**: pytest + FastAPI `TestClient`, matching the existing suite layout (`tests/test_measure_dsl.py`, `tests/test_engine.py`, `tests/test_api.py`) — new cases added alongside existing `lag()`/window-measure tests, not a new test module.

**Target Platform**: Linux server (single Docker image, single uvicorn worker, per constitution "Technology Constraints"), browser (no build step, ES modules loaded natively).

**Project Type**: Web application (FastAPI backend + hand-rolled vanilla-JS frontend), single repo — matches every prior feature in this codebase.

**Performance Goals**: No new performance target — parameter resolution is one dict lookup per `lag()` call, negligible next to the existing query cost; must not defeat Polars lazy pushdown (Principle II — see Constitution Check below, this feature doesn't touch the scan/filter/group-by path at all, only the post-group-by `.over()` step window measures already use).

**Constraints**: Parameter values must never be `eval`'d or otherwise interpreted beyond an allowlist-membership check (Principle VI); parameter values are integers only in v1 (no new Polars dtype handling needed).

**Scale/Scope**: Same single-tenant, developer-and-a-handful-of-viewers scale as the rest of the product — no concurrency/multi-tenancy concerns beyond what already exists (single SQLite writer, per constitution).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|---|---|---|
| I. Semantic Layer Is the Only Contract | ✅ Pass | Parameters are declared on the visual, referenced only from measures that already go through the model/inline measure path. No raw-column access introduced. |
| II. Lazy Evaluation, Pushdown by Default | ✅ Pass | `param()` resolution is a dict lookup that feeds `lag()`'s existing `.shift(periods).over(...)` — the same post-group-by window step that already exists for literal periods. No new scan, no new materialization, no change to predicate/projection pushdown. |
| III. Every Feature Ships With Tests | ✅ Pass (planned) | `quickstart.md` enumerates DSL, engine, and API test cases to add alongside existing `lag()`/window-measure coverage; Phase 2 tasks will include them explicitly. |
| IV. Browser-Verified Before "Done" | ✅ Pass (planned) | `quickstart.md` §4 defines the required browser walkthrough (single visual, shared-parameter dashboard, conflict rejection, persistence round-trip, zero console errors) before this feature is reported done. |
| V. Ephemeral vs. Persisted State Is a Deliberate Choice | ✅ Pass | Explicitly decided in spec Assumptions: parameter selections follow the *persisted* named-view mechanism (like filters), not the ephemeral mechanisms (cross-filter, focus mode, dash-grain override). Portal (read-only) mode treats parameter changes as session-local, matching existing filter/view behavior there. |
| VI. Trusted-Config Security Boundary Is Explicit, Never Silently Widened | ✅ Pass, no amendment needed | `param()` is a new DSL surface but is **not** eval-based — it's one more allowlisted, structurally-restricted construct in the existing AST-walking compiler (`measure_dsl.py`), exactly like every function already there. The compiler never receives a parameter's raw declared-values list, only a value the caller (`engine.py`/`api/models.py`) has already validated against it — same trust posture as `partition_by`/`order_by` today. Because no eval-based construct is introduced and no less-trusted actor gains reach into the `frame:` path, this principle's re-open trigger is not tripped. |
| VII. Feature Branches, One Development Per Branch | ✅ Pass | Developed on `claude/visual-parameter-declarations-6mztd3`, per repo convention. |

No violations — Complexity Tracking table below is empty.

## Project Structure

### Documentation (this feature)

```text
specs/009-visual-parameters/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   ├── compile_measure_param.md
│   └── parameters-api.md
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

This is a single-project web application (existing structure, no new
top-level directories) — every touched file already exists:

```text
app/
├── measure_dsl.py          # + param() special-case in _fn_lag, + unknown_parameter
│                            #   ErrorKind, + referenced_parameter_names()
├── engine.py                # + parameter declaration/selection validation and
│                            #   resolution in run_query(), threaded into the
│                            #   existing window-measure compile_measure() call
├── api/
│   ├── query.py              # + QueryRequest.parameters / .parameter_values
│   ├── models.py             # + MeasureCheckIn.parameters; + referenced_parameter_names
│   │                          #   guard in _validate_measure_body (blocks model-measure save)
│   └── visuals.py            # + save-time validation of spec.query.parameters
│                             #   (dup names, default-in-values, unknown-param refs)
│   └── dashboards.py         # + save-time parameter-conflict validation across
│                             #   a dashboard's tiles (FR-015/FR-016)
├── store.py                  # unchanged (views' free-form dict already carries
│                             #   through a new "parameters" key with no code change)
└── static/js/
    ├── state.js               # + state.parameters, state.parameterValues
    ├── builder.js              # + parameters in buildQuery()/currentSpec()/loadVisual()
    ├── measurelab.js            # + param() insertion, save-to-model guard
    └── dashboard.js             # + dashParamUnion(), renderDashParams(), tileQuery()
                                 #   parameter merge, tile-add conflict check

tests/
├── test_measure_dsl.py     # + param()/lag() cases (contracts/compile_measure_param.md)
├── test_engine.py           # + parameter resolution/validation cases
└── test_api.py               # + /api/query, /api/measures/check, /api/visuals,
                              #   /api/models/{name}/measures, /api/dashboards cases
```

**Structure Decision**: No new modules, packages, or projects — this
feature is entirely additive changes to the existing single FastAPI
backend + vanilla-JS frontend, following the same file-per-concern layout
every prior spec (001-008) already used. `app/measure_dsl.py` and
`app/engine.py` are the load-bearing changes (Phase 0/1 above);
everything in `app/api/*` and `app/static/js/*` is plumbing that threads
the same `parameters`/`parameter_values` shape through existing endpoints
and UI flows.

## Complexity Tracking

*No Constitution Check violations — table intentionally empty.*
