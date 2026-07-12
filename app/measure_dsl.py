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
"""
from __future__ import annotations

import ast
from typing import Literal, Optional

import polars as pl

MAX_MEASURE_LEN = 2000
MAX_NODES = 200
MAX_DEPTH = 30

ErrorKind = Literal["disallowed", "unknown_function", "unknown_column", "limit_exceeded"]


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
    def __init__(self, schema: "pl.Schema"):
        self.schema = schema

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
        if name not in self.schema:
            raise MeasureCompileError(f"unknown column '{name}'", kind="unknown_column")
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
        builder = _FUNCTIONS.get(name)
        if builder is None:
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
    if name not in compiler.schema:
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


def compile_measure(text: str, schema: "pl.Schema", *, alias: str) -> pl.Expr:
    """Parse -> allowlist/validate -> build polars.Expr. Never evaluates `text`.
    Raises MeasureCompileError (fail closed) on anything outside the allowlist."""
    if len(text) > MAX_MEASURE_LEN:
        raise MeasureCompileError(f"measure text exceeds {MAX_MEASURE_LEN} character limit", kind="limit_exceeded")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise MeasureCompileError(f"invalid syntax: {exc}", kind="disallowed") from exc
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_NODES:
        raise MeasureCompileError(f"expression exceeds {MAX_NODES} node limit", kind="limit_exceeded")
    compiler = _Compiler(schema)
    expr = compiler.build(tree.body, depth=0)
    return expr.alias(alias)
