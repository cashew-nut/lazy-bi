# Contract: `app/sqlgrammar.py`

The security boundary. Everything authored — a measure `expr:`, a `from:`
block, an inline measure from an unauthenticated visual — passes through here
before it touches a connection.

## Method

DuckDB's own parser, via `json_serialize_sql()`, which returns the parsed AST
as JSON without planning or executing anything. The validator walks that JSON
and refuses anything not explicitly allowed. It is the same posture spec 008
established with Python's `ast` module, using the parser that matches the
language being parsed.

`json_serialize_sql` is a pure parse. It reaches no catalog, opens no file and
resolves no name, so validating a hostile string is not itself an execution.

## Two profiles

### `EXPRESSION` — a measure's `expr:`

Wrapped as `SELECT <text>` and the single select-list node is walked.

**Allowed node classes**: `CONSTANT`, `COLUMN_REF`, `FUNCTION`, `OPERATOR`,
`COMPARISON`, `CONJUNCTION`, `CASE`, `CAST`, `COLLATE`, `BETWEEN`,
`WINDOW` (window profile only).

**Refused**: `SUBQUERY`, `STAR`, `LAMBDA`, `PARAMETER`, `POSITIONAL_REFERENCE`,
`DEFAULT`, `TABLE_FUNCTION`, and any class not listed above — fail closed, so a
future DuckDB node type is refused until someone reads it.

Also refused: more than one statement, a statement that is not a `SELECT`, a
`FROM` clause, any modifier (`LIMIT`, `ORDER BY` at statement level, `QUALIFY`),
and a top-level expression that is not an aggregate or window function.

### `RELATION` — a measure's `from:` block

The whole statement is walked.

**Additionally allowed**: CTEs, joins, `WHERE`, `GROUP BY`, `HAVING`,
`QUALIFY`, set operations, `ORDER BY`/`LIMIT` inside the block, subqueries,
and window specifications.

**Still refused**: any table function at all, and any base-table reference
other than `{model}` (post-substitution: the engine's own CTE name) or a CTE
the block itself declares. This is the whole of the I/O boundary — with table
functions gone there is no `read_parquet`, no `read_csv`, no `glob`, no
`iceberg_scan`, no `duckdb_settings`, no `sniff_csv`.

## The function allowlist

Built once per process from DuckDB's own catalog:

```sql
SELECT DISTINCT function_name FROM duckdb_functions()
WHERE function_type IN ('scalar', 'aggregate', 'window', 'macro')
```

minus an explicit **deny set** covering, by name and by prefix:

- filesystem and network: `read_*`, `glob`, `sniff_csv`, `parquet_*`,
  `iceberg_*`, `delta_*`, `postgres_*`, `mysql_*`, `sqlite_*`, `arrow_scan`,
  `load_extension`, `install_extension`;
- catalog and configuration: `duckdb_*`, `pg_*`, `current_setting`,
  `set_config`, `create_secret`, `which_secret`, `checkpoint`, `nextval`,
  `currval`, `error`, `getvariable`, `setseed`;
- anything whose name is not a plain identifier.

Deriving the allowlist from the catalog rather than hardcoding it means a
DuckDB upgrade that adds `regexp_split_to_table` does not need a code change,
while one that adds `read_azure` is denied by the `read_*` prefix rule. A test
pins the deny set against the live catalog so a rename in DuckDB surfaces as a
failing test rather than a silent hole.

`param` is the one name the validator knows that DuckDB does not: it is
resolved to a literal and removed from the tree before the allowlist is
consulted.

## Bounds

Unchanged in spirit from spec 008: a maximum text length, a maximum node
count and a maximum nesting depth, each reported as `limit_exceeded`.

## Error kinds

`disallowed` · `unknown_function` · `unknown_column` · `unknown_parameter` ·
`limit_exceeded` · `not_aggregate` · `legacy_dsl`

`legacy_dsl` is the migration path: text matching an old-DSL construct is
reported with its SQL equivalent rather than a generic parse error.

## What this does not claim

The validator bounds *what a query may name*, not what it may cost. A
permitted expression can still be slow. Resource limits — the row cap, the
statement timeout, DuckDB's memory limit — are separate mechanisms and stay
separate.
