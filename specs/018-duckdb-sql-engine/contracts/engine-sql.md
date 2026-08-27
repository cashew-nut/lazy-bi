# Contract: the shape of a generated query

One semantic query compiles to **one** DuckDB statement. This document is the
reference for what that statement looks like, layer by layer, so the engine's
output stays inspectable rather than emergent.

Every identifier the engine emits is quoted; every literal is bound as a
prepared-statement parameter (`?`) rather than interpolated. Only authored
fragments that passed the validator are ever concatenated as text.

## 1. Source relations

| `format` | Relation |
|---|---|
| `parquet` | `read_parquet([<resolved file list>])`, or `read_parquet('<glob>')` when no listing is available |
| `csv` | `read_csv('<path>')` |
| `delta` | `delta_scan('<table root>')` |
| `iceberg` | `iceberg_scan('<resolved metadata.json>')` — the catalog-free convention `app/iceberg_util.py` already implements, which also avoids `unsafe_enable_version_guessing` |

A source small enough to pin (FR-032) is replaced by its local table name.

## 2. The fact scan

```sql
WITH __fact AS (
  SELECT * FROM <base source>
    LEFT JOIN <join source>   ON ...        -- model joins:
    LEFT JOIN <bundle>        ON ...        -- how: left  imports, only when read
    JOIN      <bundle>        ON ...        -- how: inner imports, always
)
```

`how: between` imports are not a plain join — see §5.

## 3. Grouping and aggregation

```sql
SELECT
  date_trunc('month', "order_date")   AS "order_date",   -- a grained time dimension
  "region"                            AS "region",
  AVG("region_lat") AS "__lat_region",                   -- geo hidden columns
  AVG("region_lon") AS "__lon_region",
  SUM(unit_price * quantity)          AS "revenue"       -- authored, validated
FROM __fact
WHERE "region" IN (?, ?) AND "order_date" >= ?
GROUP BY ALL
```

`GROUP BY ALL` is used deliberately: it keeps the emitted SQL readable and
makes the grouping list impossible to desynchronize from the select list.

## 4. Window measures

Applied in an outer select over the aggregated relation, so they read
one-row-per-group values:

```sql
SELECT *, SUM("revenue") OVER w AS "revenue_running_total"
FROM __agg
WINDOW w AS (PARTITION BY "channel" ORDER BY "order_date")
```

## 5. Point-in-time

### Spine dimensions

```sql
WITH __spine AS (
  SELECT CAST(b AS DATE) AS "active_at",
         CAST(b AS DATE) AS __period_from,
         CAST(b + INTERVAL 1 MONTH - INTERVAL 1 DAY AS DATE) AS __period_to
  FROM range(?, ?, INTERVAL 1 MONTH) t(b)
)
SELECT ... FROM __spine JOIN __fact
  ON <the match: predicate>
```

### `how: between` calendar imports

The date table is thinned to one row per bucket *at the query's grain* before
the join, carrying that bucket's real span:

```sql
WITH __cal AS (
  SELECT * FROM (
    SELECT *,
           MIN("date") OVER (PARTITION BY date_trunc('month', "date")) AS __period_from,
           MAX("date") OVER (PARTITION BY date_trunc('month', "date")) AS __period_to
    FROM <calendar>
  ) WHERE "date" = __period_from
)
```

### The `match:` predicate — one definition, both mechanisms

| `match` | Predicate |
|---|---|
| `overlap` (default) | `start <= __period_to AND end >= __period_from` |
| `period_start` | `start <= __period_from AND end >= __period_from` |
| `period_end` | `start <= __period_to AND end >= __period_to` |

A null interval end is coalesced to `DATE '9999-01-01'` first, as today.

## 6. Complex (`from:`) measures

Each becomes a CTE and is joined back on the dimensions:

```sql
WITH __m_median_tenure_days AS (
  SELECT "plan", date_trunc('month', "churn_month") AS "churn_month",
         MEDIAN(tenure_days) AS "median_tenure_days"
  FROM ( <the authored from: block, placeholders substituted> )
  GROUP BY ALL
)
SELECT ...
FROM __agg
FULL OUTER JOIN __m_median_tenure_days
  ON __agg."plan" IS NOT DISTINCT FROM __m_median_tenure_days."plan"
 AND ...
```

`IS NOT DISTINCT FROM` rather than `=`: a null dimension value is a real
group, and today's polars merge uses `nulls_equal=True`.

## 7. Several fact tables in one model

The part that stops being N queries. Each fact table is a CTE; the parts are
merged on the dimensions they share:

```sql
WITH __p_orders AS ( ... ), __p_spend AS ( ... ), __p_subs AS ( ... )
SELECT
  COALESCE(__p_orders."calendar_date", __p_spend."calendar_date") AS "calendar_date",
  __p_orders."revenue", __p_spend."ad_spend"
FROM __p_orders
FULL OUTER JOIN __p_spend
  ON __p_orders."calendar_date" IS NOT DISTINCT FROM __p_spend."calendar_date"
ORDER BY 1
LIMIT ?
```

Rules preserved exactly: only the parts a query names a measure from are read
at all; only dimensions every read part offers are groupable; a bucket only
one part has rows for keeps its row and leaves the others null; sort and
limit apply after the merge.

## 8. Ordering and limits

Unchanged: an explicit sort if it names a valid key, else time ascending if a
time dimension is present, else the first measure descending. The limit is
`min(query limit, MAX_ROWS)` and is applied after the merge.

## 9. What the engine does *not* do

- It never interpolates a user-supplied literal into SQL text.
- It never issues more than one statement per query.
- It never materializes a fact table to answer a query (Principle II).
