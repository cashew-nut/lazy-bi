# API Contract Changes: Visual Parameters

All existing endpoints keep their current request/response shape unless
listed below. Every change is additive — old clients (and old saved
visuals/dashboards with no `parameters` anywhere) keep working unchanged
(FR-017).

## Changed: `POST /api/query` (`app/api/query.py`)

`QueryRequest` gains two optional fields:

```python
class QueryRequest(BaseModel):
    model: str
    dimensions: list = []
    measures: list[str] = []
    inline_measures: list[dict] = []
    filters: list[dict] = []
    sort: Optional[dict] = None
    limit: Optional[int] = None
    parameters: list[dict] = []          # NEW: [{name, values, default}]
    parameter_values: dict[str, int] = {}  # NEW: {name: selected int}
```

Both default to empty — a request with neither behaves exactly as today.

**Server-side handling** (`app/engine.py::run_query`, before any measure
compiles):

1. For each entry in `parameters`: `name` non-empty and unique within the
   list; `values` non-empty list of ints; `default` a member of `values`.
   Any violation → 400 (`QueryError`).
2. For each key in `parameter_values`: must name a declared parameter;
   its value must be a member of that parameter's `values`. Any
   violation → 400. This is the enforcement point for FR-010 — no
   parameter value the request supplies is ever used unvalidated.
3. Build `resolved = {p["name"]: parameter_values.get(p["name"], p["default"]) for p in parameters}`.
4. Pass `resolved` as `parameter_values=` into every
   `measure_dsl.compile_measure(...)` call for window-mode measures (the
   existing call site at the query's `.over()` step). Non-window measures
   never receive it — `param()` is illegal outside `lag()`, so it would
   never be looked up there anyway.

## Changed: `POST /api/measures/check` (`app/api/models.py`)

`MeasureCheckIn` gains one optional field so the Measure Lab's live-typing
check can validate a `param(...)`-referencing draft measure the same way
a real query would:

```python
class MeasureCheckIn(BaseModel):
    expr: str = ""
    frame: Optional[str] = None
    frame_emits: list[str] = []
    columns: list[str] = []
    measure_names: list[str] = []
    parameters: list[dict] = []   # NEW: the visual's currently-declared parameters
```

`check_measure` resolves each declared parameter to its `default` (there
is no "current selection" concept while still drafting — this endpoint
answers "would this compile", not "what would it currently show") and
passes that as `parameter_values` into `compile_measure`, exactly as
`run_query` does. A `param('name')` referencing a name absent from
`parameters` fails with the same `unknown_parameter` the real query path
would give, surfaced live as the developer types (FR-006, checked at
author time not just query time).

## Unchanged, but newly load-bearing: `POST/PUT /api/visuals[/{id}]`

No shape change — `spec` is already an opaque JSON blob. Convention:
`spec.query.parameters: [{name, values, default}]` lives alongside
`spec.query.inline_measures`, saved and loaded as part of the same object
(mirrors how inline measures are already visual-scoped with no dedicated
endpoint of their own).

**New validation on save** (mirrors `_validate_measure_body`'s posture —
reject clearly, at save time, not silently at first-query time):
- Duplicate parameter names within one visual → 400.
- A declared `default` not in that parameter's `values` → 400.
- Any `inline_measures` entry whose `expr` references (via
  `measure_dsl.referenced_parameter_names`) a parameter name not present
  in this same visual's `spec.query.parameters` → 400 (FR-006 enforced at
  visual-save time too, not just query time).

## Changed: `POST/PUT /api/models/{name}/measures` (`app/api/models.py`)

`_validate_measure_body()` gains one check, first, before anything else:

```python
if measure_dsl.referenced_parameter_names(m.expr):
    raise HTTPException(
        status_code=400,
        detail=f"measure '{m.name}': references a parameter — parameterized "
               "measures can only be saved to a visual, not to the shared model",
    )
```

Enforces FR-007 server-side (authoritative); the Measure Lab's "save to
model" button is also disabled client-side under the same condition as a
UX convenience, not as the actual guard.

## Changed: `POST/PUT /api/dashboards[/{id}]` (`app/api/dashboards.py`)

`views[i]` gains an optional `parameters: {name: int}` map alongside the
existing `filters` list — no request-model shape change needed beyond
widening the existing free-form view dict the same way `filters` already
is (see `data-model.md`).

**New validation on save** (FR-015/FR-016, authoritative — the dashboard
UI performs the same check client-side at tile-add time for immediate
feedback, but this is what actually blocks a bad state from persisting):

For every pair of tiles in `items` whose visuals declare a parameter with
the same `name`: their declarations (`values` as a set + `default`) must
be identical (FR-014), or the save is rejected with a 400 naming the
parameter and both visuals' names/ids. This walks `items` → looks up each
`visual_id`'s saved `spec.query.parameters` → groups by name, exactly as
`data-model.md`'s "Shared Parameter" derivation describes.

## Frontend state additions (no new endpoints, existing modules)

| Module | Addition |
|---|---|
| `state.js` | `state.parameters` (declarations, mirrors `state.inlineMeasures`), `state.parameterValues` (current picks, visual-builder-local) |
| `builder.js` | `buildQuery()` includes `parameters`/`parameter_values`; `currentSpec()`/`loadVisual()` round-trip `spec.query.parameters` the same way they already round-trip `inline_measures` |
| `measurelab.js` | `param('name')` insertable from a picker listing `state.parameters`; `saveToModel()` disabled (with an explanatory tooltip) when the draft expr's `referenced_parameter_names` (computed client-side by the same regex/AST-lite the completion helper already uses, or via a call to `/api/measures/check`) is non-empty |
| `dashboard.js` | `dashParamUnion()` (parallel to `dashDimUnion()`), `renderDashParams()` (parallel to `renderDashFilters()`), `tileQuery()` extended to merge in the resolved parameter values for that tile's visual, tile-add handler runs the conflict check before calling the add API |
