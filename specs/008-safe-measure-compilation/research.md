# Phase 0 Research: Safe Measure Compilation

## R1 — DSL grammar covers all 33 non-framed existing measures

**Decision**: The DSL surface described in the brief (aggregations `sum, mean, min, max, count, count_distinct, median, std, var, first, last`; combinators `col, where, if_, coalesce, cast`; arithmetic/comparison/boolean operators) is sufficient for every non-framed `expr:` in `models/*.yaml` today. Verified by inspecting all 34 `expr:` lines directly:

| Existing syntax (method-chain) | New DSL syntax (function-call) |
|---|---|
| `pl.len()` | `count()` |
| `pl.col("total_amount").sum()` | `sum(total_amount)` |
| `pl.col("fare_amount").mean()` | `mean(fare_amount)` |
| `pl.col("tip_amount").sum() / pl.col("fare_amount").sum()` | `sum(tip_amount) / sum(fare_amount)` |
| `pl.col("order_id").n_unique()` | `count_distinct(order_id)` |
| `pl.col("event_count").filter((pl.col("event_type") == "screened") & (pl.col("scenario") == "actual")).sum()` | `sum(where(event_count, event_type == "screened" and scenario == "actual"))` |
| `(pl.col("unit_price") - pl.col("unit_cost")) * pl.col("quantity")).sum()` | `sum((unit_price - unit_cost) * quantity)` |
| `pl.col("months_to_75").median()` (the framed measure's final aggregation) | stays on the eval-based path — see R2 (not run through the DSL; its input column doesn't exist in the base schema, only in the derived frame) |

**Rationale**: `where(value, predicate)` covers every `.filter(...)` use (all 6 filtered-aggregate measures in `clinical_ops_recruitment.yaml`); `count_distinct` covers every `.n_unique()`; plain arithmetic covers every ratio/computed measure. No measure uses casts, `coalesce`, or `if_` today, but those stay in the allowlist for future authoring per the original brief.

**Alternatives considered**: Accepting method-chain syntax directly (parsing `.sum()`-style calls as `ast.Attribute` + `ast.Call`) was rejected — it would require allowing `ast.Attribute` nodes, which is precisely the escape hatch (`__class__`, `__globals__`, etc.) the compiler must reject outright. A prefix function-call grammar has no attribute access at all, which is the whole point.

## R2 — Framed-measure carve-out mechanics

**Decision**: `Measure.expr()` branches on `frame_source`:
- `frame_source is None` → `self.expr_source` compiles via `compile_measure(text, schema, alias=name)` (new DSL, no eval).
- `frame_source is not None` → both `frame_source` (via existing `compile_frame`, still `exec`-based) and `expr_source` (via existing `compile_expr`, still `eval`-based) keep using today's mechanism unchanged. This whole branch is reachable **only** for `Measure` objects loaded from a model YAML that was written through the authenticated save path (see R4) — never for a dict built from an inline-measure request body.

`app/engine.py::run_query`'s inline-measure handling (`inline[m]["expr"]`, `inline[m].get("frame")`) changes to: build `inline[m]` from the request as today, but if `inline[m].get("frame")` or `inline[m].get("frame_emits")` is present, raise `QueryError` immediately — before any compilation is attempted — naming the reason ("frame-based measures require an authenticated model-measure save; they are never available inline"). The non-frame `inline[m]["expr"]` always compiles via `compile_measure`, never `compile_expr`.

**Rationale**: Directly implements FR-012 / User Story 3. Keeps the change small — no new frame-handling code, just gating the existing mechanism's two entry points differently, and adding one guard clause in the inline path.

**Alternatives considered**: A generic "capability flag" system for arbitrary future carve-outs was rejected as speculative — there is exactly one construct (`frame`) that needs this, and the brief is explicit about not building generalized sandbox infrastructure. A single `if` guard is proportionate.

## R3 — Where the auth dependency lives

**Decision**: New `app/auth.py` (not folded into `deps.py`) exposing `require_measure_author(x_api_key: str = Header(...), author: str = Header(...)) -> str` (returns the author label on success), used as a FastAPI `Depends()` on the four mutating measure routes. Kept separate from `deps.py` because `deps.py`'s existing role is 404-lookup helpers (`get_model`/`get_bundle`), a different concern from identity/secret-checking, and a distinct file makes the "this is the pluggable auth seam, replace me later" boundary obvious.

**Mechanics**: `config.API_KEY = os.environ.get("CI_API_KEY", "")`. If `config.API_KEY` is empty (not configured), `require_measure_author` always raises 401 — fail closed by default, forcing an operator to explicitly opt in by setting `CI_API_KEY`, rather than silently defaulting to "no auth" in a fresh checkout. If configured, the dependency compares the `X-API-Key` header against it (constant-time compare via `secrets.compare_digest`) and requires a non-empty `X-Author` header as the self-declared author label, returning it for the route to use in the provenance write.

**Rationale**: Matches the brief's "minimal, pluggable" instruction and the maintainer's confirmed choice. Fail-closed-when-unconfigured avoids the common footgun of a security feature being a no-op out of the box.

**Alternatives considered**: Session cookies/login flow rejected (maintainer's confirmed choice — heavier than "minimal"). A no-op stub dependency was rejected for the same reason — the maintainer chose to actually enforce something now, not defer it further.

## R4 — Provenance table shape and write path

**Decision**: Extend `VisualStore` (renamed conceptually but not in code — it's already the app's one persistence class) with:

```sql
CREATE TABLE IF NOT EXISTS measure_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    measure TEXT NOT NULL,
    action TEXT NOT NULL,       -- 'create' | 'update' | 'delete'
    expr TEXT,                  -- NULL for 'delete'
    frame TEXT,                 -- NULL unless a framed measure
    frame_emits TEXT,           -- JSON list, NULL unless framed
    author TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
```

`version` = `1 + COALESCE((SELECT MAX(version) FROM measure_provenance WHERE model = ? AND measure = ?), 0)`, computed inside the same connection/transaction as the insert. The model-measure route writes the YAML file, then writes the provenance row, inside one request — if the YAML write fails, no provenance row is written; if the YAML write succeeds and the provenance write fails, the request still 500s (surfacing the inconsistency rather than silently swallowing it), matching the store's existing no-migration-framework, direct-SQL style.

**Rationale**: Matches the maintainer-confirmed hybrid decision and the existing `store.py` conventions exactly (autoincrement int id, ISO8601 text timestamps, one `SCHEMA` string with `CREATE TABLE IF NOT EXISTS`). Append-only (no `UPDATE`/`DELETE` on this table itself) gives the "full history" stretch goal from the original brief for free, at the cost of one row per save — acceptable at this app's scale (single-user/developer authoring, not high-frequency writes).

**Alternatives considered**: A single mutable `model_measures` row with `version`/`updated_by` columns (the brief's literal suggested schema) was rejected — it can't answer "what did this measure look like at version 3," which the brief calls out as the actually-valuable story ("prefer an append-only history... Full history is a stretch goal; the version int + author is the floor"). Append-only gets the stretch goal at negligible extra cost.

## R5 — Constitution amendment

**Decision**: Principle VI needs a documented amendment (part of this feature's implementation, not a follow-up) reflecting the three-way split in R2: inline measures are no longer trusted-config-level at all (allowlisted, `eval`-free); model measures' scalar expressions are equally allowlisted; the single `frame` construct remains eval-level but is now access-controlled rather than ambiently trusted. This is Principle VI's own required action ("must re-open this principle explicitly rather than ship quietly") — done here, not deferred.

**Rationale**: The constitution is a living document per its own Governance section ("Amendments should be grounded... in a real decision... recorded here"). This feature is exactly that real decision.
