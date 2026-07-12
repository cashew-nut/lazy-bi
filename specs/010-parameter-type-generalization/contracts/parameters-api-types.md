# API Contract Changes: Parameter Types

Delta to `specs/009-visual-parameters/contracts/parameters-api.md`. Every
route listed there keeps its existing shape — `parameters: list[dict]`
was always untyped JSON, so `type` rides along as one more optional key
per entry with no pydantic model change anywhere. What changes is the
*validation* each route performs on those dicts.

## Unchanged (no shape or validation-point change)

- `POST /api/query` (`QueryRequest.parameters`/`.parameter_values`) — the
  route itself is unchanged; the validation it delegates to
  (`engine.resolve_parameter_values`) becomes type-aware, per
  `data-model.md`.
- `POST /api/measures/check` (`MeasureCheckIn.parameters`) — unchanged;
  already called `resolve_parameter_values` unconditionally (not gated on
  window mode), so it already exercises the new type-aware path with no
  code change of its own (research.md §9).
- `POST/PUT /api/models/{name}/measures` — unchanged; the
  `referenced_parameter_names` guard that blocks any `param()`-referencing
  measure from being saved as a model measure is structural (an AST scan
  for `param(...)` call sites anywhere in the expression) and was never
  position- or type-specific.

## Changed: `POST/PUT /api/visuals[/{id}]` (`app/api/visuals.py`)

`_validate_visual_spec`'s parameter-declaration checks become type-aware,
reusing `engine.PARAM_TYPES`/`param_type_ok`/`coerce_param_value`:

- A `type` field, if present, must be one of `"int"`/`"float"`/`"string"`
  — else 400.
- Every entry in `values` must satisfy `param_type_ok(entry, type)` (type
  defaults to `"int"` if the field is absent) — else 400, naming the
  offending value.
- `default` must satisfy `param_type_ok` for the same type **and** its
  coerced value must be a member of the coerced `values` set — else 400.

Unchanged from spec 009: duplicate-name rejection, and the
`referenced_parameter_names`-based check that every `inline_measures`
entry's `param()` references name a parameter actually declared on this
visual (that check is name-based, not type-based, and needed no change).

## Changed: `POST/PUT /api/dashboards[/{id}]` (`app/api/dashboards.py`)

`_same_param_def` (the FR-012/FR-014 identity check backing the
conflict-rejection in `_check_param_conflicts`) gains a `type` comparison:

```python
def _same_param_def(a: dict, b: dict) -> bool:
    a_type, b_type = a.get("type") or "int", b.get("type") or "int"
    if a_type != b_type:
        return False
    a_vals = {engine.coerce_param_value(v, a_type) for v in (a.get("values") or [])}
    b_vals = {engine.coerce_param_value(v, b_type) for v in (b.get("values") or [])}
    return a_vals == b_vals and (
        engine.coerce_param_value(a.get("default"), a_type)
        == engine.coerce_param_value(b.get("default"), b_type)
    )
```

`_check_param_conflicts`'s walking logic (group same-named parameters
across a dashboard's tiles, reject on the first non-identical pair) is
unchanged — it already delegates the actual "are these the same" question
to `_same_param_def`.

## Frontend contract additions (`app/static/js/*`, no new endpoints)

| Module | Addition |
|---|---|
| `builder.js` | `renderParameters()`: a `type` `<select>` (`int`/`float`/`string`) per declared parameter row; `values`-input parsing dispatches on the row's type (`parseInt`/`parseFloat`/trimmed-string-split-and-dedupe); changing a row's type clears its `values`/`default` rather than reinterpreting them; `addParameter()` defaults a new row to `type: "int"` (unchanged default behavior for anyone not touching the new selector) |
| `measurelab.js` | The `param('name')` completion hint (`dslItems`'s `kind: "param"` branch consumer) includes the parameter's type: `` `${type} · values: ... (default ...)` `` |
| `dashboard.js` | `sameParamDef(a, b)`: adds the same type-first comparison as the Python `_same_param_def`, with type-aware sort (numeric comparator for `int`/`float`, default/lexicographic for `string`) before the values-set comparison |
