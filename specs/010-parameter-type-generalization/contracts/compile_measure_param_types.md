# Contract: Generalized `param()` — a delta to spec 009's `compile_measure_param.md`

This is an additive/modifying delta to
`specs/009-visual-parameters/contracts/compile_measure_param.md`. Read that
first; only what changes is documented here.

## Signature — unchanged

```python
def compile_measure(
    text: str,
    schema: "polars.Schema" | None,
    *,
    alias: str,
    partition_by: list | None = None,
    order_by: str | None = None,
    parameter_values: dict[str, int | float | str] | None = None,   # widened type
) -> "polars.Expr": ...
```

Only the *type* of `parameter_values`'s values widens (was `dict[str,
int]`); the parameter itself, and every caller's obligation to have
already validated/coerced each value against its parameter's declared
type before building this dict, is unchanged from spec 009.

## Grammar delta

`param('name')` is no longer special-cased to one call site. It is now a
normal entry in both `_FUNCTIONS` and `_WINDOW_FUNCTIONS`:

```
atom := ... | "param" "(" STRING ")" | ...
```

usable anywhere `atom` already was — the right-hand side of a comparison,
either branch or the predicate of `if_()`, any argument of `coalesce()`,
either argument of `where()`, the *value* argument (not the type-name
argument) of `cast()`, either operand of a `BinOp`, and so on.

`lag()`'s periods argument keeps its own grammar rule, additive to the
general one above:

```
lag_periods := NUMBER | "param" "(" STRING ")"     # unchanged from spec 009
```

but the **resolution** behind `param(...)` in that one position now
additionally requires the resolved value to be a genuine Python `int`
(see table below) — this is new precision spec 009 didn't need to state
because every parameter was `int`-typed then.

`cast()`'s type-name argument (its second argument) is **not** part of
this grammar widening — it is still extracted via the pre-existing
`_string_literal_arg`, which structurally only accepts a bare string
constant. `param(...)` there still hits `_string_literal_arg`'s existing
"requires a string literal argument" rejection (`kind="disallowed"`), not
a new check — see research.md §2.

## `param()` general resolution (new — every position except `lag()`'s periods argument)

| Condition | Result |
|---|---|
| `name` resolves in `parameter_values` | `pl.lit(parameter_values[name])` — a literal of whatever Python type (`int`/`float`/`str`) the caller stored there |
| `name` absent from `parameter_values` (or `parameter_values` is `None`) | `MeasureCompileError("unknown parameter 'name'", kind="unknown_parameter")` — unchanged from spec 009 |
| `param()` called with != 1 arg, or a non-string-literal arg | `MeasureCompileError("param() takes exactly one string literal argument", kind="disallowed")` — unchanged from spec 009 |
| resolved value is not one of `_is_allowed_constant`'s types (`None`/`bool`/`int`/`float`/`str`) | `MeasureCompileError("parameter 'name' resolved to an unsupported value type", kind="disallowed")` — new defense-in-depth check; should be unreachable given `engine.py`'s contract, but the compiler doesn't blindly trust it |

## `lag()`'s periods argument — full delta table

Extends spec 009's table (`contracts/compile_measure_param.md`'s "`lag()`
behavior, full table") with the type dimension:

| `periods` argument | Behavior |
|---|---|
| literal integer (unchanged from spec 009) | unchanged |
| `param('name')`, resolves to a genuine Python `int` (not `bool`), value `>= 1` | resolves to that int — unchanged from spec 009 |
| `param('name')`, resolves to a genuine Python `int`, value `< 1` | rejected, same message as a literal `0`/negative — unchanged |
| `param('name')`, resolves to a Python `float` (even a whole one, e.g. `2.0`) | **new**: rejected — `MeasureCompileError("lag()'s periods argument must be a literal integer or an int-typed param('name')", kind="disallowed")` |
| `param('name')`, resolves to a Python `str` | **new**: rejected with the same message as the `float` case |
| `param('name')`, `name` not in `parameter_values` | rejected: `unknown_parameter` — unchanged |

The float/string rejection is deliberately based on the resolved value's
**Python type**, not its numeric content — a `float`-typed parameter
whose current value is `2.0` is rejected exactly the same as one whose
value is `2.5`, per spec.md User Story 3's explicit requirement that
declared type, not incidental value shape, governs eligibility.

## `param_type_ok` / `coerce_param_value` — new, in `app/engine.py`, public

```python
PARAM_TYPES = {"int", "float", "string"}

def param_type_ok(value, type_name: str) -> bool: ...
def coerce_param_value(value, type_name: str): ...
```

Not part of the DSL compiler's contract (they operate on raw JSON-decoded
values before anything reaches `measure_dsl.py`) — documented here
because every other contract in this feature (visual save validation,
dashboard definition-equality) depends on them being the single source of
truth for "is this value a legitimate, canonically-typed member of this
parameter's declared type." See `data-model.md` for their exact
semantics.

## Non-goals — reaffirmed and extended from spec 009

- No `date` parameter type (spec.md Assumptions — the DSL has no date
  literal or cast target; out of scope until that groundwork exists).
- No boolean parameter type (not requested).
- No implicit cross-type coercion at a DSL-position boundary beyond the
  one JSON-numeric-shape accommodation described in research.md §5 (an
  `int`-typed parameter is never treated as satisfying a position that
  needs a `str`, etc.).
- `param()` inside an `in`/`not in` literal list (e.g. `x in
  [param('a'), param('b')]`) is **not** part of this feature —
  `_build_literal_collection` still requires every element to be an
  `ast.Constant`, and this feature does not change that. Not in FR-005's
  enumerated position list; a natural future extension if a concrete need
  arises.
- Parameters driving dashboard/visual *filters* remain entirely out of
  scope (spec.md FR-014) — this contract only concerns
  `app/measure_dsl.py`.
