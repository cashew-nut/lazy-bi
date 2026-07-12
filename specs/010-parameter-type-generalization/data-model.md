# Data Model: Generalize Visual Parameters to More Types and DSL Positions

No new SQLite tables or columns, and no change to the free-form JSON
shapes spec 009 already introduced (`visuals.spec`, `dashboards.items`) —
every entity below is the same entity spec 009 defined, extended with a
`type` field. See `specs/009-visual-parameters/data-model.md` for what's
unchanged and not repeated here.

## Parameter (extended)

Declared inside a visual's `spec.query.parameters` list, same location as
spec 009.

| Field     | Type              | Rules |
|-----------|-------------------|-------|
| `name`    | string            | Unchanged from spec 009. |
| `type`    | `"int"` \| `"float"` \| `"string"` | **New.** Optional; absent means `"int"` everywhere it's read (FR-004). Any other value is rejected. |
| `values`  | list, homogeneous in `type` | Non-empty (unchanged). Every member must satisfy `param_type_ok(member, type)` — see below. |
| `default` | one value, of `type` | Must satisfy `param_type_ok(default, type)` **and** be a member of `values` under the same type-aware comparison (not raw `in`). |

### Type membership and coercion (`app/engine.py`)

```python
PARAM_TYPES = {"int", "float", "string"}

def param_type_ok(value, type_name) -> bool:
    # int:    isinstance(value, int) and not isinstance(value, bool)
    # float:  isinstance(value, (int, float)) and not isinstance(value, bool)
    #         (accepts a JSON integer as a legitimate float — see research.md §5)
    # string: isinstance(value, str)

def coerce_param_value(value, type_name):
    # float:  float(value)   — canonicalizes a JSON-integer-shaped float value
    # int, string: value unchanged (already the correct concrete Python type
    #              once param_type_ok has passed)
```

Every declared `values` entry and `default` is coerced through
`coerce_param_value` before being stored in any resolved/comparison
structure — nothing downstream (the DSL compiler, dashboard
definition-equality) ever sees a `float`-typed value that is secretly a
Python `int`, or vice versa.

## Query-time parameter selection (extended)

Unchanged shape from spec 009 — `query.parameters` (declarations) /
`query.parameter_values` (the caller's picks) travel with the query
exactly as before. The values inside them are simply no longer
assumed-`int`; `resolve_parameter_values()` validates and coerces per
each declared parameter's own `type`.

```
resolved: dict[str, int | float | str]   # was dict[str, int] under spec 009
```

This `resolved` dict is now threaded into **every** `compile_measure()`
call for a measure that might reference `param()` — both the window-
measure call site (already true under spec 009) and the plain aggregate-
mode inline-measure call site (new — see research.md §9). Model-measure
compilation never receives it, and never needs to, because a model
measure can never contain `param()` (FR-009, unchanged from spec 009).

## Parameterized Measure (extended)

As spec 009: a visual-scoped (inline) measure whose expression contains
one or more `param(...)` calls, ineligible for promotion to the shared
model measure library. The only change is *where* those calls may
legally appear — anywhere `measure_dsl._Compiler.build()` recurses
(comparisons, `if_()`, `coalesce()`, `where()`, `cast()`'s value
argument, and `lag()`'s periods argument with its extra int-genuineness
check) — not only inside `lag()`.

`referenced_parameter_names()` (the structural scan used to block
model-measure promotion) needed no change — it already scans the whole
expression tree for any `param(...)` call site, not just ones inside
`lag()`.

## Shared Parameter / Parameter Conflict (extended)

As spec 009 (`data-model.md`'s "Shared Parameter"/"Parameter Conflict"
sections), with FR-014's identity check extended:

> Two parameter definitions are identical only if their **name, type,
> value list (as a type-aware-compared set), and default** all match
> exactly.

`type` is compared first and exactly (no cross-type coercion — an `int`
parameter and a `string` parameter are never identical regardless of
their values' contents); `values`/`default` are then compared using the
same `coerce_param_value`-normalized comparison the declaration-time
validation already uses, so two visuals whose float parameter values
arrived via slightly different JSON shapes (e.g. `[1, 2, 3]` vs.
`[1.0, 2.0, 3.0]`) are still correctly recognized as identical.
