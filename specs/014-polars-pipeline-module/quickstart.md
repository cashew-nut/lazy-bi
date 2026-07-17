# Quickstart: Polars Pipeline Module — validation guide

Proves the feature end-to-end per Constitution IV (browser-verified, golden
path + persistence round-trip + zero console errors). Formats and routes:
see [contracts/](./contracts/).

## Prerequisites

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
./run.sh          # demo mode; note the bootstrap admin password in the log
```

Sign in as `admin`. Demo pipelines (`pipelines/*.yaml`, bronze→silver→gold
over seeded sales data) and `pipelines/layers.yaml` load at startup.

## Automated checks

```bash
.venv/bin/python -m pytest tests/                       # full suite
.venv/bin/python -m pytest tests/test_pipelines.py tests/test_pipeline_api.py tests/test_role_matrix.py
```

Must cover: YAML parse/cross-rule validation, the materialization matrix
(replace delta/parquet; upsert × {ignore, sync, soft_delete, predicate} ×
{update, insert, missing-key} against the moto bucket — SC-002), guard
failures (null/dup keys, schema diff, empty-sync halt), run lifecycle incl.
interrupted sweep, lineage validation + model-YAML section surgery
(byte-preservation outside the section), API + role matrix.

## Golden path (browser)

1. **MODELLING** → pipelines rail section lists the demo pipelines with
   latest-run status. Open one: YAML editor with live validation.
2. Create a pipeline (**+ PIPELINE**): script joins two seeded datasets,
   target `s3://cash-intel/silver/qs_demo` (delta), mode `replace`, two
   lineage entries (accept one pass-through suggestion). Save.
3. **RUN** → status chips `queued → running → succeeded`; run history row
   shows duration + rows written.
4. Data overview shows the new target object mapped; build a model over it
   (dataset picker) and query it in **STUDIO** — rows come back.
5. The new model's YAML now contains the `pipeline_lineage:` section with
   both entries; hand-authored YAML elsewhere untouched.
6. Switch the pipeline to `upsert` (keys + `soft_delete` +flag column). Run
   with a script variant that drops one key: row remains, flag set. Re-add
   the key, run: flag cleared.
7. Break lineage (remove a declared field from the script's output), run:
   run **succeeds** with a lineage warning naming the field; model section
   marks the entry `stale`.
8. **Lineage graph**: nodes in bronze/silver/gold columns, edges carry run
   status; expand a gold field → upstream highlight across both hops with
   transform descriptions. Force a failing run → edge shows failure.

## Failure / safety checks

- Failing script (raise mid-frame): run `failed` with the traceback; target
  still serves its pre-run content in STUDIO (SC-003).
- Timeout: `timeout_seconds: 1` + a sleeping script → `timed_out`, process
  gone (`ps`), target intact.
- Restart mid-run: kill the app during `running` → after restart the run
  reads `interrupted`, never stuck.
- Upsert guards: duplicate/null keys or a schema-diff output → `failed`
  before any target change; `sync` + empty output halts unless
  `allow_empty_sync: true`.
- Roles: as an **author** account, every pipeline mutation and RUN control
  is absent/refused (403 via curl); list/runs/graph readable. As **viewer**,
  same read-only story (SC-006). Audit log shows every mutation + trigger.

## Persistence round-trip + console

- Cold-restart the app: pipelines, layer assignments, run history, and the
  model lineage section all survive; the graph re-renders identically.
- Graph selection/field expansion and run-panel polling reset on reload
  (deliberately ephemeral — Constitution V).
- Full pass through the above with the browser console open: zero errors.
