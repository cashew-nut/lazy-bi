# Quickstart: Validating Common Dimensional Models

Concrete worked example used to prove the feature end-to-end. Uses real
values already in `app/seed.py` (`REGIONS = ["Neo-Tokyo", "Night City",
"Euro-Zone", "Pacifica", "Badlands"]`, `REGION_COORDS`) so the demo is
grounded in this repo's actual demo data, not placeholders.

**Why this example**: `sales.yaml` and `logistics.yaml` both declare a bare
`region` dimension today, with no map coordinates. `marketing.yaml` has
region-level coordinates, but only because they're baked directly into its
own parquet rows — undeclared and unreusable by the other two. This is
exactly the duplication-in-waiting the feature targets: one shared
`geography` bundle, imported by both `sales` and `logistics`, proves reuse
(spec SC-001) and demonstrates the multi-dataset/internal-join mechanism
(spec FR-003, FR-006) — while `marketing.yaml` is deliberately left
untouched, as proof the existing inline-join mechanism keeps working
unchanged (spec Assumptions: additive, not a replacement).

## Prerequisites

- Working tree on `feature/common-dimensions` with the implementation tasks
  from `tasks.md` complete (this guide validates the finished feature, it
  doesn't build it).
- Local dev server running: `./run.sh` (or `docker compose up`).

## Setup

1. Two new reference files, seeded alongside the existing demo data (extend
   `app/seed.py`'s existing `REGIONS`/`REGION_COORDS`, do not replace them —
   `marketing`'s seeding is unaffected):
   - `s3://cash-intel/ref/regions.csv` — columns `region, region_lat,
     region_lon, territory`, one row per existing `REGIONS` value, each
     assigned a `territory` grouping (e.g. `Night City` + `Badlands` →
     `west`, `Neo-Tokyo` + `Pacifica` → `pacific-rim`, `Euro-Zone` →
     `emea`).
   - `s3://cash-intel/ref/territories.csv` — columns `territory, name`
     (e.g. `west, West Coast`).

2. `dimensions/geography.yaml`:

   ```yaml
   name: geography
   label: Geography
   description: >
     Shared region/territory reference data - any fact model with its own
     region-shaped column can import this to gain a validated region
     dimension (with map coordinates) and its territory rollup.

   datasets:
     - name: regions
       source: { format: csv, path: s3://cash-intel/ref/regions.csv }
       dimensions:
         - name: region
           label: Region
           geo: { lat: region_lat, lon: region_lon }
         - name: territory
           label: Territory Code
       joins:
         - to: territories
           on: territory

     - name: territories
       source: { format: csv, path: s3://cash-intel/ref/territories.csv }
       dimensions:
         - name: territory_name
           column: name
           label: Territory
   ```

3. In `models/sales.yaml` and `models/logistics.yaml`: remove the existing
   native `- name: region / label: Region` dimension entry, and add:

   ```yaml
   dimension_imports:
     - bundle: geography
       anchor_dataset: regions
       on: region
   ```

   (`models/marketing.yaml` is **not** touched — see "Why this example"
   above.)

## Validate

### 1. Load-time correctness (pytest)

```bash
.venv/bin/python -m pytest tests/ -k "dimension or explorer" -v
```

Expect the new tests from `tasks.md` to pass, including: bundle parsing,
cycle/collision rejection, subset-import correctness, and the explorer
attribution fix.

### 2. API-level correctness

```bash
curl -s -X POST localhost:8080/api/models/reload | python3 -m json.tool
curl -s localhost:8080/api/models | python3 -c "
import json, sys
models = json.load(sys.stdin)
sales = next(m for m in models if m['name'] == 'sales')
print('imports:', sales['imports'])
dims = {d['name'] for d in sales['dimensions']}
assert 'region' in dims and 'territory_name' in dims, dims
print('sales now exposes territory_name via import: OK')
"
```

`territory_name` must be present even though `sales.yaml` only declared an
anchor to the `regions` dataset — it arrives transitively via `regions`'
own join to `territories` inside the bundle (FR-006).

```bash
curl -s -X POST localhost:8080/api/query -H 'content-type: application/json' -d '{
  "model": "sales",
  "dimensions": ["territory_name"],
  "measures": ["revenue"]
}' | python3 -m json.tool
```

Expect revenue grouped by territory, computed by joining sales → geography
bundle's `regions` → `territories`, entirely lazily.

### 3. Data explorer attribution

```bash
curl -s localhost:8080/api/explorer | python3 -c "
import json, sys
data = json.load(sys.stdin)
hits = {f['key']: f['models'] for f in data['files'] if 'regions.csv' in f['key'] or 'territories.csv' in f['key']}
print(hits)
assert all(hits.values()), 'regions.csv / territories.csv must not be unmapped'
"
```

Both new ref files must show up attributed to **both** `sales` and
`logistics` (not `unmapped`) — this is the explorer fix from
`contracts/api-changes.md`.

### 4. Browser verification (per constitution Principle IV)

- Open the builder on `sales`: confirm `territory_name` and a geo-enabled
  `region` are selectable exactly like any native dimension.
- Select `region` as a dimension with `revenue` as a measure, choose the
  **geo** chart type: confirm bubbles render for `sales` — impossible
  before this feature, since `sales` had no coordinates of its own.
- Put a `sales` tile and a `logistics` tile (also geography-imported) plus
  the existing `marketing` tile (still its own inline geo) on one
  dashboard; cross-filter by clicking a region on any one of them and
  confirm all three filter together — proving imported and native
  same-named dimensions interoperate with the existing cross-filter
  mechanism ([003](../003-advanced-visuals-cross-filtering/spec.md)) with
  zero changes to that mechanism.
- Zero console errors throughout.
