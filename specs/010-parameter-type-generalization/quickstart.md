# Quickstart: Validating Generalized Visual Parameters

## Prerequisites

Same as `specs/009-visual-parameters/quickstart.md` — app runnable locally
per `README.md`, no new environment variables or services.

## 1. DSL: `param()` in every legal position, across types

```bash
pytest tests/test_measure_dsl.py -k param -v
```

Expected, per `contracts/compile_measure_param_types.md`:
- `revenue > param('threshold')` (float-typed) compiles and, with
  `parameter_values={"threshold": 100.0}`, matches the same result as the
  literal `revenue > 100.0` would.
- `if_(status == param('target_status'), revenue, 0)` (string-typed)
  compiles and matches `if_(status == "shipped", revenue, 0)` for
  `parameter_values={"target_status": "shipped"}`.
- `coalesce(revenue, param('fallback'))`, `where(revenue,
  revenue > param('threshold'))` both compile with a `param()` argument.
- `cast(param('x'), "int")` — `param()` as `cast()`'s **value** argument
  compiles; `cast(revenue, param('x'))` — `param()` as `cast()`'s
  **type-name** argument — still raises `kind="disallowed"` (this is the
  pre-existing `_string_literal_arg` guard, not new behavior).
- `lag(revenue, param('float_param'))` where `float_param` resolves to
  `2.0` (a genuine Python `float`) raises `kind="disallowed"`, even though
  `2.0 == 2`. The identical measure with an `int`-typed parameter
  resolving to `2` still works exactly as under spec 009.

## 2. `engine.resolve_parameter_values`: type validation and coercion

```bash
pytest tests/test_engine.py -k "parameter and type" -v
```

Expected:
- A declared `float` parameter with `values: [1, 2.5, 3]` (mixed
  JSON-integer/float shapes) validates successfully; the resolved value
  for a JSON-integer-shaped selection (e.g. `parameter_values:
  {"x": 1}`) is a genuine Python `float` (`1.0`) by the time it's handed
  to `compile_measure`.
- A declared `int` parameter with a `values` entry of `2.5` (a JSON
  float) is rejected at validation, not silently truncated.
- A declared `string` parameter with `values: ["a", "b"]`, default
  `"a"`, round-trips correctly; a numeric default against a string-typed
  `values` list is rejected.
- An absent `type` field behaves identically to an explicit
  `"type": "int"` in every one of the above.

## 3. API: visual save and dashboard sharing/conflict, type-aware

```bash
pytest tests/test_api.py -k "parameter and type" -v
```

Manual confirmation — a string parameter driving an equality comparison,
end to end:

```bash
curl -s -X POST http://localhost:8000/api/query -H 'Content-Type: application/json' -d '{
  "model": "sales", "dimensions": [], "measures": ["flagged_revenue"],
  "inline_measures": [{"name": "flagged_revenue",
    "expr": "if_(category == param(\"target_category\"), revenue, 0)"}],
  "parameters": [{"name": "target_category", "type": "string",
    "values": ["electronics", "apparel", "grocery"], "default": "electronics"}],
  "parameter_values": {"target_category": "apparel"}
}'
# expect: 200, flagged_revenue computed against category == "apparel"
```

Dashboard type-conflict:

```bash
# two visuals each declaring "threshold" — one int, one string — added to
# one dashboard: expect 400 naming "threshold" as conflicting, mentioning
# both visuals, exactly as a values/default mismatch already does under
# spec 009 — see tests/test_api.py's existing conflict tests for the
# request shape to mirror with a type mismatch instead
```

## 4. Backward compatibility (Constitution Principle IV — required)

1. Using a visual saved **before** this feature shipped (a spec-009-era
   parameter with no `type` field, referenced only inside `lag()`): open
   it in the builder. Confirm it loads, displays as an (implicitly) `int`
   parameter, its toggle control still works, and its measure still
   resolves — with zero code changes to the saved data required.
2. Run the full pre-existing automated suite (`pytest -v`) and confirm
   every spec-009 test (measure_dsl, engine, api, static) still passes
   unmodified.

## 5. Browser-verified, end to end (required, not optional)

1. **Float parameter in a comparison**: declare a `float`-typed parameter
   (e.g. `threshold`, values `10/25/50/100`, default `25`) on a visual,
   author `if_(revenue > param('threshold'), revenue, 0)` in the Measure
   Lab, confirm it resolves and saves, confirm "save to model" stays
   blocked. Toggle the control to a different value and confirm the chart
   updates. Zero console errors.
2. **String parameter in an equality/coalesce**: declare a `string`-typed
   parameter with a handful of category-label values, author a measure
   using `param('name')` in a comparison or inside `coalesce()`, confirm
   the toggle control shows the declared text values (not attempting
   numeric parsing) and toggling re-runs correctly.
3. **Type selector UX**: in the parameter declaration editor, switch a
   parameter's type after entering values, and confirm the values/default
   are cleared (not silently kept as now-mismatched-type data).
4. **Dashboard type conflict**: two visuals with a same-named parameter,
   one `int` one `string` (same-looking string/int values, e.g. `[1,2,3]`
   vs `["1","2","3"]`); confirm they're blocked from coexisting on one
   dashboard with a message naming the conflict, exactly like spec 009's
   values/default conflict already behaves.
5. **Legacy visual still works**: open a visual/dashboard saved under
   spec 009 (untyped, int, `lag()`-only) and confirm it behaves
   identically to before this feature shipped.

## 6. Regression check

```bash
pytest -v
```

Expected: full suite, including every existing spec-009 parameter test,
passes unchanged — this feature is additive only.
