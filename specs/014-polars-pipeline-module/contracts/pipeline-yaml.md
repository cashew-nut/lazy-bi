# Contract: Pipeline YAML, Layers YAML, and the Model Lineage Section

The file formats are the editable contract (Constitution I). Field-by-field
rules live in [data-model.md](../data-model.md); this contract fixes the
shapes and the script execution contract.

## Pipeline file — `pipelines/<name>.yaml`

```yaml
name: silver_orders                # required, = filename stem
label: Silver Orders               # optional
description: Clean + enrich raw order lines.   # optional
sources:                           # required, ≥1
  - name: raw_orders               # handle the script uses
    format: parquet                # parquet | csv | delta
    path: s3://cash-intel/sales/*.parquet
    layer: bronze                  # optional, must exist in layers.yaml
  - name: products
    format: csv
    path: s3://cash-intel/ref/products.csv
    layer: bronze
target:                            # required, exactly one
  path: s3://cash-intel/silver/orders   # delta table root (or object key for parquet)
  format: delta                    # delta (default) | parquet (replace-only)
  layer: silver                    # optional
materialization:
  mode: upsert                     # replace | upsert
  keys: [order_id]                 # upsert: required
  on_delete: soft_delete           # ignore (default) | sync | soft_delete | predicate
  soft_delete_column: is_deleted   # soft_delete: required
  # delete_predicate: "region = 'EU'"   # predicate: required (delta SQL predicate)
  # allow_empty_sync: true         # sync: opt-in for empty-output truncation
timeout_seconds: 600               # optional, default 600, max 3600
script: |                          # required — real Python, admin-trust (Principle VI)
  orders = sources["raw_orders"]
  products = sources["products"]
  output = (
      orders.join(products, on="product", how="left")
            .with_columns((pl.col("unit_price") * pl.col("quantity")).alias("net_revenue"))
  )
lineage:                           # optional, per target field
  - field: order_id
    from: [raw_orders.order_id]
    transform: pass-through
  - field: net_revenue
    from: [raw_orders.unit_price, raw_orders.quantity]
    transform: "unit_price × quantity per order line"
```

### Script execution contract

- Namespace provided: `sources` (dict: declared source name →
  `polars.LazyFrame`, scanned with the platform's storage options), `pl`
  (polars). Nothing else is sanctioned.
- The script MUST assign `output`: a `polars.LazyFrame` or
  `polars.DataFrame`. Missing/wrong-typed `output` fails the run.
- The platform performs all target writes; a script writing anywhere itself
  is out of contract (admin-trusted, not sandboxed — Principle VI).
- Load-time check: `compile(script, "exec")` syntax validation only; runtime
  errors surface on the run record with full detail.

## Layers file — `pipelines/layers.yaml` (optional)

```yaml
layers:          # ordered: leftmost graph column first
  - name: bronze
    label: Bronze
  - name: silver
  - name: gold
```

## Model lineage section — regenerated inside `models/<target-model>.yaml`

Written only by the platform after a successful run (see data-model.md for
shape and staleness/orphan marks). Guarantees:

- The section (and its banner comment) is the ONLY region the writer touches;
  byte-identical preservation of everything else in the file.
- Regeneration is idempotent: same validated lineage ⇒ same section text.
- A model with no `pipeline_lineage:` section gains one at the end of the
  file; an existing section is replaced in place.
- Deleting the owning pipeline marks the section `orphaned: true` on the
  next reload; it is never silently removed.
