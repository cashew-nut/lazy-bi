# Contract: `compile_measure` (Tier 2 DSL compiler)

## Signature

```python
class MeasureCompileError(ValueError): ...

def compile_measure(text: str, schema: "polars.Schema", *, alias: str) -> "polars.Expr":
    """Parse `text` to an AST (mode="eval") and build a polars.Expr by walking
    it against a strict allowlist. Never evaluates, execs, or compiles `text`
    as Python. Raises MeasureCompileError (fail closed) on anything outside
    the allowlist, an unknown column, or a size/depth-limit violation."""
```

## Grammar (informal EBNF)

```
expr        := or_expr
or_expr     := and_expr ( "or" and_expr )*
and_expr    := not_expr ( "and" not_expr )*
not_expr    := "not" not_expr | compare
compare     := arith ( ("==" | "!=" | "<" | "<=" | ">" | ">=" | "in" | "not in") arith )*
arith       := term ( ("+" | "-") term )*
term        := factor ( ("*" | "/" | "%" | "**") factor )*
factor      := ("+" | "-") factor | atom
atom        := NUMBER | STRING | "True" | "False" | "None"
             | NAME                          # bare column reference
             | NAME "(" [args] ")"           # allowlisted function call only
             | "(" expr ")"
args        := expr ("," expr)*
```

## Node allowlist

| AST node | Allowed | Notes |
|---|---|---|
| `Expression` | yes | root only |
| `Constant` | yes | `int, float, str, bool, None` only — reject `bytes`, `complex`, `Ellipsis` |
| `Name` | yes | bare identifier → `pl.col(name)`; reject any name starting/ending with `_` or containing `__` |
| `BinOp` | yes | `Add, Sub, Mult, Div, Mod, Pow` — `Pow` exponent must itself be a `Constant` int/float within `[-8, 8]` |
| `UnaryOp` | yes | `UAdd, USub, Not` |
| `Compare` | yes | `Eq, NotEq, Lt, LtE, Gt, GtE, In, NotIn`; single comparison per node (reject chained `a < b < c`, which Python represents as multiple ops in one `Compare` — require exactly one `ops`/`comparators` pair) |
| `BoolOp` | yes | `And, Or` |
| `Call` | yes, restricted | callee MUST be a bare `ast.Name` present in the function table; reject calls where the callee is anything else (`Attribute`, another `Call`, a subscript, etc.) |
| everything else | **no** | `Attribute, Subscript, Lambda, ListComp, SetComp, DictComp, GeneratorExp, Starred, IfExp, JoinedStr, FormattedValue, Await, Yield, YieldFrom, NamedExpr, Assign, AugAssign, AnnAssign, List, Tuple, Dict, Set, Slice` — all rejected outright |

## Function table

| Name | Arity | Builds |
|---|---|---|
| `sum, mean, min, max, count_distinct, median, std, var, first, last` | 1 | `_expr(arg).<method>()` (`count_distinct` → `.n_unique()`) |
| `count` | 0 or 1 | 0-arg → row count (`pl.len()`); 1-arg → `_expr(arg).count()` |
| `col` | 1 (string literal only) | `pl.col(literal)` — explicit form; must be a `Constant[str]`, not a nested expr |
| `where` | 2 | `_expr(value).filter(_expr(predicate))` |
| `if_` | 3 | `pl.when(_expr(pred)).then(_expr(a)).otherwise(_expr(b))` |
| `coalesce` | 1+ | `pl.coalesce([_expr(a) for a in args])` |
| `cast` | 2 (2nd arg string literal from a fixed dtype allowlist: `"int", "float", "str", "bool"`) | `_expr(x).cast(<mapped dtype>)` |

No function accepts a Python callable, performs I/O, or is a `map_elements`/`map_batches`/`apply`/`scan_*`/`read_*`/`write_*` equivalent — none of those names are in the table, and the `Call` rule above means nothing outside this exact table is ever reachable regardless of name.

## Guards (checked before/during the walk, in this order)

1. `len(text) <= MAX_MEASURE_LEN` (2000 chars) — else `MeasureCompileError("measure text exceeds Nchar limit")`.
2. `ast.parse(text, mode="eval")` — a `SyntaxError` is caught and re-raised as `MeasureCompileError`, never propagated as a raw `SyntaxError`.
3. Total node count via one `ast.walk` pass `<= MAX_NODES` (200) — else reject.
4. Max nesting depth (tracked during the allowlist walk) `<= MAX_DEPTH` (30) — else reject.
5. Per-node allowlist checks as above, column-existence checks against `schema` for every `Name`/`col(...)` resolved.

Every rejection raises `MeasureCompileError` with a message distinguishing "disallowed construct" (security-relevant) from "unknown column"/"unsupported function name" (DX-relevant) per FR-014 — e.g. prefix security rejections with a stable marker the caller can pattern-match on if it ever wants to log them distinctly, without over-engineering a taxonomy.

## Column resolution

Every bare `Name` and every `col("literal")` first argument is looked up in `schema` (a `polars.Schema`, i.e. an ordered mapping of column name → dtype, as already returned by `LazyFrame.collect_schema()` elsewhere in this codebase). Unknown → `MeasureCompileError(f"unknown column '{name}'")`.

## Non-goals (explicitly out of scope for this contract)

- No group-by/window/multi-row reshaping (that's the `frame` carve-out, a separate mechanism — see research.md R2).
- No string/date helper functions in v1 (matches the original brief's "optional v1.1" note) — can be added to the function table later without changing the grammar or the node allowlist.
