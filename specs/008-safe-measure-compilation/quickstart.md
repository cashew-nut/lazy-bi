# Quickstart: Validating Safe Measure Compilation

## Prerequisites

- `pip install -r requirements.txt -r requirements-dev.txt`
- App runnable locally per `run.sh` / existing `README.md` instructions (embedded moto S3 emulator, local sqlite).
- Set `CI_API_KEY=devsecret` in the environment before starting the app, to exercise the auth-gated routes (unset = every mutation 401s, which is also worth confirming once).

## 1. Compiler correctness + red-team suite (pure, no server needed)

```bash
pytest tests/test_measure_dsl.py -v
```

Expected: every correctness case (plain aggregate, ratio, filtered aggregate via `where`, `if_`, `coalesce`, `cast`, `count_distinct`) matches a hand-computed or directly-constructed-`pl.Expr` expected value; every red-team payload raises `MeasureCompileError` and is asserted to never execute (e.g. via a sentinel side-effect that must never fire).

## 2. Full regression pass against rewritten model YAML

```bash
pytest tests/test_engine.py tests/test_semantic.py tests/test_api.py -v
```

Expected: all existing measure-dependent tests still pass against the rewritten `models/*.yaml` DSL syntax — in particular the taxi-benchmark-style aggregate/ratio measures in `models/taxi.yaml`, `models/sales.yaml`, etc. The three tests that previously exercised **inline** frame execution (`test_inline_framed_measure`, `test_frame_that_drops_dimensions_rejected`, `test_emitted_dimension_missing_from_frame_rejected`) are now rewritten to assert rejection (`QueryError`) instead of success.

## 3. Framed-measure carve-out, end to end

```bash
pytest tests/test_engine.py -k framed -v
```

Expected: `models/clinical_ops_recruitment.yaml`'s `months_to_75` measure still computes correct values when queried normally (model-measure path, unauthenticated read). Separately, confirm via a manual `curl` that the identical `frame`/`frame_emits` construct submitted as an *inline* measure on `/query` is rejected:

```bash
curl -s -X POST http://localhost:8000/query -H 'Content-Type: application/json' -d '{
  "model": "clinical_ops_recruitment", "dimensions": [], "measures": ["probe"],
  "inline_measures": [{"name": "probe", "expr": "median(x)", "frame": "frame = lf"}]
}'
# expect: 400, message naming that frame-based measures require an authenticated model-measure save
```

**One-time operational step** (per your own running instance — the shipped `models/clinical_ops_recruitment.yaml` predates this feature, so its `median_months_to_75pct_randomised` measure has no provenance history yet; this establishes its first record without changing its behavior). Fetch the measure's current `frame`/`frame_emits`/`expr` from the yaml or `GET /models/clinical_ops_recruitment/yaml`, then re-PUT it through the authenticated endpoint:

```bash
curl -s -X PUT http://localhost:8000/models/clinical_ops_recruitment/measures/median_months_to_75pct_randomised \
  -H 'Content-Type: application/json' -H 'X-API-Key: devsecret' -H 'X-Author: <your name>' \
  -d @- <<'EOF'
{
  "name": "median_months_to_75pct_randomised",
  "label": "Median Months to 75% Randomised",
  "description": "Median across studies of the months between a study's first actual randomisation and the month its cumulative randomisations reached 75% of its total.",
  "expr": "pl.col(\"months_to_75\").median()",
  "frame": "keys = list(dict.fromkeys([\"study_id\", *dims]))\nmonthly = (\n    lf.filter(\n        (pl.col(\"event_type\") == \"randomised\")\n        & (pl.col(\"scenario\") == \"actual\")\n        & (pl.col(\"event_count\") > 0)\n    )\n    .group_by(list(dict.fromkeys([*keys, \"event_month\"])))\n    .agg(pl.col(\"event_count\").sum())\n    .sort(\"event_month\")\n)\nframe = (\n    monthly.with_columns(\n        (pl.col(\"event_count\").cum_sum().over(keys)\n         / pl.col(\"event_count\").sum().over(keys)).alias(\"cume_share\"),\n        pl.col(\"event_month\").min().over(keys).alias(\"first_month\"),\n    )\n    .filter(pl.col(\"cume_share\") >= 0.75)\n    .group_by(keys)\n    .agg(\n        pl.col(\"first_month\").first(),\n        pl.col(\"event_month\").min().alias(\"month_75\"),\n    )\n    .with_columns(\n        ((pl.col(\"month_75\") - pl.col(\"first_month\")).dt.total_days() / 30.44)\n        .alias(\"months_to_75\"),\n        pl.col(\"month_75\").alias(\"event_date\"),\n    )\n)",
  "frame_emits": ["event_date"]
}
EOF
# expect: 200; GET .../median_months_to_75pct_randomised/history now shows one row, version 1
# confirm identical results before/after: query the measure both ways and diff the numbers.
```

## 4. Auth-gated model-measure authoring

```bash
# no credentials -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/models/taxi/measures \
  -H 'Content-Type: application/json' -d '{"name": "probe1", "expr": "sum(fare_amount)"}'
# expect: 401

# with credentials -> 201, provenance recorded
curl -s -X POST http://localhost:8000/models/taxi/measures \
  -H 'Content-Type: application/json' -H 'X-API-Key: devsecret' -H 'X-Author: alice' \
  -d '{"name": "probe1", "expr": "sum(fare_amount)"}'
curl -s -H 'X-API-Key: devsecret' http://localhost:8000/models/taxi/measures/probe1/history
# expect: one row, version 1, author "alice"

# invalid expression -> refused, nothing persisted
curl -s -X POST http://localhost:8000/models/taxi/measures \
  -H 'Content-Type: application/json' -H 'X-API-Key: devsecret' -H 'X-Author: alice' \
  -d '{"name": "probe2", "expr": "__import__(\"os\").system(\"id\")"}'
# expect: 400, and probe2 absent from models/taxi.yaml and from the history endpoint
```

## 5. Browser verification (Constitution IV, scoped per plan.md)

Since this feature adds no new UI: open Studio, pick the `taxi` model, build a visual using a rewritten measure (e.g. `avg_fare`), confirm the value matches what it showed before this feature (spot-check against a pre-change screenshot or the correctness suite's expected value) — this is the "no regression for representable measures" check (SC-003) done visually rather than only via pytest. Zero console errors expected, since no frontend code changes.

## 6. Static check for the acceptance-criterion "no eval/exec/compile on measure input" (SC-006)

```bash
grep -rn "eval(\|exec(\|compile(" app/measure_dsl.py
# expect: no matches (the module must not contain these calls at all)
grep -rn "eval(\|exec(\|compile(" app/semantic.py
# expect: matches only inside compile_expr/compile_frame/validate_frame,
# reachable only from the authenticated frame carve-out — confirm no other
# caller in app/engine.py or app/api/*.py reaches these for inline/query-time input
```
