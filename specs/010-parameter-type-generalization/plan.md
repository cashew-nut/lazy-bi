# Implementation Plan: Generalize Visual Parameters to More Types and DSL Positions

**Branch**: `claude/visual-parameter-declarations-6mztd3` | **Date**: 2026-07-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/010-parameter-type-generalization/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Add a `type` field (`int` | `float` | `string`) to a visual parameter's declaration, and make `param('name')` usable anywhere a literal is already legal in the measure DSL — comparisons, `if_()`, `coalesce()`, `where()`, `cast()`'s value argument — instead of only `lag()`'s periods argument. The technical core is small: register `param` as a normal function in both `_FUNCTIONS`/`_WINDOW_FUNCTIONS` tables, resolving to `pl.lit(value)` from the same pre-validated `parameter_values` dict the compiler already trusts; `lag()`'s periods argument keeps its own extra check (must resolve to a genuine Python `int`, not a numerically-whole `float`) on top of the same lookup. Every layer that currently assumes parameter values are `int` (declaration validation, query-time resolution, dashboard definition-equality) becomes type-aware, with an absent `type` field always treated as `int` for full backward compatibility with everything spec 009 already shipped. The one deliberately-navigated wrinkle: JSON (and JavaScript, which has a single numeric type) cannot distinguish a whole float from an int syntactically, so a `float`-typed parameter must accept JSON integers as valid members and coerce them to genuine Python `float` before they ever reach the compiler — the declared `type` governs eligibility, never the incidental shape a value arrived in.

## Technical Context

**Language/Version**: Python 3.12 (backend, `app/`), vanilla ES modules / no build step (frontend, `app/static/js/`) — unchanged from spec 009, no new language surface.

**Primary Dependencies**: FastAPI, Polars 1.42 (`pl.lit()` for the new general parameter-literal construction — already used identically for every other constant in the DSL), stdlib `ast` — no new dependency.

**Storage**: SQLite, unchanged schema — a parameter's `type` field is one more key in the same free-form JSON `spec`/`items` columns spec 009 already introduced; no migration, additive only.

**Testing**: pytest + FastAPI `TestClient`, extending `tests/test_measure_dsl.py`, `tests/test_engine.py`, `tests/test_api.py`, `tests/test_static.py` alongside the existing spec-009 parameter test cases — not a new test module.

**Target Platform**: Linux server (unchanged), browser (unchanged).

**Project Type**: Web application (FastAPI backend + vanilla-JS frontend), single repo — same as spec 009 and every prior feature.

**Performance Goals**: No new performance target. `param()` resolution is still one dict lookup producing one `pl.lit()` per reference, now just reachable from more AST positions — negligible cost, no change to scan/pushdown behavior (Principle II).

**Constraints**: Same as spec 009 — parameter values are never `eval`'d, only ever substituted from an already-validated, already-typed allowlist (Principle VI). The new wrinkle this feature must navigate without weakening that: JSON's single numeric type means a `float`-typed parameter's value-validation must accept an integer-shaped JSON number and canonically coerce it to Python `float`, while an `int`-typed parameter must still reject anything that isn't a genuine JSON integer — the coercion step, not the DSL compiler, is what keeps `lag()`'s "must be a real positive int" rule airtight against a merely-numerically-whole float.

**Scale/Scope**: Same single-tenant scale as the rest of the product; no new concurrency/multi-tenancy concerns.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|---|---|---|
| I. Semantic Layer Is the Only Contract | ✅ Pass | No new raw-column access; parameters remain visual-declared context feeding only into already-model/inline-measure-gated expressions. |
| II. Lazy Evaluation, Pushdown by Default | ✅ Pass | `param()` still resolves to a single `pl.lit()` wherever it appears — no new scan, no new materialization, regardless of how many DSL positions now accept it. |
| III. Every Feature Ships With Tests | ✅ Pass (planned) | `quickstart.md` enumerates DSL/engine/API/frontend test cases extending the existing spec-009 parameter test suites. |
| IV. Browser-Verified Before "Done" | ✅ Pass (planned) | `quickstart.md` requires a browser walkthrough covering a `float` parameter in a comparison and a `string` parameter in `coalesce()`/equality, plus a backward-compatibility check against a pre-existing (untyped) spec-009 visual. |
| V. Ephemeral vs. Persisted State Is a Deliberate Choice | ✅ Pass | No new state category introduced — `type` rides along on the same persisted parameter declaration spec 009 already classified; portal-mode session-local override behavior (spec 009) is unchanged and type-agnostic by construction. |
| VI. Trusted-Config Security Boundary Is Explicit, Never Silently Widened | ✅ Pass, no amendment needed | `param()` becoming reachable from more DSL positions is still zero new eval-based surface — it is the same allowlisted `pl.lit(pre-validated value)` substitution as before, just registered in the general function tables instead of one bespoke call site. The compiler still never receives a parameter's declared value list, only a value the caller (`engine.py`) has already validated and coerced to the declared type — the trust boundary described in spec 009's amendment is unchanged, only its reach within the grammar widens. |
| VII. Feature Branches, One Development Per Branch | ✅ Pass | Continues on `claude/visual-parameter-declarations-6mztd3`, the same branch spec 009 shipped on, per this session's established convention. |

No violations — Complexity Tracking table below is empty.

## Project Structure

### Documentation (this feature)

```text
specs/010-parameter-type-generalization/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   └── compile_measure_param_types.md
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

Single-project web application, same structure as spec 009 — every touched file already exists and already has parameter-handling code from spec 009 to extend:

```text
app/
├── measure_dsl.py          # + param registered as a general function in _FUNCTIONS/
│                            #   _WINDOW_FUNCTIONS (resolves to pl.lit(value)); lag()'s
│                            #   periods argument keeps its own int-genuineness check on
│                            #   top of the same lookup
├── engine.py                # resolve_parameter_values() becomes type-aware: new
│                            #   PARAM_TYPES, param_type_ok(), coerce_param_value()
│                            #   (public — reused by api/visuals.py, api/dashboards.py);
│                            #   the plain (non-window) inline-measure compile_measure()
│                            #   call site gains parameter_values= (previously only the
│                            #   window-measure call site had it, since param() couldn't
│                            #   appear in aggregate-mode measures before this feature)
├── api/
│   ├── visuals.py            # _validate_visual_spec's parameter checks become
│   │                          #   type-aware (reuses engine.param_type_ok/coerce_param_value)
│   └── dashboards.py          # _same_param_def compares type as well as values/default
│                              #   (reuses the same engine helpers)
│   # api/query.py, api/models.py: no shape change — QueryRequest.parameters and
│   # MeasureCheckIn.parameters are already untyped list[dict], type rides along for free;
│   # check_measure() already passes parameter_values unconditionally (not gated on
│   # window mode), so it already works correctly for the new aggregate-mode positions
└── static/js/
    ├── builder.js              # renderParameters(): + a type selector per parameter;
    │                            #   values-list parsing becomes type-dispatched
    │                            #   (parseInt/parseFloat/trimmed-string-split);
    │                            #   addParameter() defaults new parameters to type: "int"
    ├── measurelab.js             # completion hint includes the parameter's type
    └── dashboard.js               # sameParamDef() compares type; type-aware sort when
                                   # collapsing a shared parameter's declared values

tests/
├── test_measure_dsl.py     # + param() in comparisons/if_/coalesce/where/cast-value-arg,
│                            #   across int/float/string; lag()'s type-genuineness rejection
├── test_engine.py           # + resolve_parameter_values() type validation/coercion cases
├── test_api.py                # + visual/dashboard type-aware validation and conflict cases
└── test_static.py              # + frontend type-dispatch structural assertions, matching
                                # the existing spec-009 intellisense-style test convention
```

**Structure Decision**: No new modules, packages, or projects — purely additive changes to files spec 009 already introduced parameter-handling into. `app/measure_dsl.py` and `app/engine.py` remain the load-bearing changes; `app/api/*` and `app/static/js/*` changes are the type-aware extensions of validation/comparison logic already living there.

## Complexity Tracking

*No Constitution Check violations — table intentionally empty.*
