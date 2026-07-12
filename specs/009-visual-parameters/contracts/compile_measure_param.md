# Contract: `param()` — a delta to `compile_measure` (spec 008's DSL contract)

This is an additive delta to `specs/008-safe-measure-compilation/contracts/compile_measure.md`.
Everything in that contract is unchanged; this document covers only what's new.

## Signature change

```python
def compile_measure(
    text: str,
    schema: "polars.Schema" | None,
    *,
    alias: str,
    partition_by: list | None = None,
    order_by: str | None = None,
    parameter_values: dict[str, int] | None = None,   # NEW
) -> "polars.Expr": ...
```

`parameter_values` is a plain dict of already-validated `{parameter_name:
int}`. The compiler never sees a parameter's declared `values` list or
`default` — only the one int the caller has already picked. Left `None`
for structural-only validation (mirrors `partition_by`/`order_by` being
`None` at load/save time) — any `param(...)` reference then fails with
`unknown_parameter` (see below), the same "can't fully validate without
context, but every construct is at least recognized" posture
`partition_by=None` already gives `running_total()`/`lag()`.

## Grammar delta

`lag()`'s second argument gains one more legal shape:

```
lag_periods := NUMBER                    # existing: literal integer
             | "param" "(" STRING ")"    # NEW: parameter reference
```

`param(...)` is **not** added to `atom`, `_FUNCTIONS`, or `_WINDOW_FUNCTIONS`
generally — it is recognized only while parsing `lag()`'s second argument,
nowhere else in the grammar. A `param(...)` call appearing anywhere else
(a bare expression, inside `if_()`/`cast()`/a comparison, as
`running_total()`'s argument, etc.) is not special-cased and falls through
to the ordinary `Call` rule, which does not find `param` in either
function table and raises the pre-existing `unknown function 'param'`
(`unknown_function`) — no new code path, the omission from both tables
*is* the scope restriction.

## New `ErrorKind`

`"unknown_parameter"` — added to the existing `disallowed |
unknown_function | unknown_column | limit_exceeded` literal. Raised when
`param('name')` appears in the one legal position (`lag()`'s second
argument) but `name` is not a key in `parameter_values` (covers: the
parameter genuinely isn't declared on the visual, and the defensive case
of a caller passing an incomplete `parameter_values`).

## `lag()` behavior, full table

| `periods` argument | Behavior |
|---|---|
| omitted | defaults to `1`, unchanged from today |
| literal integer `< 1` | rejected, unchanged from today (`"lag()'s periods argument must be a positive integer"`) |
| literal integer `>= 1` | unchanged from today |
| `param('name')`, `name` in `parameter_values`, value `>= 1` | resolves to `parameter_values[name]` |
| `param('name')`, `name` in `parameter_values`, value `< 1` | rejected, same message as the literal case — a parameter is not a bypass for the positivity rule |
| `param('name')`, `name` not in `parameter_values` | rejected: `MeasureCompileError("unknown parameter 'name'", kind="unknown_parameter")` |
| `param()` with != 1 arg, or a non-string-literal arg | rejected: `MeasureCompileError("param() takes exactly one string literal argument", kind="disallowed")` |
| `param(...)` anywhere other than `lag()`'s 2nd argument | rejected: `MeasureCompileError("unknown function 'param'", kind="unknown_function")` (pre-existing generic path, not new) |
| any other shape (e.g. a name, a binop) | rejected, unchanged from today (`"lag()'s periods argument must be a literal integer"` — message extended to mention `param(...)` as the other legal form) |

`running_total()` is unchanged — it takes no arguments today and this
feature does not add one.

## New structural-only helper

```python
def referenced_parameter_names(text: str) -> set[str]:
    """Bare names passed to param(...) anywhere in `text` (not just inside a
    legal lag() position — this is used to detect and reject the construct
    where it's out of scope, so it must see it everywhere it appears).
    Never evaluates `text`; parses only, same posture as referenced_names()
    and is_window_expr()."""
```

Used by `app/api/models.py`'s `_validate_measure_body()` to reject any
attempt to save a `param()`-referencing measure to the shared model
library (FR-007), before any other validation runs.

## Non-goals (unchanged from spec 008's contract, reaffirmed here)

- `param()` is not a general-purpose literal substitution mechanism in
  this iteration — it has exactly one legal call site.
- No new value types — `parameter_values` is `dict[str, int]` only.
- No change to `running_total()`, `if_()`, `coalesce()`, `cast()`, or any
  aggregate-mode function.
