# Implementation Plan: Safe Measure Compilation

**Branch**: `claude/safe-measure-compilation-qhzd5t` | **Date**: 2026-07-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/008-safe-measure-compilation/spec.md`

## Summary

Replace the `eval`-based scalar measure path with an AST-allowlisting compiler (`app/measure_dsl.py::compile_measure`) that never calls `eval`/`exec`/`compile`, and route both inline (query-time) and model (saved) measures through it. The one existing exec-based capability — "framed" measures (`frame`/`frame_emits`, multi-statement derived-frame snippets) — is carved out as an authenticated-only construct: reachable exclusively through the new auth-gated model-measure save path, never through inline/query-time measures. This closes the actual worst case in the current code — an **unauthenticated** `POST /query` can run arbitrary Python today via an inline `frame` snippet (see `tests/test_engine.py::test_inline_framed_measure`, currently passing) — while keeping the one real production measure that needs it (`months_to_75`) working. A new minimal API-key dependency gates model-measure create/update/delete; a new append-only SQLite table records provenance (author, version, timestamp) alongside every YAML write, without changing model YAML's role as the executable source of truth (Constitution Principle I). The 34 existing `models/*.yaml` measures are rewritten from method-chain `pl` syntax to the new DSL grammar.

## Technical Context

**Language/Version**: Python 3.12 (backend only — this feature has no frontend surface; it's a compiler + API/auth change). `ast` (stdlib) for parsing; no new third-party dependency.

**Primary Dependencies**: FastAPI + Polars (existing). No new library — the compiler is a hand-written `ast.NodeVisitor`, not a parser-generator or expression library, per the brief ("never call eval").

**Storage**: `models/*.yaml` remains the sole source of truth for measure DSL text (unchanged role). New SQLite table `measure_provenance` in the existing `cash_intel.db` (via `VisualStore`, extended) — append-only audit log, never read at query time.

**Testing**: pytest (existing harness). New `tests/test_measure_dsl.py` (compiler correctness + red-team suite, no S3/moto needed — pure AST/Polars-schema tests). Extended `tests/test_engine.py` (inline-frame rejection, framed-model-measure execution), `tests/test_api.py` (auth-gated measure endpoints, provenance), `tests/test_semantic.py` (Measure.expr() routing), `tests/test_store.py` (provenance table CRUD). Existing model YAML fixtures/tests continue to run against the rewritten DSL syntax.

**Target Platform**: Unchanged — single Docker image, one uvicorn worker.

**Project Type**: Web application (FastAPI backend); this feature touches backend only (`app/`), no `app/static/` changes required (Measure Lab already just posts `{name, expr, frame?}` dicts to `/query`; it needs no client change to lose frame support server-side, though its UI copy may want a follow-up note — out of scope here).

**Performance Goals**: Preserve lazy/pushdown (Constitution II) — `compile_measure` only inspects a pre-computed `collect_schema()` (metadata, no scan) and builds `pl.Expr` objects; no additional data materialization versus today's `eval` path.

**Constraints**: No sandboxing (Tier 3 explicitly out of scope — a documented extension point only). No execution-mode flag/legacy fallback (FR-008). No file from any sandbox/worker-pool/subprocess branch is touched (none exist on this branch — confirmed).

**Scale/Scope**: One new module (`app/measure_dsl.py`, ~250-350 LOC including the function-builder table), edits to `app/semantic.py`, `app/engine.py`, `app/api/models.py`, `app/api/query.py`, `app/api/deps.py`, `app/config.py`, `app/store.py`; rewrite of `expr:` text in 6 YAML files (34 measures); one new SQLite table.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment |
|-----------|------------|
| **I. Semantic layer is the only contract** | ✅ Preserved. Model YAML remains the sole executable definition of a measure; the new provenance table is an audit log, not an alternate read path. No query path gains the ability to reference an undeclared column. |
| **II. Lazy/pushdown by default** | ✅ Unaffected. `compile_measure` builds `pl.Expr` objects and validates against `collect_schema()` (metadata only); no full materialization is introduced anywhere in the compile or execution path. |
| **III. Every feature ships with tests** | ✅ Planned — correctness suite, red-team suite, auth suite, provenance suite, framed-carve-out suite, and a full-repo regression pass on the rewritten `models/*.yaml` measures (see Phase 1 test plan / quickstart.md). |
| **IV. Browser-verified before done** | ⚠️ Scoped down deliberately: this feature has no new UI. "Browser verification" here means driving the existing Studio/Measure Lab UI end-to-end (build a visual using a rewritten model measure; submit an inline measure) to confirm no regression, plus hitting the new auth-gated endpoints via HTTP client (curl/TestClient) since there is no auth UI to click through. Documented explicitly rather than skipped. |
| **V. Ephemeral vs. persisted** | ✅ N/A — no new interactive/session state is introduced. |
| **VI. Trusted-config eval boundary** | 🔶 **Explicitly re-opened, as the constitution itself anticipates.** This feature is the "re-open this principle explicitly" event Principle VI calls for. New position: (a) inline/query-time measures are no longer "trusted config" at all — they are now compiled through an allowlist and can never reach `eval`/`exec`, regardless of who calls `/query`; (b) model measures remain effectively trusted-config-level for their *scalar* expression (still no wider than the DSL — the DSL applies to both, Tier 1 doesn't grant more scalar power); (c) the one exception is the narrow, pre-existing `frame` construct, which keeps today's eval-level trust *but only reachable behind the new auth+provenance gate* — never from a query-time request body. Constitution update proposed in Phase 1 (see research.md). |
| **VII. Feature branch per development** | ✅ On `claude/safe-measure-compilation-qhzd5t`. |

**Technology-constraint checks**: one router per resource preserved (measure-mutation endpoints stay in `app/api/models.py`; auth dependency lives in `app/api/deps.py` or a new `app/auth.py`, following the existing "small shared helper" pattern of `deps.py`); runtime state stays centralized in `app/registry.py` (provenance store hangs off `registry.store`, same as visuals/dashboards); SQLite remains a persistence store, not a data source (Technology Constraints section) — the new table fits this exactly (it's app metadata, not queried business data).

**Result**: PASS, with Principle VI re-opened by design (not a violation — the constitution explicitly names this feature's exact scenario as the correct way to re-open it). Complexity Tracking below documents the one deliberate, scoped exception (frame carve-out) so it isn't a silent widening.

## Project Structure

### Documentation (this feature)

```text
specs/008-safe-measure-compilation/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md         # Phase 1 output
├── quickstart.md         # Phase 1 output
├── contracts/            # Phase 1 output
│   ├── compile_measure.md    # DSL grammar + compiler function contract
│   └── measures-api.md       # Model-measure CRUD + auth contract
├── checklists/
│   └── requirements.md  # from /speckit-specify
└── tasks.md              # /speckit-tasks output (not created here)
```

### Source Code (repository root)

```text
app/
├── measure_dsl.py        # NEW: compile_measure(), MeasureCompileError, AST allowlist
│                          #      visitor, function-builder table, size/depth guards
├── semantic.py            # Measure.expr() routes non-framed measures through
│                          #      compile_measure; compile_expr/compile_frame narrow
│                          #      to the authenticated framed-measure path only
├── engine.py               # run_query(): inline measures always go through
│                          #      compile_measure and reject "frame"/"frame_emits"
│                          #      keys outright; model measures branch on
│                          #      meas.frame_source (framed vs. plain)
├── auth.py                 # NEW: require_measure_author FastAPI dependency
│                          #      (API-key header check against config.API_KEY)
├── config.py                # + API_KEY setting (env CI_API_KEY)
├── store.py                  # + measure_provenance table + record/list methods
├── registry.py                # unchanged (store already hung off registry.store)
└── api/
    ├── deps.py                 # unchanged, or hosts the auth dependency instead
    │                          #   of a new auth.py — decided in Phase 1
    ├── models.py                # measure endpoints require auth; add PUT/DELETE
    │                          #   for model measures; validate-on-save via
    │                          #   compile_measure (or compile_expr+compile_frame
    │                          #   for the frame carve-out)
    └── query.py                  # unchanged (engine.run_query already does the work)

models/*.yaml              # 34 measures rewritten to DSL grammar; the one framed
                          #   measure (clinical_ops_recruitment.yaml) re-saved
                          #   through the authenticated path to get its first
                          #   provenance record (functionally unchanged YAML shape)

tests/
├── test_measure_dsl.py    # NEW: correctness + red-team suites (pure, no S3)
├── test_engine.py          # + inline-frame-rejected tests (replacing the 3
                          #   existing inline-frame-success tests), + framed
                          #   model-measure-through-DSL-for-plain-part tests
├── test_api.py              # + auth-gated measure CRUD, + provenance assertions
├── test_semantic.py          # + Measure.expr() routing tests
└── test_store.py              # + measure_provenance CRUD tests
```

**Structure Decision**: Single existing FastAPI app layout (`app/`), no new top-level directories. One new backend module (`measure_dsl.py`) plus one new small auth module, following the existing "small router-adjacent helper" pattern already used by `deps.py`. No frontend changes (the Measure Lab UI already just posts measure dicts; it doesn't need to change to lose the `frame` field support server-side — a follow-up UI note, not this feature).

## Complexity Tracking

> Documenting the one deliberate constitutional exception so it is never mistaken for scope creep later.

| Exception | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|---------------------------------------|
| Model measures may use `frame`/`frame_emits` (still `eval`/`exec`-based) behind the auth+provenance gate, bypassing the Tier 2 allowlist for that one construct | The one real production measure (`months_to_75`) needs multi-step group-by/window/date-arithmetic logic no small expression allowlist can safely express; deleting it outright breaks a working feature with no replacement (Tier 3 is out of scope) | Expanding the Tier 2 DSL to cover group-by/window/frame-reshaping was rejected — it turns a small, provably-safe expression allowlist into a general query-language subset, multiplying the audit surface for the exact vulnerability class this feature exists to close. Accepting the regression (breaking `months_to_75` with no path forward until an unscheduled Tier 3) was rejected as an unnecessary loss of working functionality when a narrow, auth-gated carve-out fully contains the risk (a `frame` snippet is only ever accepted from an authenticated save, never a query-time request body) |
