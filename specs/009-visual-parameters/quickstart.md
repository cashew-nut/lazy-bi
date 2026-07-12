# Quickstart: Validating Visual Parameters

## Prerequisites

- App runnable locally per `README.md` (embedded moto S3 emulator, local
  SQLite) — no new environment variables or services.
- A model with at least one plain aggregate measure and one time
  dimension, to build a `lag()` measure over (e.g. `taxi.yaml`'s
  `revenue` + a monthly time dimension — same setup existing window-
  measure tests already use).

## 1. DSL correctness + scope enforcement (pure, no server needed)

```bash
pytest tests/test_measure_dsl.py -k param -v
```

Expected, matching `contracts/compile_measure_param.md`'s table exactly:
- `lag(revenue, param('period_list'))` with `parameter_values={"period_list": 2}` compiles to the same `pl.Expr` as `lag(revenue, 2)` would.
- `param('period_list')` with `period_list` absent from `parameter_values` raises `MeasureCompileError(kind="unknown_parameter")`.
- `param(...)` used anywhere other than `lag()`'s second argument (e.g. `param('x') > 0`, `running_total(param('x'))`, `if_(param('x') > 1, revenue, 0)`) raises `MeasureCompileError(kind="unknown_function")`.
- `param(1)` (non-string arg) and `param('a', 'b')` (wrong arity) both raise `kind="disallowed"`.
- A resolved `param()` value `< 1` is rejected with the same message as a literal `lag(x, 0)` today.

## 2. Query-time validation (engine + API)

```bash
pytest tests/test_engine.py -k parameter -v
pytest tests/test_api.py -k parameter -v
```

Expected:
- A query declaring `period_list = [1,2,3,4]` default `1`, with no `parameter_values`, computes `lag(revenue, 1)`.
- The same query with `parameter_values: {"period_list": 3}` computes `lag(revenue, 3)`.
- `parameter_values: {"period_list": 99}` (not in the declared list) → 400, and — check this explicitly — no query is executed against the scan (assert via the same "sentinel never fires" technique `test_measure_dsl.py`'s red-team suite already uses, or by asserting the mock/spy scan is never called).
- `parameter_values: {"nope": 1}` (undeclared name) → 400.

Manual confirmation of the same, end to end:

```bash
curl -s -X POST http://localhost:8000/api/query -H 'Content-Type: application/json' -d '{
  "model": "taxi", "dimensions": [{"name": "pickup_month"}], "measures": ["lag_fare"],
  "inline_measures": [{"name": "lag_fare", "expr": "lag(fare_total, param(\'period_list\'))"}],
  "parameters": [{"name": "period_list", "values": [1,2,3,4], "default": 1}],
  "parameter_values": {"period_list": 2}
}'
# expect: 200, values shifted by 2 periods
```

## 3. Model-measure promotion is blocked

```bash
pytest tests/test_api.py -k "param and model" -v
```

Manual confirmation:

```bash
curl -s -X POST http://localhost:8000/api/models/taxi/measures \
  -H 'Content-Type: application/json' -H 'X-API-Key: devsecret' -H 'X-Author: me' -d '{
  "name": "lag_fare", "expr": "lag(fare_total, param(\x27period_list\x27))"
}'
# expect: 400, "parameterized measures can only be saved to a visual"
```

## 4. Browser-verified, end to end (Constitution Principle IV — required, not optional)

1. **Single visual**: In the builder, open the Measure Lab, declare a
   parameter `period_list = [1,2,3,4]` default `1`, write
   `lag(revenue, param('period_list'))`, save to the visual. Confirm the
   visual renders using a 1-period lag by default, a toggle control
   appears, and picking `3` re-runs the query and changes the displayed
   values. Confirm "save to model" is disabled/rejected for this
   measure. **Refresh the page** and confirm the parameter declaration
   and measure both survive (persistence round-trip, Principle IV).

2. **Dashboard, shared parameter**: Save two visuals that each declare an
   identically-defined `period_list` parameter (same values, same
   default) and each use it in a measure. Add both to one dashboard.
   Confirm only **one** control appears, and changing it updates both
   tiles. Save the dashboard as a named view with a non-default
   selection, reload the page, switch to that view, and confirm the
   saved value is restored to both tiles (not just one).

3. **Dashboard, conflict**: Edit one of the two visuals' parameter to a
   different value list (e.g. `[1,2,3]` instead of `[1,2,3,4]`), keeping
   the same name. Try to add both to a fresh dashboard. Confirm the
   add is blocked with a clear message naming `period_list` and both
   visuals, and that no bad dashboard state gets saved. Rename one
   visual's parameter so the names differ, and confirm both now add
   successfully with independent controls.

4. **Zero console errors** throughout steps 1-3, per Principle IV.

## 5. Regression check

```bash
pytest -v
```

Expected: full existing suite (including `test_measure_dsl.py`'s existing
non-parameter `lag()`/`running_total()` cases, and every dashboard/view
test) still passes unchanged — this feature is additive only.
