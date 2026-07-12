# Research: Generalize Visual Parameters to More Types and DSL Positions

All decisions below came from reading the existing spec-009 implementation
(`app/measure_dsl.py`, `app/engine.py`, `app/api/{visuals,dashboards,models,query}.py`,
`app/static/js/{builder,dashboard,measurelab}.js`) rather than external
research — this is a self-contained generalization of an existing in-house
mechanism.

## 1. How `param()` becomes usable in every literal position at once

**Decision**: Register `param` as a normal entry in both `_FUNCTIONS` and
`_WINDOW_FUNCTIONS` (the two tables `_build_call` dispatches through),
mapped to a new `_fn_param(compiler, args, depth)` that resolves to
`pl.lit(value)` from `compiler.parameter_values`. Because `where()`,
`if_()`, `coalesce()`, `cast()`'s value argument, and every comparison
operand already recurse through `compiler.build()` on their arguments,
this one registration makes `param()` legal everywhere those already
accept a literal — no per-function special-casing needed.

**Rationale**: The DSL already has exactly this pattern for `col()` — a
function that produces a `pl.Expr` from something outside the raw
AST-literal grammar, usable anywhere `build()` recurses. Adding `param`
the same way is the smallest change that satisfies FR-005, and it's
impossible for it to accidentally work in a position nobody intended,
because every position it now reaches is a position that already called
`compiler.build()` on a sub-expression — there's no new recursion path to
audit, only a new leaf the existing ones can produce.

**Alternatives considered**: Special-casing `param()` recognition inside
each of `_fn_if_`, `_fn_coalesce`, `_fn_where`, `_build_compare`
individually (as spec 009 did for `_fn_lag` alone). Rejected — four to
five near-identical special cases would have to be added and kept in sync
by hand, exactly the kind of duplication the general-function-table
registration avoids for free.

## 2. Why `cast()`'s type-name argument needed no code change

**Decision**: No change. `_fn_cast`'s second argument is already extracted
via `_string_literal_arg`, which only accepts an `ast.Constant` string —
an `ast.Call` node (i.e. `param(...)`) is structurally rejected by that
same check that already exists for spec 008. FR-006 ("`param()` must stay
illegal as `cast()`'s type-name argument") is satisfied by a pre-existing
guard, not a new one.

**Rationale**: Worth recording explicitly so a future reader doesn't
wonder why `_fn_cast` was untouched in this feature's diff — it's not an
oversight, the existing string-literal-only extraction already enforces
the restriction FR-006 asks for.

## 3. `lag()`'s periods argument keeps a stricter check than everywhere else

**Decision**: `_resolve_periods_arg` stays a separate, bespoke helper
(not routed through the new general `_fn_param`) because `.shift(periods)`
needs a **raw Python `int`**, not a `pl.Expr` — this is the one DSL
position that was never really "a literal position accepting any
constant," it's a position requiring a concrete Python value at compile
time. It now shares a `_lookup_param(compiler, name)` helper with the
general `_fn_param` (same "does this name resolve, and to what" logic),
but layers its own check on top: the looked-up value must satisfy
`isinstance(value, int) and not isinstance(value, bool)`.

**Rationale**: This is also exactly how the type boundary gets enforced
(User Story 3 / SC-003) — a `float`-typed parameter's resolved value is
always a genuine Python `float` object by the time it reaches this check
(see research §5's coercion rule), so `isinstance(value, int)` correctly
rejects it even when its value is numerically whole (e.g. `2.0`). The
type check is "is this the right Python type," never "would this value
numerically work" — exactly what spec.md's User Story 3 asks for.

**Alternatives considered**: Routing `lag()`'s periods argument through
the general `_fn_param`/`build()` path and then unwrapping the resulting
`pl.Expr` back into a Python value. Rejected — `pl.Expr` has no clean,
safe "give me back the literal Python value" operation from outside a
lazy evaluation context; keeping the raw-AST-node bespoke path (as spec
009 already established) is simpler and was already proven to work.

## 4. Declared `type`, backward compatibility, and where it's checked

**Decision**: `type` is one more field on a parameter declaration
(`int` | `float` | `string`), read with `p.get("type") or "int"`
everywhere a declaration is consumed — never a required field, never a
migration. `resolve_parameter_values()` (engine.py) gains two new public
helpers reused across every layer that validates or compares a
parameter's `values`/`default`:

```python
PARAM_TYPES = {"int", "float", "string"}
def param_type_ok(value, type_name) -> bool: ...
def coerce_param_value(value, type_name): ...
```

**Rationale**: `resolve_parameter_values` is already the one place spec
009 put all allowlist-membership logic (per its own research.md §1 — the
DSL compiler never re-derives validation, it trusts a pre-resolved dict).
Keeping the type-check/coercion logic there too, and exporting it, means
`app/api/visuals.py` (visual-save-time declaration validation) and
`app/api/dashboards.py` (definition-equality for sharing/conflicts) reuse
the exact same rules the query path enforces — one implementation of "is
this value a legitimate member of this type," not three.

## 5. The JSON/JavaScript numeric-type wrinkle, and why it forces a coercion step

**Decision**: `param_type_ok(value, "float")` accepts a JSON value that
decoded as either Python `int` or `float` (excluding `bool`).
`param_type_ok(value, "int")` accepts **only** a genuine Python `int`
(excluding `bool`) — a JSON float is never a legitimate `int` value, even
if whole. Whatever passes `param_type_ok` is then run through
`coerce_param_value`, which for `type: "float"` always returns
`float(value)` — so by the time a value reaches the measure compiler (via
`parameter_values`), a `float`-typed parameter's value is *always* a
genuine Python `float` object, never an `int` that happens to be
numerically whole.

**Rationale**: JSON has one numeric literal grammar — `100` and `100.0`
are different JSON tokens, but a JavaScript number has no persistent
int/float identity at all: `JSON.stringify(100)` and
`JSON.stringify(100.0)` in a browser produce the *identical* string
`"100"`, because JavaScript's one numeric type collapses that distinction
before JSON serialization even happens. A frontend built to send "proper"
float-shaped JSON for a whole-number float value structurally cannot do
so. Rejecting whole-number JSON integers as invalid `float` values would
therefore make a legitimate use case (a float parameter whose default or
declared values happen to be round numbers, e.g. thresholds of `50`,
`100`, `150`) impossible to save from the actual UI — not a hypothetical
edge case, a routine one. Accepting both shapes for `float` and then
immediately coercing to a canonical Python `float` is what lets
`isinstance(value, int)` in `_resolve_periods_arg` (research §3) stay a
reliable type gate rather than an accident of incoming JSON shape.

**Alternatives considered**: Requiring the frontend to tag float values
some other way (e.g. always sending them as strings, `"100.0"`, and
parsing server-side). Rejected as needless indirection — coercing at the
one point values enter `resolve_parameter_values` is simpler and keeps
`QueryRequest.parameters`/`MeasureCheckIn.parameters` exactly as
JSON-shaped as spec 009 already made them (`list[dict]`, no new pydantic
sub-model).

## 6. Dashboard definition-equality (`sameParamDef`) now compares type

**Decision**: Both `app/api/dashboards.py::_same_param_def` (Python,
authoritative) and `app/static/js/dashboard.js::sameParamDef` (JS,
client-side UX) add a `type` comparison as the first check, short-
circuiting to "not identical" on a mismatch before comparing
values/default at all — extending FR-014 (spec 009) / FR-012 (this
feature) with the new dimension.

**Rationale**: Directly what User Story 4 asks for — an `int` parameter
named `x` and a `string` parameter also named `x` must never be treated
as shareable just because, say, both happen to have the string `"1"` and
the int `1` compare loosely equal somewhere. Comparing `type` first and
exactly (no coercion across types) closes that off structurally.

## 7. Frontend: per-type input parsing, not a heavier per-type widget

**Decision**: The parameter declaration editor (`builder.js`) gains a
`type` `<select>` per parameter row; the existing single comma-separated
text input for `values` stays, but its parsing dispatches on the selected
type (`parseInt`/`parseFloat`/trimmed-and-deduped string split). Changing
a parameter's type clears its `values`/`default` rather than attempting
to reinterpret already-entered values under the new type.

**Rationale**: Keeps the UI change proportionate to the feature — a
dedicated multi-row string-value editor (to support commas *inside* a
string value) would be a real usability improvement but is extra surface
this pass doesn't need; the limitation (no comma-containing string values
in v1's editor) is recorded in spec.md's Assumptions rather than silently
shipped. Clearing on type change avoids a worse failure mode: silently
carrying over `values: [1,2,3]` as "valid" `string` values after a type
switch, which would immediately fail the new type-check on next save.

## 8. `is_window_expr` is untouched

**Decision**: No change to `is_window_expr` — it still triggers window
mode purely on the presence of `running_total`/`lag`, exactly as before.
A measure using only `param()` in a comparison (e.g.
`revenue > param('threshold')`, no `lag`/`running_total` anywhere) stays
in **aggregate** mode.

**Rationale**: This is required, not incidental — User Story 1's own
examples (`if_(revenue > param('threshold'), revenue, 0)`) are
aggregate-mode measures. `param` needed to be added to `_FUNCTIONS` (the
aggregate-mode table) specifically because of this, not just
`_WINDOW_FUNCTIONS`.

## 9. One engine.py call site needed `parameter_values=` added

**Decision**: `engine.py`'s plain (non-window) inline-measure compile
call — `add_plain(nm, measure_dsl.compile_measure(text, schema,
alias=nm))` — gains `parameter_values=resolved_params`. Under spec 009
this call site never needed it (`param()` could only appear inside a
`lag()` call, which always forced window mode, which always went through
the *other* `compile_measure` call site that already had it). Under this
feature, `param()` can appear in a purely aggregate-mode measure, so this
call site needs the same resolved dict threaded in.

**Rationale**: Found by tracing every `compile_measure(...)` call site in
`engine.py` against the new "param() can appear in aggregate mode too"
fact — the model-measure call site (`meas.expr(schema)`, inside
`semantic.py`) and the model-measure-save validation call site
(`api/models.py::_validate_measure_body`) both correctly still pass no
`parameter_values`, because both are structurally guaranteed to never
contain a `param()` reference (model measures can't reference parameters
at all — FR-009). The Measure Lab's live-check endpoint
(`api/models.py::check_measure`) already passed `parameter_values`
unconditionally (not gated on window mode) when it was built for spec
009, so it needed no change — a case of the original implementation
already being more general than the feature it was built for required.
