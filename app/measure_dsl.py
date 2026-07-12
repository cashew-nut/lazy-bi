"""Safe measure compiler: parses a small DSL expression to an AST and builds a
polars.Expr from it via a strict allowlist. This module never calls eval,
exec, or compile on measure text — only ast.parse (structure only) and
constructors we write ourselves. Anything outside the allowlist raises
MeasureCompileError (fail closed).

Both inline (query-time, untrusted) and model (saved, authenticated) measures
compile through compile_measure(); Tier 1 auth grants governance over what
gets saved, not extra language power. The one exception is the pre-existing
"framed measure" construct (see app/semantic.py's compile_frame), which stays
on its own, narrower, authenticated-only path — never reachable from here.

Two compile modes share this one allowlist:
  - aggregate (default): bare identifiers are raw source columns; the
    expression must reduce every source row in a query group to one value
    (sum/mean/count/... — the existing behavior, unchanged).
  - window (auto-detected — see is_window_expr): triggered by the presence
    of running_total()/lag() anywhere in the expression. Bare identifiers
    here are *sibling measure names* (already-aggregated, one row per query
    group) rather than raw columns, since running totals and period-over-
    period math only make sense over an already-grouped result — there's
    no window into "the previous quarter" until quarters have been
    aggregated. The engine compiles these post-group-by, over(partition_by=
    the query's other dimensions, order_by=its time dimension).
"""
from __future__ import annotations

import ast
from typing import Literal, Optional

import polars as pl

MAX_MEASURE_LEN = 2000
MAX_NODES = 200
MAX_DEPTH = 30

ErrorKind = Literal["disallowed", "unknown_function", "unknown_column", "unknown_parameter", "limit_exceeded"]


class MeasureCompileError(ValueError):
    def __init__(self, message: str, kind: ErrorKind = "disallowed"):
        super().__init__(message)
        self.kind = kind


def _is_allowed_constant(value: object) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _check_identifier(name: str) -> None:
    # blocks __class__, __globals__, etc. anywhere in a name — the dunder walk
    # is the actual attribute-free escape hatch this compiler must close
    if "__" in name:
        raise MeasureCompileError(f"invalid identifier '{name}'", kind="disallowed")


def _numeric_literal(node: ast.AST) -> float:
    """Extract a plain Python number from a Constant or a +/-Constant, for the
    ** exponent bound check — the only place this compiler needs an actual
    Python value rather than a constructed pl.Expr."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _numeric_literal(node.operand)
        return inner if isinstance(node.op, ast.UAdd) else -inner
    raise MeasureCompileError("** exponent must be a constant number", kind="disallowed")


_COMPARE_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


class _Compiler:
    def __init__(
        self,
        schema: Optional["pl.Schema"],
        *,
        window: bool = False,
        partition_by: Optional[list] = None,
        order_by: Optional[str] = None,
        parameter_values: Optional[dict] = None,
    ):
        # schema is None only for the model-yaml load-time structural check
        # (no live schema is fetched just to parse config, matching this
        # codebase's lazy/no-scan-to-load-yaml precedent) — column existence
        # is otherwise always checked wherever a real schema is available
        # (query time, and the API's validate/generate/measure-save routes).
        # In window mode `schema` (when given) is the *aggregated* schema —
        # dimensions + sibling measure names — not raw source columns.
        self.schema = schema
        self.window = window
        # partition_by/order_by are the query's actual grouping — only known
        # once the engine has resolved the query's dimensions, so they're
        # None during structural-only validation (load time, measure save);
        # running_total()/lag() build the bare reduction then, without .over().
        self.partition_by = partition_by
        self.order_by = order_by
        # parameter_values maps a declared visual parameter's name to the one
        # already-validated int to substitute for it — the caller (engine.py
        # for real queries, api/models.py for the live measure check) has
        # already checked this value is a member of that parameter's declared
        # list; this compiler never sees the list itself, only the resolved
        # value, exactly like partition_by/order_by above. None during
        # structural-only validation, in which case any param() reference
        # fails closed with "unknown_parameter" (see _resolve_periods_arg).
        self.parameter_values = parameter_values

    def build(self, node: ast.AST, depth: int) -> pl.Expr:
        if depth > MAX_DEPTH:
            raise MeasureCompileError(f"expression exceeds maximum nesting depth ({MAX_DEPTH})", kind="limit_exceeded")
        if isinstance(node, ast.Constant):
            if not _is_allowed_constant(node.value):
                raise MeasureCompileError(f"unsupported literal type: {type(node.value).__name__}", kind="disallowed")
            return pl.lit(node.value)
        if isinstance(node, ast.Name):
            return self._build_name(node)
        if isinstance(node, ast.BinOp):
            return self._build_binop(node, depth)
        if isinstance(node, ast.UnaryOp):
            return self._build_unaryop(node, depth)
        if isinstance(node, ast.BoolOp):
            return self._build_boolop(node, depth)
        if isinstance(node, ast.Compare):
            return self._build_compare(node, depth)
        if isinstance(node, ast.Call):
            return self._build_call(node, depth)
        raise MeasureCompileError(f"unsupported syntax: {type(node).__name__}", kind="disallowed")

    def _build_name(self, node: ast.Name) -> pl.Expr:
        name = node.id
        _check_identifier(name)
        if self.schema is not None and name not in self.schema:
            what = "measure" if self.window else "column"
            raise MeasureCompileError(f"unknown {what} '{name}'", kind="unknown_column")
        return pl.col(name)

    def _build_binop(self, node: ast.BinOp, depth: int) -> pl.Expr:
        op_type = type(node.op)
        if op_type is ast.Pow:
            exponent = _numeric_literal(node.right)
            if not (-8 <= exponent <= 8):
                raise MeasureCompileError("** exponent must be between -8 and 8", kind="limit_exceeded")
        left = self.build(node.left, depth + 1)
        right = self.build(node.right, depth + 1)
        if op_type is ast.Add:
            return left + right
        if op_type is ast.Sub:
            return left - right
        if op_type is ast.Mult:
            return left * right
        if op_type is ast.Div:
            return left / right
        if op_type is ast.Mod:
            return left % right
        if op_type is ast.Pow:
            return left ** right
        raise MeasureCompileError(f"unsupported operator: {op_type.__name__}", kind="disallowed")

    def _build_unaryop(self, node: ast.UnaryOp, depth: int) -> pl.Expr:
        operand = self.build(node.operand, depth + 1)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.Not):
            return operand.not_()
        raise MeasureCompileError(f"unsupported unary operator: {type(node.op).__name__}", kind="disallowed")

    def _build_boolop(self, node: ast.BoolOp, depth: int) -> pl.Expr:
        values = [self.build(v, depth + 1) for v in node.values]
        result = values[0]
        if isinstance(node.op, ast.And):
            for v in values[1:]:
                result = result & v
            return result
        if isinstance(node.op, ast.Or):
            for v in values[1:]:
                result = result | v
            return result
        raise MeasureCompileError("unsupported boolean operator", kind="disallowed")

    def _build_literal_collection(self, node: ast.AST) -> list:
        if not isinstance(node, (ast.List, ast.Tuple)):
            raise MeasureCompileError(
                "'in'/'not in' requires a literal list/tuple of constants on the right", kind="disallowed"
            )
        values = []
        for elt in node.elts:
            if not isinstance(elt, ast.Constant) or not _is_allowed_constant(elt.value):
                raise MeasureCompileError(
                    "'in'/'not in' list must contain only literal constants", kind="disallowed"
                )
            values.append(elt.value)
        return values

    def _build_compare(self, node: ast.Compare, depth: int) -> pl.Expr:
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise MeasureCompileError("chained comparisons are not supported", kind="disallowed")
        op = node.ops[0]
        left = self.build(node.left, depth + 1)
        if isinstance(op, (ast.In, ast.NotIn)):
            values = self._build_literal_collection(node.comparators[0])
            expr = left.is_in(values)
            return expr if isinstance(op, ast.In) else expr.not_()
        op_type = type(op)
        if op_type not in _COMPARE_OPS:
            raise MeasureCompileError(f"unsupported comparison: {op_type.__name__}", kind="disallowed")
        right = self.build(node.comparators[0], depth + 1)
        return _COMPARE_OPS[op_type](left, right)

    def _build_call(self, node: ast.Call, depth: int) -> pl.Expr:
        if not isinstance(node.func, ast.Name):
            raise MeasureCompileError(
                "calls must use a bare function name (no attribute/expression calls)", kind="disallowed"
            )
        if node.keywords:
            raise MeasureCompileError("keyword arguments are not supported", kind="disallowed")
        name = node.func.id
        table = _WINDOW_FUNCTIONS if self.window else _FUNCTIONS
        builder = table.get(name)
        if builder is None:
            if self.window and name in _FUNCTIONS:
                raise MeasureCompileError(
                    f"'{name}()' reduces raw source rows and can't be used inside a window "
                    "measure — reference the sibling measure by its bare name instead",
                    kind="disallowed",
                )
            raise MeasureCompileError(f"unknown function '{name}'", kind="unknown_function")
        return builder(self, node.args, depth)


def _fn_agg(method: str):
    def builder(compiler: _Compiler, args: list, depth: int) -> pl.Expr:
        if len(args) != 1:
            raise MeasureCompileError(f"{method}() takes exactly 1 argument", kind="disallowed")
        return getattr(compiler.build(args[0], depth + 1), method)()
    return builder


def _fn_count(compiler: _Compiler, args: list, depth: int) -> pl.Expr:
    if len(args) == 0:
        return pl.len()
    if len(args) == 1:
        return compiler.build(args[0], depth + 1).count()
    raise MeasureCompileError("count() takes 0 or 1 arguments", kind="disallowed")


def _fn_count_distinct(compiler: _Compiler, args: list, depth: int) -> pl.Expr:
    if len(args) != 1:
        raise MeasureCompileError("count_distinct() takes exactly 1 argument", kind="disallowed")
    return compiler.build(args[0], depth + 1).n_unique()


def _string_literal_arg(node: ast.AST, what: str) -> str:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise MeasureCompileError(f"{what} requires a string literal argument", kind="disallowed")
    return node.value


def _fn_col(compiler: _Compiler, args: list, depth: int) -> pl.Expr:
    if len(args) != 1:
        raise MeasureCompileError("col() requires exactly 1 argument", kind="disallowed")
    name = _string_literal_arg(args[0], "col()")
    _check_identifier(name)
    if compiler.schema is not None and name not in compiler.schema:
        raise MeasureCompileError(f"unknown column '{name}'", kind="unknown_column")
    return pl.col(name)


def _fn_where(compiler: _Compiler, args: list, depth: int) -> pl.Expr:
    if len(args) != 2:
        raise MeasureCompileError("where() takes exactly 2 arguments (value, predicate)", kind="disallowed")
    value = compiler.build(args[0], depth + 1)
    predicate = compiler.build(args[1], depth + 1)
    return value.filter(predicate)


def _fn_if_(compiler: _Compiler, args: list, depth: int) -> pl.Expr:
    if len(args) != 3:
        raise MeasureCompileError("if_() takes exactly 3 arguments (predicate, then, else)", kind="disallowed")
    pred = compiler.build(args[0], depth + 1)
    then = compiler.build(args[1], depth + 1)
    otherwise = compiler.build(args[2], depth + 1)
    return pl.when(pred).then(then).otherwise(otherwise)


def _fn_coalesce(compiler: _Compiler, args: list, depth: int) -> pl.Expr:
    if not args:
        raise MeasureCompileError("coalesce() takes at least 1 argument", kind="disallowed")
    return pl.coalesce([compiler.build(a, depth + 1) for a in args])


_CAST_TYPES = {"int": pl.Int64, "float": pl.Float64, "str": pl.Utf8, "bool": pl.Boolean}


def _fn_cast(compiler: _Compiler, args: list, depth: int) -> pl.Expr:
    if len(args) != 2:
        raise MeasureCompileError('cast() requires (expr, "type")', kind="disallowed")
    target = _string_literal_arg(args[1], 'cast()\'s second argument')
    if target not in _CAST_TYPES:
        raise MeasureCompileError(
            f"unsupported cast type '{target}' (expected one of {sorted(_CAST_TYPES)})", kind="disallowed"
        )
    value = compiler.build(args[0], depth + 1)
    return value.cast(_CAST_TYPES[target])


_FUNCTIONS = {
    "sum": _fn_agg("sum"),
    "mean": _fn_agg("mean"),
    "min": _fn_agg("min"),
    "max": _fn_agg("max"),
    "median": _fn_agg("median"),
    "std": _fn_agg("std"),
    "var": _fn_agg("var"),
    "first": _fn_agg("first"),
    "last": _fn_agg("last"),
    "count": _fn_count,
    "count_distinct": _fn_count_distinct,
    "col": _fn_col,
    "where": _fn_where,
    "if_": _fn_if_,
    "coalesce": _fn_coalesce,
    "cast": _fn_cast,
}


def _is_param_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "param"


def _param_name_from_args(args: list) -> str:
    if len(args) != 1 or not isinstance(args[0], ast.Constant) or not isinstance(args[0].value, str):
        raise MeasureCompileError("param() takes exactly one string literal argument", kind="disallowed")
    return args[0].value


def _lookup_param(compiler: "_Compiler", name: str):
    """Return the pre-resolved, already-type-validated value a caller (engine.py
    for real queries, api/models.py for the live measure check) has stored for
    a declared parameter. This compiler never sees the parameter's declared
    values list or type — only the one resolved value, exactly like
    partition_by/order_by. None during structural-only validation, in which
    case any param() reference fails closed with 'unknown_parameter'."""
    if compiler.parameter_values is None or name not in compiler.parameter_values:
        raise MeasureCompileError(f"unknown parameter '{name}'", kind="unknown_parameter")
    return compiler.parameter_values[name]


def _fn_param(compiler: "_Compiler", args: list, depth: int) -> pl.Expr:
    """param('name') as a general expression — legal anywhere build() already
    recurses (comparisons, if_(), coalesce(), where(), cast()'s value
    argument, ...). Resolves to a polars literal of whatever type (int/float/
    str) the caller's pre-validated parameter_values dict holds; never the
    declared value list itself. See _resolve_periods_arg for the one
    position (lag()'s periods argument) that needs a raw Python int instead
    of a pl.Expr and layers its own extra type check on top of this same
    lookup."""
    name = _param_name_from_args(args)
    value = _lookup_param(compiler, name)
    if not _is_allowed_constant(value):
        raise MeasureCompileError(f"parameter '{name}' resolved to an unsupported value type", kind="disallowed")
    return pl.lit(value)


# registered after _FUNCTIONS' dict literal (above) since _fn_param is
# defined below it — param() is legal in both compile modes, exactly like
# if_()/coalesce()/cast() (a pure scalar substitution, no aggregation or raw-
# row semantics either way)
_FUNCTIONS["param"] = _fn_param


def _resolve_periods_arg(compiler: "_Compiler", node: ast.AST) -> int:
    """lag()'s periods argument accepts either a literal integer (unchanged)
    or param('name') — a visual-declared parameter reference. Unlike every
    other position param() can now appear in (see _fn_param), this one needs
    a raw Python int for polars' .shift(), not a pl.Expr, so it stays a
    bespoke AST-node inspection rather than routing through compiler.build().
    The resolved value must be a genuine Python int (not bool, not a
    numerically-whole float, not a str) — an int-typed parameter's value is
    always a real int by the time it reaches here (engine.py coerces per the
    declared type), so this check is what makes a float- or string-typed
    parameter fail here exactly like an incompatible literal already does,
    regardless of its numeric content."""
    if _is_param_call(node):
        name = _param_name_from_args(node.args)
        value = _lookup_param(compiler, name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise MeasureCompileError(
                "lag()'s periods argument must be a literal integer or an int-typed param('name')",
                kind="disallowed",
            )
        return value
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    raise MeasureCompileError(
        "lag()'s periods argument must be a literal integer or param('name')", kind="disallowed"
    )


def _fn_running_total(compiler: "_Compiler", args: list, depth: int) -> pl.Expr:
    if len(args) != 1:
        raise MeasureCompileError("running_total() takes exactly 1 argument", kind="disallowed")
    value = compiler.build(args[0], depth + 1)
    expr = value.cum_sum()
    if compiler.order_by is not None:
        expr = expr.over(compiler.partition_by or None, order_by=compiler.order_by)
    return expr


def _fn_lag(compiler: "_Compiler", args: list, depth: int) -> pl.Expr:
    if len(args) not in (1, 2):
        raise MeasureCompileError("lag() takes 1 or 2 arguments: (measure[, periods])", kind="disallowed")
    value = compiler.build(args[0], depth + 1)
    periods = 1
    if len(args) == 2:
        periods = _resolve_periods_arg(compiler, args[1])
        if periods < 1:
            raise MeasureCompileError("lag()'s periods argument must be a positive integer", kind="disallowed")
    expr = value.shift(periods)
    if compiler.order_by is not None:
        expr = expr.over(compiler.partition_by or None, order_by=compiler.order_by)
    return expr


# Functions legal inside a window measure. running_total/lag are the two
# window primitives; if_/coalesce/cast carry over unchanged (pure scalar
# transforms, no aggregation semantics) so e.g. a first-period null lag can
# be coalesced to 0. Aggregate functions (sum/mean/...), col(), and where()
# are deliberately absent — there are no raw source rows left to reduce or
# filter once a measure has become a window calculation over sibling
# measures' already-aggregated values.
_WINDOW_ONLY_FUNCTIONS = {"running_total", "lag"}

_WINDOW_FUNCTIONS = {
    "running_total": _fn_running_total,
    "lag": _fn_lag,
    "if_": _fn_if_,
    "coalesce": _fn_coalesce,
    "cast": _fn_cast,
    "param": _fn_param,
}


def is_window_expr(text: str) -> bool:
    """True if `text` uses running_total()/lag() anywhere — the signal that
    flips a measure from an aggregate reduction (raw columns -> one value per
    query group) into a window calculation (sibling measures' already-
    aggregated values -> a running total / lag within a query-time
    partition). Never evaluates `text`; parses only."""
    if len(text) > MAX_MEASURE_LEN:
        raise MeasureCompileError(f"measure text exceeds {MAX_MEASURE_LEN} character limit", kind="limit_exceeded")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise MeasureCompileError(f"invalid syntax: {exc}", kind="disallowed") from exc
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _WINDOW_ONLY_FUNCTIONS
        for n in ast.walk(tree)
    )


def referenced_names(text: str) -> set:
    """Bare identifiers a window measure's expression reads (sibling measure
    names), excluding function names — used by the engine to discover
    dependencies the caller didn't explicitly request (e.g. running_total
    (revenue) needs `revenue` computed even if only the running total was
    asked for). Never evaluates `text`; parses only."""
    tree = ast.parse(text, mode="eval")
    call_funcs = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - call_funcs


def referenced_parameter_names(text: str) -> set:
    """Names passed to param(...) anywhere in `text` — not just inside a
    legal lag() position, since this is used to detect and reject the
    construct where it's out of scope (e.g. a model-measure save, where no
    visual parameter context exists at all). Never evaluates `text`; parses
    only, same posture as referenced_names()/is_window_expr()."""
    tree = ast.parse(text, mode="eval")
    return {
        n.args[0].value
        for n in ast.walk(tree)
        if _is_param_call(n) and len(n.args) == 1
        and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
    }


def compile_measure(
    text: str,
    schema: Optional["pl.Schema"],
    *,
    alias: str,
    partition_by: Optional[list] = None,
    order_by: Optional[str] = None,
    parameter_values: Optional[dict] = None,
) -> pl.Expr:
    """Parse -> allowlist/validate -> build polars.Expr. Never evaluates `text`.
    Raises MeasureCompileError (fail closed) on anything outside the allowlist.
    `schema=None` skips column/measure-existence checks (used only for
    model-yaml load-time structural validation, where no live schema is
    fetched). `partition_by`/`order_by` are consulted only for window
    measures (see is_window_expr) — the engine passes the query's actual
    dimensions once it knows them; left None for structural-only validation,
    which builds the bare running_total/lag reduction without `.over()`.
    `parameter_values` is a pre-resolved {name: int} for any param('name')
    reference inside lag()'s periods argument — see _resolve_periods_arg."""
    if len(text) > MAX_MEASURE_LEN:
        raise MeasureCompileError(f"measure text exceeds {MAX_MEASURE_LEN} character limit", kind="limit_exceeded")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise MeasureCompileError(f"invalid syntax: {exc}", kind="disallowed") from exc
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_NODES:
        raise MeasureCompileError(f"expression exceeds {MAX_NODES} node limit", kind="limit_exceeded")
    window = is_window_expr(text)
    compiler = _Compiler(
        schema, window=window, partition_by=partition_by, order_by=order_by, parameter_values=parameter_values
    )
    expr = compiler.build(tree.body, depth=0)
    return expr.alias(alias)
