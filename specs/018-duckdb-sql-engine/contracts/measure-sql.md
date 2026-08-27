# Contract: the SQL measure grammar

One grammar, one key. Every measure is:

```
SELECT <expr>  FROM <from: or the fact scan>  GROUP BY <the query's dimensions>
```

`expr:` is always a SQL aggregate. Whether a measure is "simple" or "complex"
is decided by one optional key — `from:` — and nothing about the aggregate
itself changes.

## 1. Plain measures

```yaml
measures:
  - name: revenue
    expr: SUM(unit_price * quantity)
  - name: orders
    expr: COUNT(DISTINCT order_id)
  - name: margin_pct
    format: percent
    expr: SUM((unit_price - unit_cost) * quantity) / SUM(unit_price * quantity)
  - name: eu_revenue
    expr: SUM(unit_price * quantity) FILTER (WHERE region = 'EU')
  - name: p95_fare
    expr: QUANTILE_CONT(fare_amount, 0.95)
```

Bare identifiers are source columns, post-join, exactly as before.

## 2. Window measures

A measure containing a window function reads *sibling measures'* already
aggregated values. The engine supplies one named window:

```sql
WINDOW w AS (PARTITION BY <the query's non-time dimensions>
             ORDER BY <the query's single time dimension>)
```

```yaml
  - name: revenue_running_total
    expr: SUM(revenue) OVER w
  - name: revenue_pct_change
    expr: (revenue - LAG(revenue) OVER w) / LAG(revenue) OVER w
  - name: revenue_rank
    expr: RANK() OVER (ORDER BY revenue DESC)      # an explicit window is also legal
```

Rules (unchanged from the DSL's window mode):

- bare names are measure names, not columns; aggregate functions are not
  available (there are no raw rows left to reduce);
- the query needs exactly one time dimension to order by;
- a referenced sibling is computed even if the query didn't ask for it, and
  dropped from the response if so;
- a window measure cannot depend on another window measure or on a `from:`
  measure.

## 3. Complex measures — `from:`

```yaml
  - name: median_tenure_days
    label: Median Tenure (Days)
    expr: MEDIAN(tenure_days)
    from: |
      SELECT {dims},
             date_diff('day', start_date, end_date) AS tenure_days,
             date_trunc('month', end_date)          AS churn_month
      FROM {model}
      WHERE end_date IS NOT NULL
    emits: [churn_month]
```

`expr:` is a SQL aggregate — same as every measure above. The only difference
is that it aggregates the `from:` relation rather than the fact scan.

### Placeholders

| Placeholder | Expands to |
|---|---|
| `{model}` | the fact scan with the query's filters applied and its dimension columns materialized under their semantic names |
| `{dims}` | the query's grouping columns, comma-separated |

`{dims}` **always expands to at least one column** (the engine carries a
constant grouping column when the query groups by nothing), so
`SELECT {dims}, x` and `GROUP BY {dims}, y` are safe unconditionally. That is
the one thing the placeholder does that plain substitution would not.

### `emits:`

A dimension the block computes itself. It is withheld from `{dims}` during
the step — so the intermediary partitions stay whole — and applied to the
block's output afterwards: the engine truncates it at the query's grain and
groups the derived rows by it. Identical semantics to `frame_emits`.

### A larger example

"Median days for a study to log 75% of its events" — the README's
demonstration of something the old DSL could not express:

```yaml
  - name: median_days_to_75pct
    expr: MEDIAN(days_to_75)
    from: |
      WITH ranked AS (
        SELECT {dims}, study_id, event_date,
               ROW_NUMBER() OVER (PARTITION BY study_id, {dims} ORDER BY event_date)
                 / COUNT(*) OVER (PARTITION BY study_id, {dims}) AS cume,
               MIN(event_date) OVER (PARTITION BY study_id, {dims}) AS first_event
        FROM {model}
      )
      SELECT {dims}, study_id,
             date_diff('day', MIN(first_event), MIN(event_date)) AS days_to_75,
             MIN(event_date) AS event_date
      FROM ranked
      WHERE cume >= 0.75
      GROUP BY {dims}, study_id
    emits: [event_date]
```

### Joining rules

- Groups the derived relation has no rows for come back null (a `FULL OUTER
  JOIN` on the dimensions, so an `emits:`ted dimension can surface groups the
  raw rows never form).
- Framed and plain measures mix freely in one query.
- A model with several unrelated fact tables scopes each measure to its own
  dataset, so `{model}` is always unambiguous.

## 4. `param('name')`

Unchanged in spelling and in rules. It is a call in the grammar, resolved at
compile time to a literal of the parameter's declared type, legal anywhere a
literal is legal.

```yaml
  - name: flagged_revenue
    expr: SUM(CASE WHEN unit_price > param('threshold') THEN unit_price * quantity ELSE 0 END)
  - name: revenue_lag
    expr: LAG(revenue, param('period_list')) OVER w
```

`LAG`'s offset argument still requires a genuine `int`-typed parameter.

## 5. Migration table

| Old DSL | SQL |
|---|---|
| `sum(x)` `mean(x)` `min(x)` `max(x)` `median(x)` `std(x)` `var(x)` | `SUM(x)` `AVG(x)` `MIN(x)` `MAX(x)` `MEDIAN(x)` `STDDEV(x)` `VARIANCE(x)` |
| `first(x)` / `last(x)` | `FIRST(x)` / `LAST(x)` |
| `count()` / `count(x)` | `COUNT(*)` / `COUNT(x)` |
| `count_distinct(x)` | `COUNT(DISTINCT x)` |
| `col("name")` | `"name"` |
| `where(value, pred)` | `SUM(value) FILTER (WHERE pred)` — the aggregate moves outside |
| `if_(pred, a, b)` | `CASE WHEN pred THEN a ELSE b END` |
| `coalesce(a, b)` | `COALESCE(a, b)` |
| `cast(x, "int")` | `CAST(x AS BIGINT)` |
| `x in [1, 2]` | `x IN (1, 2)` |
| `running_total(m)` | `SUM(m) OVER w` |
| `lag(m, n)` | `LAG(m, n) OVER w` |
| `frame:` + `expr: pl.col(...).median()` | `from:` + `expr: MEDIAN(...)` |

Old syntax is a **load-time error**, not a silent reinterpretation. The
error names the SQL equivalent from this table — `sum(x)` in particular
parses as valid SQL, so the check is explicit rather than incidental:
lowercase-only aggregate calls are accepted (SQL is case-insensitive), but
`where(...)`, `if_(...)`, `col(...)`, `count_distinct(...)`,
`running_total(...)` and `cast(x, "int")` are not SQL functions and are
reported with their replacement.

## 6. What is refused

An `expr:` is refused if it:

- is not an aggregate or window function (a bare column reference is not a
  measure);
- names a function outside the allowlist (see `sql-validator.md`);
- contains a subquery, a table reference, a star, a lambda, a prepared
  parameter, or a table function;
- references a column the source does not have (where a live schema is
  available);
- exceeds the size or nesting bounds.

A `from:` block is refused if it:

- names any base table other than `{model}` or a CTE it declares itself;
- uses **any** table function — this is what denies it file, HTTP and catalog
  access;
- is not a single `SELECT` statement;
- fails to carry the query's dimension columns through to its output.
