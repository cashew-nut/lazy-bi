"""The grammar boundary: every fragment of SQL anyone authors passes through
here before it reaches a connection.

Measures — model-saved or supplied inline on a query by whoever is looking at
a visual — are SQL now, which is a much larger language than the allowlisted
Python expressions it replaces. The safety property is kept the same way spec
008 kept it, with the parser that matches the language: DuckDB's own parser,
reached through `json_serialize_sql()`, which returns a parsed AST as JSON
without planning, binding, or touching a catalog. This module walks that JSON
and refuses anything not explicitly allowed.

Two things make that a real boundary rather than a filter:

  - **Fail closed.** An unrecognized node class is refused, so a DuckDB
    upgrade that introduces one cannot widen the grammar by accident.
  - **The author's text is never embedded.** What the engine emits is the
    *validated AST, re-serialized* — so a trailing `--`, an unbalanced quote
    or anything else that survives a naive parse cannot reach the statement
    the engine builds. `SELECT sum(x) -- ` validates and is emitted as
    `sum(x)`, not as something that comments out the rest of a select list.

What this module does not do is bound *cost*. A permitted expression can
still be slow; the row cap, the statement timeout and DuckDB's memory limit
are separate mechanisms and stay separate.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Literal, Optional

import duckdb

from . import duck

MAX_SQL_LEN = 2000
MAX_RELATION_LEN = 20_000
MAX_NODES = 400
MAX_DEPTH = 40

ErrorKind = Literal[
    "disallowed", "unknown_function", "unknown_column", "unknown_parameter",
    "limit_exceeded", "not_aggregate", "legacy_dsl", "syntax",
]


class SqlCompileError(ValueError):
    def __init__(self, message: str, kind: ErrorKind = "disallowed"):
        super().__init__(message)
        self.kind = kind


# ── the old DSL, reported rather than reinterpreted ──────────────────────
# `sum(x)` is valid SQL and means the same thing, so most of the old grammar
# migrates by doing nothing. These are the constructs that are *not* SQL, and
# a generic "unknown function" for them would send an author looking for a
# typo instead of at the migration table. See
# specs/018-duckdb-sql-engine/contracts/measure-sql.md.
LEGACY_REPLACEMENTS = {
    "count_distinct": "COUNT(DISTINCT x)",
    "where": "an aggregate with a FILTER clause, e.g. SUM(x) FILTER (WHERE pred)",
    "if_": "CASE WHEN pred THEN a ELSE b END",
    "col": "the column name on its own, quoted if it needs it",
    "running_total": "SUM(measure) OVER w",
}
# Only consulted when a parse has already *failed*, so it never has to
# distinguish `where(` from SQL's own WHERE keyword — `SUM(x) FILTER (WHERE
# p)` parses, and never reaches this. A construct that parses cleanly is
# caught by name during the walk instead (_check_function), which is
# unambiguous.
_LEGACY_CALL_RE = re.compile(
    r"\b(count_distinct|where|if_|col|running_total)\s*\(", re.IGNORECASE)
# cast(x, "int") — SQL spells it CAST(x AS BIGINT), and the two-argument form
# is a syntax error rather than an unknown overload
_LEGACY_CAST_RE = re.compile(r"\bcast\s*\(\s*[^(),]+\s*,", re.IGNORECASE)

# Window functions written without an OVER clause — the shape the old DSL's
# running_total()/lag() migrate into if someone drops the `OVER w`. DuckDB
# accepts these at parse time and only complains when it binds them, which is
# too late to say anything useful.
_WINDOW_ONLY = {
    "lag", "lead", "rank", "dense_rank", "row_number", "ntile", "first_value",
    "last_value", "nth_value", "cume_dist", "percent_rank",
}


def _legacy_hint(text: str) -> Optional[str]:
    """The migration message for `text`, if it looks like the old measure DSL
    — else None, and the caller reports the parse error it already had."""
    match = _LEGACY_CALL_RE.search(text)
    if match:
        name = match.group(1).lower()
        suffix = " (the engine supplies the named window `w`)" if name == "running_total" else ""
        return (f"'{name}(...)' is the old measure DSL — write "
                f"{LEGACY_REPLACEMENTS[name]} instead{suffix}")
    if _LEGACY_CAST_RE.search(text):
        return 'cast(x, "int") is the old measure DSL — write CAST(x AS BIGINT) instead'
    return None


# ── node classes ─────────────────────────────────────────────────────────
# Fail closed: anything not listed is refused, including a class a future
# DuckDB introduces. The two profiles differ only in whether a fragment may
# name relations of its own.

_EXPRESSION_CLASSES = {
    "CONSTANT", "COLUMN_REF", "FUNCTION", "OPERATOR", "COMPARISON",
    "CONJUNCTION", "CASE", "CAST", "COLLATE", "BETWEEN", "WINDOW",
    "LAMBDA", "LAMBDA_REF", "STRUCT_PACK",
}
# a relation may also contain subqueries, stars in its own select list, and
# the node types a join/CTE/set-operation is made of
_RELATION_CLASSES = _EXPRESSION_CLASSES | {"SUBQUERY", "STAR"}

# never allowed anywhere: a prepared-statement parameter would let a caller
# smuggle a value past the validator at bind time, and a positional reference
# means something different in every position it is transplanted into
_NEVER = {"PARAMETER", "POSITIONAL_REFERENCE", "DEFAULT"}


# ── the function allowlist ───────────────────────────────────────────────
# Derived from DuckDB's own catalog rather than hardcoded, so a DuckDB upgrade
# that adds a maths function needs no code change here. The structural half of
# the rule does the heavy lifting: only scalar, aggregate and macro functions
# are eligible at all, which excludes every one of the ~150 *table* functions
# in one move — read_parquet, read_csv, glob, sniff_csv, delta_scan,
# iceberg_scan, arrow_scan and the whole duckdb_*/pg_* introspection family.
# The deny set below then covers the scalars that remain risky.

_DENY_PREFIXES = ("pg_", "duckdb_", "read_", "write_", "sniff_", "install_", "load_")
_DENY_NAMES = {
    # configuration and catalog introspection
    "current_setting", "current_catalog", "current_database", "current_schema",
    "current_schemas", "current_role", "current_user", "current_query",
    "current_query_id", "current_connection_id", "current_transaction_id",
    "which_secret", "version", "getvariable",
    # stateful or side-effecting
    "nextval", "currval", "setseed", "error", "sleep_ms", "checkpoint",
    "force_checkpoint", "enable_object_cache", "disable_object_cache",
    "enable_checkpoint_on_shutdown", "disable_checkpoint_on_shutdown",
    "glob", "copy_database", "copy_dir",
    # nondeterministic: a measure whose value changes when nothing changed is
    # a support problem, and it silently breaks instant-mode extracts
    "random", "uuid", "uuidv4", "uuidv7", "gen_random_uuid",
}
# `param` is the one name this module knows that DuckDB does not: a visual
# parameter reference, resolved to a literal before anything is emitted.
PARAM_FUNCTION = "param"

_catalog_lock = threading.Lock()
_allowed_functions: Optional[set[str]] = None


def allowed_functions() -> set[str]:
    """Every function an authored expression may call, read once from the live
    catalog. Cached: the catalog only changes when an extension loads, and
    every extension this process will ever load is loaded when the connection
    opens."""
    global _allowed_functions
    with _catalog_lock:
        if _allowed_functions is not None:
            return _allowed_functions
    rows = duck.cursor().execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() "
        "WHERE function_type IN ('scalar', 'aggregate', 'macro')"
    ).fetchall()
    names = {
        name for (name,) in rows
        if name and not name.startswith(_DENY_PREFIXES) and name not in _DENY_NAMES
    }
    with _catalog_lock:
        _allowed_functions = names
    return names


def _reset_catalog() -> None:
    """Forget the cached allowlist. Tests that swap the connection use this;
    nothing in the request path does."""
    global _allowed_functions
    with _catalog_lock:
        _allowed_functions = None


# aggregate and window function names, needed to answer "is this expression
# actually a measure" — a bare column reference is not one.
_aggregate_names: Optional[set[str]] = None


def aggregate_functions() -> set[str]:
    global _aggregate_names
    if _aggregate_names is None:
        rows = duck.cursor().execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() "
            "WHERE function_type IN ('aggregate', 'macro')"
        ).fetchall()
        _aggregate_names = {name for (name,) in rows if name}
    return _aggregate_names


# ── parsing ──────────────────────────────────────────────────────────────
# One wrapper serves both profiles. The `FROM (SELECT 1)` is what makes a
# fragment carrying its own FROM clause a parse error rather than something
# that quietly reads another table, and the WINDOW clause is what lets
# `OVER w` parse at all — DuckDB resolves a named window syntactically, so it
# has to exist before the parser will accept a reference to it.
_CTX = "FROM (SELECT 1) AS __ctx"


def _parse(sql: str, original: Optional[str] = None) -> dict:
    """`sql`'s AST, or a SqlCompileError carrying DuckDB's own message.

    Parse only: no plan, no binding, no catalog lookup, no I/O. Validating a
    hostile string is therefore not itself an execution."""
    try:
        raw = duck.cursor().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    except duckdb.Error as exc:                              # pragma: no cover
        raise SqlCompileError(f"could not parse SQL: {exc}", kind="syntax") from exc
    parsed = json.loads(raw)
    if parsed.get("error"):
        hint = _legacy_hint(original if original is not None else sql)
        if hint:
            raise SqlCompileError(hint, kind="legacy_dsl")
        message = parsed.get("error_message") or "invalid SQL"
        raise SqlCompileError(message.strip().replace("\n", " "), kind="syntax")
    statements = parsed.get("statements") or []
    if len(statements) != 1:
        raise SqlCompileError(
            f"expected one statement, got {len(statements)}", kind="disallowed")
    return parsed


_TEMPLATE: Optional[dict] = None


def _template() -> dict:
    """A parsed `SELECT 1`, used as the clean statement a validated expression
    node is transplanted into before being re-serialized. Building the output
    this way — rather than by handing back the author's own text — is what
    makes comments, trailing tokens and whitespace tricks structurally unable
    to reach the emitted statement."""
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = _parse("SELECT 1")
    return json.loads(json.dumps(_TEMPLATE))


def _emit(node: dict) -> str:
    """One validated expression node, back to SQL text."""
    statement = _template()
    node = json.loads(json.dumps(node))
    node["alias"] = ""
    statement["statements"][0]["node"]["select_list"] = [node]
    try:
        sql = duck.cursor().execute(
            "SELECT json_deserialize_sql(?)", [json.dumps(statement)]).fetchone()[0]
    except duckdb.Error as exc:                              # pragma: no cover
        raise SqlCompileError(f"could not render SQL: {exc}") from exc
    prefix = "SELECT "
    if not sql.startswith(prefix):                           # pragma: no cover
        raise SqlCompileError("could not render SQL: unexpected serializer output")
    return sql[len(prefix):]


# ── the walk ─────────────────────────────────────────────────────────────

class _Walker:
    def __init__(self, *, classes: set[str], schema: Optional[dict],
                 window: bool, allow_tables: Optional[set[str]] = None):
        self.classes = classes
        self.schema = schema
        # window mode: bare identifiers are sibling *measure* names over an
        # already-aggregated relation, not raw source columns, so `schema` (when
        # given) is the aggregated schema and aggregates are not available
        self.window = window
        self.allow_tables = allow_tables
        self.columns: set[str] = set()
        self.functions: set[str] = set()
        self.tables: set[str] = set()
        self.saw_window = False
        self.nodes = 0

    def walk(self, node: Any, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            raise SqlCompileError(
                f"expression exceeds maximum nesting depth ({MAX_DEPTH})",
                kind="limit_exceeded")
        if isinstance(node, list):
            for item in node:
                self.walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise SqlCompileError(
                f"expression exceeds maximum size ({MAX_NODES} nodes)",
                kind="limit_exceeded")

        cls = node.get("class")
        if cls:
            self._check_class(cls, node)
        # a table reference is not an expression class; it carries `type` instead
        node_type = node.get("type")
        if node_type in ("BASE_TABLE", "TABLE_FUNCTION", "SUBQUERY", "JOIN",
                         "CROSS_PRODUCT", "EMPTY", "EXPRESSION_LIST", "PIVOT",
                         "SHOW_REF", "DELIM_GET", "COLUMN_DATA"):
            self._check_table(node_type, node)

        for key, value in node.items():
            if key in ("query_location", "alias", "column_names", "type", "class"):
                continue
            self.walk(value, depth + 1)

    def _check_class(self, cls: str, node: dict) -> None:
        if cls in _NEVER:
            raise SqlCompileError(
                f"{cls.lower().replace('_', ' ')} is not allowed in a measure expression")
        if cls not in self.classes:
            raise SqlCompileError(f"{cls.lower().replace('_', ' ')} is not allowed here")
        if cls == "WINDOW":
            self.saw_window = True
            self._check_function(node, is_window=True)
        elif cls == "COLUMN_REF":
            self._check_column(node)
        elif cls == "FUNCTION":
            self._check_function(node)

    def _check_column(self, node: dict) -> None:
        names = node.get("column_names") or []
        if not names:
            return
        # a qualified reference either reaches into a struct (fine — the first
        # part is still a column of this relation) or names another relation,
        # which in an expression there is no legitimate reason to do
        root = names[0]
        self.columns.add(root)
        if self.schema is not None and root not in self.schema:
            what = "measure" if self.window else "column"
            raise SqlCompileError(f"unknown {what} '{root}'", kind="unknown_column")

    def _check_function(self, node: dict, is_window: bool = False) -> None:
        name = (node.get("function_name") or "").lower()
        schema_name = node.get("schema") or ""
        if schema_name and schema_name.lower() not in ("main", ""):
            raise SqlCompileError(
                f"schema-qualified function '{schema_name}.{name}' is not allowed")
        if name == PARAM_FUNCTION:
            self.functions.add(name)
            return          # resolved before this walk, or reported by the caller
        if name in LEGACY_REPLACEMENTS:
            suffix = (" (the engine supplies the named window `w`)"
                      if name == "running_total" else "")
            raise SqlCompileError(
                f"'{name}(...)' is the old measure DSL — write "
                f"{LEGACY_REPLACEMENTS[name]} instead{suffix}", kind="legacy_dsl")
        if name in _WINDOW_ONLY and not is_window:
            raise SqlCompileError(
                f"{name.upper()} is a window function and needs an OVER clause — "
                f"write {name.upper()}(…) OVER w", kind="legacy_dsl")
        if name not in allowed_functions():
            raise SqlCompileError(f"unknown or disallowed function '{name}'",
                                  kind="unknown_function")
        self.functions.add(name)

    def _check_table(self, node_type: str, node: dict) -> None:
        if self.allow_tables is None:
            raise SqlCompileError("a measure expression cannot read a table")
        if node_type == "TABLE_FUNCTION":
            raise SqlCompileError(
                "table functions (read_parquet, read_csv, glob, …) are not allowed — "
                "a from: block reads {model} and its own CTEs, nothing else")
        if node_type == "BASE_TABLE":
            name = (node.get("table_name") or "").lower()
            catalog = node.get("catalog_name") or ""
            schema_name = node.get("schema_name") or ""
            if catalog or (schema_name and schema_name.lower() != "main"):
                raise SqlCompileError(
                    f"qualified table reference '{catalog}.{schema_name}.{name}' is not allowed")
            self.tables.add(name)
            if name not in self.allow_tables:
                allowed = ", ".join(sorted(self.allow_tables)) or "nothing"
                raise SqlCompileError(
                    f"a from: block may only read {allowed} and CTEs it declares "
                    f"itself — '{name}' is neither")


# ── visual parameters ────────────────────────────────────────────────────
# `param('name')` stays a call in the grammar, exactly as it was in the old
# DSL, and is resolved to a literal *before* parsing. Substituting first and
# validating after is what keeps it safe: whatever lands in the text is
# re-parsed and walked like everything else, so a value that tried to be more
# than a literal fails the walk.

_PARAM_RE = re.compile(r"""\bparam\s*\(\s*(['"])(?P<name>[A-Za-z_][A-Za-z0-9_ ]*)\1\s*\)""")


def referenced_parameter_names(text: str) -> set:
    return {m.group("name") for m in _PARAM_RE.finditer(text)}


def lag_period_param_names(text: str) -> set:
    """Parameters used as the offset argument of LAG/LEAD, which must resolve
    to a genuine integer — a float-typed parameter is refused there even when
    its value happens to be whole."""
    try:
        parsed = _parse(f"SELECT {text} {_CTX} WINDOW w AS ()")
    except SqlCompileError:
        return set()
    found: set = set()

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("class") == "WINDOW" and (node.get("function_name") or "").lower() in ("lag", "lead"):
            offsets = node.get("offset_expr")
            for candidate in ([offsets] if isinstance(offsets, dict) else []):
                if (candidate.get("class") == "FUNCTION"
                        and (candidate.get("function_name") or "").lower() == PARAM_FUNCTION):
                    name = _param_literal(candidate)
                    if name:
                        found.add(name)
        for value in node.values():
            visit(value)

    visit(parsed)
    return found


def _param_literal(node: dict) -> Optional[str]:
    children = node.get("children") or []
    if len(children) != 1:
        return None
    value = (children[0].get("value") or {}).get("value")
    return value if isinstance(value, str) else None


def _sql_literal(value: object) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _substitute_params(text: str, parameter_values: Optional[dict]) -> str:
    """Replace every `param('name')` with its resolved literal.

    `parameter_values` is None only during structural validation (model load,
    a save-time check with no visual in scope), where the reference is left
    in place and reported by the caller. At query time it is always supplied —
    possibly empty — and an unresolved name fails closed."""
    if parameter_values is None:
        return text

    def replace(match: re.Match) -> str:
        name = match.group("name")
        if name not in parameter_values:
            raise SqlCompileError(
                f"unknown parameter '{name}' — declare it on the visual first",
                kind="unknown_parameter")
        return _sql_literal(parameter_values[name])

    # LAG/LEAD's offset must be a genuine integer. A float-typed parameter is
    # refused there even when its value happens to be whole, and a string one
    # always — the declared type governs eligibility, not incidental JSON
    # shape, and DuckDB only complains about it when it binds.
    for name in lag_period_param_names(text):
        value = parameter_values.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise SqlCompileError(
                f"LAG()'s offset argument references parameter '{name}', whose value "
                f"{value!r} is not an integer", kind="unknown_parameter")

    substituted = _PARAM_RE.sub(replace, text)
    # anything still spelling `param(` after that is a form the grammar does
    # not accept (a non-literal name, say), and must not reach the walk as a
    # bare unknown function
    if re.search(r"\bparam\s*\(", substituted, re.IGNORECASE):
        raise SqlCompileError(
            "param() takes one quoted parameter name, e.g. param('threshold')",
            kind="unknown_parameter")
    return substituted


# ── the public surface ───────────────────────────────────────────────────

def is_window_expr(text: str) -> bool:
    """Does this expression read *across* already-aggregated rows?

    True for anything containing a window function, which is what makes bare
    identifiers mean sibling measures rather than source columns and moves the
    whole expression to after the group-by."""
    try:
        parsed = _parse(f"SELECT {text} {_CTX} WINDOW w AS ()")
    except SqlCompileError:
        return False
    return '"class":"WINDOW"' in json.dumps(parsed, separators=(",", ":"))


def compile_expression(
    text: str,
    schema: Optional[dict] = None,
    *,
    window: bool = False,
    window_spec: str = "",
    parameter_values: Optional[dict] = None,
    require_aggregate: bool = True,
) -> str:
    """Validate one measure expression and return the SQL to embed.

    The returned text is rendered from the validated AST, never from `text`
    itself. In window mode `window_spec` is the engine's `PARTITION BY … ORDER
    BY …`, which the parser resolves into every `OVER w` reference, so the
    emitted SQL carries the whole window and depends on no named window
    downstream.

    `schema` maps column name -> type for the relation the expression reads
    (the aggregated one in window mode). None skips the existence check, which
    is what model load does — parsing config should not fetch a schema from
    S3 — while every path that has a live schema uses it.
    """
    if not isinstance(text, str) or not text.strip():
        raise SqlCompileError("measure expression is empty")
    if len(text) > MAX_SQL_LEN:
        raise SqlCompileError(
            f"measure expression exceeds {MAX_SQL_LEN} characters", kind="limit_exceeded")
    prepared = _substitute_params(text, parameter_values)
    parsed = _parse(f"SELECT {prepared} {_CTX} WINDOW w AS ({window_spec})", text)
    node = parsed["statements"][0]["node"]
    select_list = node.get("select_list") or []
    if len(select_list) != 1:
        raise SqlCompileError(
            "a measure is one expression — remove the comma, or use a single value")
    for clause in ("where_clause", "having", "qualify"):
        if node.get(clause):
            raise SqlCompileError(f"a measure expression cannot have a {clause.split('_')[0]} clause")
    if node.get("group_expressions") or node.get("modifiers"):
        raise SqlCompileError("a measure expression cannot have its own grouping or ordering")

    expression = select_list[0]
    walker = _Walker(classes=_EXPRESSION_CLASSES, schema=schema, window=window)
    walker.walk(expression)
    if parameter_values is None and PARAM_FUNCTION in walker.functions:
        raise SqlCompileError(
            "param() needs a visual to be scoped to", kind="unknown_parameter")
    if window and not walker.saw_window:
        raise SqlCompileError(
            "a window measure must use a window function, e.g. SUM(revenue) OVER w")
    if walker.saw_window and not window:
        # the engine routes by is_window_expr() and never gets here; compiling
        # one in aggregate mode anyway would emit an empty OVER () and silently
        # compute a grand total instead of a running one
        raise SqlCompileError(
            "this is a window measure (it uses a window function) and has to be "
            "compiled after the group-by")
    if require_aggregate and not window:
        _require_aggregate(expression, walker)
    return _emit(expression)


def _require_aggregate(expression: dict, walker: _Walker) -> None:
    """A measure reduces a group of rows to one value. A bare column, or an
    expression built only from scalar functions, does not — and produces a
    binder error several layers down rather than a message the author can act
    on, so it is caught here."""
    if walker.saw_window:
        return
    aggregates = walker.functions & aggregate_functions()
    if not aggregates:
        raise SqlCompileError(
            "a measure must reduce to one value per group — wrap it in an aggregate "
            "such as SUM(), COUNT(), AVG() or MEDIAN()", kind="not_aggregate")


def compile_relation(
    text: str,
    *,
    allowed_tables: set,
    max_len: int = MAX_RELATION_LEN,
) -> str:
    """Validate a measure's `from:` block and return the SQL to embed.

    A relation may do everything an expression may, plus declare CTEs, join,
    group, window and nest subqueries. What it may not do is reach outside
    itself: **no table functions at all**, and no base table other than the
    ones in `allowed_tables` (the engine's own CTE for `{model}`) or a CTE the
    block declares. That single rule is the whole I/O boundary — with table
    functions gone there is no read_parquet, no read_csv, no glob, no
    iceberg_scan, no duckdb_settings.
    """
    if not isinstance(text, str) or not text.strip():
        raise SqlCompileError("from: block is empty")
    if len(text) > max_len:
        raise SqlCompileError(
            f"from: block exceeds {max_len} characters", kind="limit_exceeded")
    parsed = _parse(text, text)
    statement = parsed["statements"][0]
    node = statement.get("node") or {}
    if node.get("type") not in ("SELECT_NODE", "SET_OPERATION_NODE"):
        raise SqlCompileError("a from: block must be a SELECT statement")

    ctes = _cte_names(node)
    walker = _Walker(classes=_RELATION_CLASSES, schema=None, window=False,
                     allow_tables={t.lower() for t in allowed_tables} | ctes)
    walker.walk(node)
    try:
        return duck.cursor().execute(
            "SELECT json_deserialize_sql(?)", [json.dumps(parsed)]).fetchone()[0]
    except duckdb.Error as exc:                              # pragma: no cover
        raise SqlCompileError(f"could not render from: block: {exc}") from exc


def _cte_names(node: dict) -> set:
    """Every CTE name in scope anywhere in the statement. Collected across the
    whole tree rather than per-scope: a name shadowing a real table is not a
    risk here, because the only tables in scope at all are ones this module
    already allowed."""
    names: set = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        cte_map = value.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map") or []:
                key = entry.get("key")
                if isinstance(key, str):
                    names.add(key.lower())
        for item in value.values():
            visit(item)

    visit(node)
    return names


def referenced_names(text: str) -> set:
    """Bare identifiers an expression reads. In a window measure these are
    sibling measure names, which is how the engine knows what to compute even
    when the query didn't ask for it directly."""
    try:
        parsed = _parse(f"SELECT {text} {_CTX} WINDOW w AS ()")
    except SqlCompileError:
        return set()
    names: set = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("class") == "COLUMN_REF":
            parts = value.get("column_names") or []
            if parts:
                names.add(parts[0])
        for item in value.values():
            visit(item)

    visit(parsed["statements"][0]["node"]["select_list"])
    return names


def referenced_columns(text: str) -> set:
    """Alias of referenced_names, for callers reading raw source columns —
    same answer, different intent at the call site."""
    return referenced_names(text)


# ── decomposition, for instant-mode extracts ─────────────────────────────
# Re-aggregating an already-aggregated extract in the browser is only sound
# for measures that decompose into additive parts. This walks a validated
# expression once, collecting those parts and the formula that recombines
# them, and gives up (returns None) the moment it meets something that would
# not survive a roll-up — which sends the tile back to the live query path
# rather than rendering a plausible-looking wrong number.

_ROLLUP_AGGS = {"sum": "sum", "count": "sum", "count_star": "sum",
                "min": "min", "max": "max"}
_FORMULA_OPS = {"+", "-", "*", "/"}
# aggregates that read more than their own group's total: a roll-up of these
# is not the same number, so a measure containing one is not decomposable
_NON_ADDITIVE = {"median", "avg", "mean", "stddev", "stddev_samp", "stddev_pop",
                 "variance", "var_samp", "var_pop", "first", "last", "mode",
                 "quantile", "quantile_cont", "quantile_disc", "arg_min", "arg_max",
                 "string_agg", "list", "array_agg", "histogram", "approx_count_distinct"}


class _NotDecomposable(Exception):
    pass


class _Decomposer:
    def __init__(self) -> None:
        self.components: list[dict] = []

    def _component(self, agg: str, node: dict) -> dict:
        """Register one additive part (de-duplicated: two references to the
        same aggregate share one extract column) and return its reference."""
        text = _emit(node)
        for index, existing in enumerate(self.components):
            if existing["agg"] == agg and existing["expr"] == text:
                return {"ref": index}
        self.components.append({"agg": agg, "expr": text})
        return {"ref": len(self.components) - 1}

    def walk(self, node: dict) -> dict:
        cls = node.get("class")
        if cls == "CONSTANT":
            return {"const": _numeric_constant(node)}
        if cls != "FUNCTION":
            # a bare identifier, a comparison, a CASE — none of these mean
            # anything applied to an already-rolled-up row
            raise _NotDecomposable()
        return self._function(node)

    def _function(self, node: dict) -> dict:
        name = (node.get("function_name") or "").lower()
        children = node.get("children") or []
        if name in _FORMULA_OPS and len(children) == 2:
            return {"op": name, "l": self.walk(children[0]), "r": self.walk(children[1])}
        if name == "-" and len(children) == 1:
            return {"op": "-", "l": {"const": 0}, "r": self.walk(children[0])}
        if name == "+" and len(children) == 1:
            return self.walk(children[0])
        if node.get("distinct") or (node.get("order_bys") or {}).get("orders"):
            raise _NotDecomposable()     # a distinct count cannot be re-summed
        if name in ("avg", "mean"):
            # the whole point of decomposition: a mean is a ratio of two
            # additive parts, so it survives a roll-up exactly
            if len(children) != 1:
                raise _NotDecomposable()
            self._require_flat(node)
            return {"op": "/",
                    "l": self._component("sum", _renamed(node, "sum")),
                    "r": self._component("sum", _renamed(node, "count"))}
        agg = _ROLLUP_AGGS.get(name)
        if agg is None:
            raise _NotDecomposable()
        self._require_flat(node)
        return self._component(agg, node)

    def _require_flat(self, node: dict) -> None:
        """An aggregate nested inside another is a shape this cannot reason
        about — `sum(x)` inside a `min()` would silently change meaning under a
        roll-up."""
        aggregates = aggregate_functions()

        def visit(value: Any, top: bool) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item, False)
                return
            if not isinstance(value, dict):
                return
            if not top and value.get("class") == "FUNCTION":
                name = (value.get("function_name") or "").lower()
                if name in aggregates or name in _NON_ADDITIVE:
                    raise _NotDecomposable()
            if value.get("class") == "WINDOW":
                raise _NotDecomposable()
            for item in value.values():
                visit(item, False)

        visit(node.get("children"), False)
        visit(node.get("filter"), False)


def _numeric_constant(node: dict) -> float:
    """A CONSTANT node's Python number, or _NotDecomposable for anything the
    browser cannot recombine.

    DECIMAL is the trap: DuckDB serializes it as the *unscaled* integer plus a
    scale in its type info, so reading `value` alone turns 1.5 into 15 — and a
    measure like `SUM(x) * 1.5` would then be re-aggregated ten times too
    large on the client while the live path stayed correct."""
    value = node.get("value") or {}
    if value.get("is_null"):
        raise _NotDecomposable()
    type_info = value.get("type") or {}
    raw = value.get("value")
    if type_info.get("id") == "DECIMAL":
        scale = ((type_info.get("type_info") or {}).get("scale")) or 0
        if not isinstance(raw, int):
            raise _NotDecomposable()
        return raw / (10 ** scale)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _NotDecomposable()
    if type_info.get("id") not in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT",
                                   "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
                                   "UBIGINT", "FLOAT", "DOUBLE"):
        raise _NotDecomposable()
    return raw


def _renamed(node: dict, function_name: str) -> dict:
    clone = json.loads(json.dumps(node))
    clone["function_name"] = function_name
    return clone


def rollup_plan(text: str) -> Optional[dict]:
    """Decompose a measure expression for client-side re-aggregation.

    Returns `{"components": [{"agg", "expr"}, ...], "formula": <tree>}` where
    the formula is a JSON-safe tree of `{"ref": i}` / `{"const": n}` /
    `{"op": "+|-|*|/", "l", "r"}` nodes, or None when the measure cannot be
    recomputed from its own already-aggregated output.

    Components are SQL text rendered from this expression's own validated AST,
    and are re-validated by compile_expression() through the ordinary
    inline-measure path — so they carry no more language power than the measure
    they came from. Parse-only; never executes `text`.
    """
    if not isinstance(text, str) or len(text) > MAX_SQL_LEN:
        return None
    try:
        parsed = _parse(f"SELECT {text} {_CTX} WINDOW w AS ()")
    except SqlCompileError:
        return None
    select_list = parsed["statements"][0]["node"].get("select_list") or []
    if len(select_list) != 1:
        return None
    tree = json.dumps(parsed, separators=(",", ":"))
    if '"class":"WINDOW"' in tree:
        return None    # running totals and lags read neighbouring rows, not just their own
    if f'"function_name":"{PARAM_FUNCTION}"' in tree:
        # a parameter change forces a re-fetch either way, so distinguishing
        # "inside an aggregate" from "outside one" buys nothing
        return None
    decomposer = _Decomposer()
    try:
        formula = decomposer.walk(select_list[0])
    except (_NotDecomposable, SqlCompileError):
        return None
    return {"components": decomposer.components, "formula": formula} if decomposer.components else None
