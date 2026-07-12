# Research: Visual Parameters for Measures

All unknowns below were resolved by reading the existing implementation
(`app/measure_dsl.py`, `app/engine.py`, `app/store.py`, `app/static/js/{builder,measurelab,dashboard}.js`)
rather than by external research — this is a self-contained extension of an
existing in-house DSL and dashboard, not a new integration.

## 1. Where does `param()` get resolved: the DSL compiler or the engine?

**Decision**: The DSL compiler (`measure_dsl.py`) only ever sees a
pre-resolved `dict[str, int]` (`parameter_values`) mapping declared
parameter name → the one already-validated int to substitute. It does not
know about a parameter's allowed-values list or default — it just does a
dict lookup, exactly the way `partition_by`/`order_by` are handed to it
today. All "is this a legal value for this parameter" validation happens
one layer up, in `app/engine.py` (for real queries) and
`app/api/models.py` (for the Measure Lab's live-check endpoint), where the
parameter *declarations* (name + values + default) naturally live.

**Rationale**: Matches the codebase's existing division of labor exactly —
`compile_measure()` already takes `partition_by`/`order_by` as pre-resolved
values the engine computed from query context, not raw request data it
re-derives itself. Keeping the DSL compiler's inputs to "a name resolves to
an int, full stop" keeps the allowlisting surface (Constitution Principle
VI) unchanged: nothing new is ever eval'd, and the compiler still can't be
handed an unvalidated value because the caller is required to have already
filtered it through the declared list before building the dict.

**Alternatives considered**: Passing the full parameter declarations (with
values lists) into the compiler and validating there. Rejected — it would
duplicate validation logic between the Measure Lab's structural-only check
(no query context) and the engine's live query path, and it blurs the
compiler's job (structure → `pl.Expr`) with request validation (is this
value allowed).

## 2. Syntax and scope enforcement for `param()`

**Decision**: `param('name')` is not registered in `_FUNCTIONS` or
`_WINDOW_FUNCTIONS` (the two allowlist tables `_build_call` dispatches
through). It is recognized only by special-casing inside `_fn_lag`'s
parsing of its own second argument (mirroring how `_int_literal` already
special-cases that same argument today). Any `param(...)` appearing
anywhere else in an expression falls through to `_build_call`'s normal
lookup, which doesn't find it in either table and raises
`unknown function 'param'` — the existing, generic "you called something
that isn't allowed here" error, for free.

**Rationale**: This structurally *cannot* let `param()` leak into other
positions — there's no separate enforcement pass to keep in sync with the
allowlist, the allowlist's absence of an entry *is* the enforcement.
Consistent with the module's existing "fail closed by construction, not by
remembering to check" design ethos (see its docstring and Constitution
Principle VI).

**Alternatives considered**: Adding `param` as a normal allowlisted
function usable anywhere, then rejecting it post-hoc with a walk over the
AST wherever it shouldn't appear. Rejected as strictly more moving parts
for a scope the spec (FR-005) deliberately keeps narrow for v1.

## 3. New `ErrorKind`

**Decision**: Add `"unknown_parameter"` to the `ErrorKind` literal in
`measure_dsl.py`, used when `param('name')` names a parameter absent from
the resolved `parameter_values` dict (covers both "the visual never
declared this parameter" — FR-006 — and, defensively, "the caller failed
to resolve every declared parameter before compiling").

**Rationale**: The existing kinds (`disallowed`, `unknown_function`,
`unknown_column`, `limit_exceeded`) are surfaced to the Measure Lab UI to
pick an icon/tone; conflating an unresolved parameter with "unknown
column" would be misleading in that UI (a window measure's schema is
sibling-measure names, not columns, and a parameter is neither).

## 4. Request/response shape for parameter declarations and selections

**Decision**: Parameters travel exactly like `inline_measures` do today —
as a self-contained list embedded in the query payload itself, not looked
up server-side by visual ID. `query.parameters: list[{name, values,
default}]` declares them; `query.parameter_values: dict[str, int]` carries
the caller's current selection (any name omitted falls back to that
parameter's declared default). The engine resolves the two into one
`resolved: dict[str, int]` per request, after validating every entry in
`parameter_values` is a declared name with an in-list value — before
compiling anything.

**Rationale**: `inline_measures` already establishes this exact pattern
(the client resends the full ad-hoc definition with every query, the
server never trusts a previously-saved copy) — reusing it means the
existing "which measures/columns exist" trust model extends to parameters
with no new concept for the query API to learn. It also sidesteps a
staleness class of bug: a visual's live-edited-but-unsaved parameter
declarations (e.g. mid-edit in the builder) work identically to a saved
one, exactly like inline measures do.

**Alternatives considered**: Looking parameters up server-side from the
saved visual by ID. Rejected — the query endpoint is visual-agnostic today
(it takes a `model` plus ad-hoc query shape, no `visual_id`), and
introducing that coupling would be a bigger, unrelated architecture change.

## 5. Dashboard view storage

**Decision**: Extend each entry in the existing `views: [{name, filters}]`
list (stored in the `dashboards.items` JSON column, `app/store.py`) with a
`parameters: {name: value}` map, following the exact shape/precedent of
`filters`. No SQLite schema change — the column is already a JSON blob and
`_dash_to_dict` already passes unrecognized keys on a view through
unchanged (it only requires `"filters" in v`), so this is additive and
backward compatible: old saved dashboards simply have no `parameters` key
per view, treated as `{}`.

**Rationale**: This is literally the mechanism FR-011/FR-012 ask for
("saveable to a dashboard view... same as filters") — no new persistence
concept needed.

## 6. Cross-visual push-down and conflict detection

**Decision**: A new `dashParamUnion()` (parallel to the existing
`dashDimUnion()` in `dashboard.js`) scans every tile's visual's
`spec.query.parameters`, grouping declarations by name. For each name with
more than one declaring visual: if every declaration is byte-for-byte
identical (name, `values` as a set, `default`), it's one shared parameter —
rendered as a single control next to the existing filter bar, saved into
`view.parameters[name]`, and threaded into every one of those visuals'
`tileQuery()` output as `parameter_values`. If declarations differ, it's a
conflict (FR-015) and is never silently resolved.

The conflict check itself runs **before** either mutation completes: when
adding a tile (`+ ADD` on the dashboard) and when a dashboard save is
submitted — both client-side (immediate feedback, matching how other
dashboard actions behave) and server-side in `PUT/POST
/api/dashboards[/{id}]` (authoritative; catches direct API use and
concurrent edits the client-side check can't see).

**Rationale**: Reuses the `dashDimUnion`/`tileFilters` push-down pattern
readers of `dashboard.js` already know, rather than inventing a second
one. Enforcing the check twice (client for UX, server for correctness)
matches how this codebase already treats client-side validation as a
convenience layer over an authoritative server check (e.g. measure name
`snake_case` regex is checked in the Measure Lab UI *and* re-checked in
`add_measure`).

## 7. Blocking a parameterized measure from being promoted to a model measure

**Decision**: A new structural-only helper, `measure_dsl.
referenced_parameter_names(text) -> set[str]` (an AST walk for `param(...)`
call sites, in the same style as the existing `referenced_names()` and
`is_window_expr()` — parses only, never evaluates), is called at the very
top of `_validate_measure_body()` in `app/api/models.py`. If it returns
anything non-empty, the save is rejected with a 400 before any of the
existing schema/window logic runs. The Measure Lab's "save to model"
button is disabled client-side under the same condition, as a UX
convenience — the server check is what actually enforces FR-007.

**Rationale**: Model measures are validated and compiled with no visual in
scope at all (`_validate_measure_body` builds its schema from
`model.measures`/the live source scan, never a visual's declared
parameters) — there is no `parameter_values` to resolve against even if
we wanted to allow it, so rejecting early with a clear, specific error is
both correct and simpler than the alternative (letting it fall through to
a generic "unknown parameter" compile error, which would be misleading
here since the real problem is *scope*, not a naming mistake).
