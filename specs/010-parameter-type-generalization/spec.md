# Feature Specification: Generalize Visual Parameters to More Types and DSL Positions

**Feature Branch**: `claude/visual-parameter-declarations-6mztd3`

**Created**: 2026-07-12

**Status**: Draft

**Input**: User description: "Generalize visual parameters (spec 009) to more data types and DSL positions. Spec 009 shipped int-only parameter values, usable only as lag()'s periods argument. This feature: (1) adds a `type` field to a parameter declaration — int, float, or string (not date — the DSL has no date literal/cast yet, deferred) — with existing untyped parameters treated as int for backward compatibility; (2) makes param('name') usable anywhere a literal is currently legal in the measure DSL (comparisons, if_(), coalesce(), where(), cast()'s value argument), not only lag()'s periods argument, while lag()'s existing positive-integer requirement stays enforced against whatever resolves there. Wiring parameters into dashboard/visual filters stays explicitly out of scope — a separate subsystem and a separate feature."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Declare a typed parameter and use it in a comparison or conditional (Priority: P1)

A dashboard developer declares a parameter with a type — for example a float parameter named `threshold` with values `[10, 25, 50, 100]` and default `25` — and writes a measure that references it somewhere other than `lag()`'s periods argument, e.g. `if_(revenue > param('threshold'), revenue, 0)` or `where(revenue, revenue > param('threshold'))`. The measure saves and compiles correctly, using the declared default when no selection has been made.

**Why this priority**: This is the entire point of the feature — without it, a non-integer parameter has nowhere valid to be used, and the feature delivers no new capability. This is the direct, minimal proof that both the new type and the new DSL positions work together.

**Independent Test**: On a single visual, declare a `float` parameter and save a measure using `param('name')` inside a comparison (not inside `lag()`); confirm it saves, compiles, and a query using the default value returns the expected result.

**Acceptance Scenarios**:

1. **Given** a visual with no declared parameters, **When** the developer declares a parameter with an explicit type (`int`, `float`, or `string`), a values list whose members all match that type, and a default drawn from that list, **Then** the parameter is saved as part of the visual.
2. **Given** a visual with a declared `float` parameter, **When** the developer writes a measure using `param('name')` inside a comparison, `if_()`, `coalesce()`, or `where()`, **Then** the measure compiles and saves successfully.
3. **Given** a visual with a declared `string` parameter, **When** the developer writes a measure comparing a column/measure to `param('name')` (e.g. matching against a category label), **Then** the measure compiles and, when queried with no explicit selection, uses the declared default.
4. **Given** a parameter declaration, **When** the developer includes a value in its `values` list that does not match the declared type (e.g. a string in an `int`-typed list), **Then** the declaration is rejected with a clear error.
5. **Given** a `cast()` call, **When** the developer tries to use `param('name')` as `cast()`'s *type-name* argument (rather than its value argument), **Then** it is rejected — the type-name argument stays a fixed literal such as `"int"`/`"float"`/`"str"`/`"bool"`, never a parameter.

---

### User Story 2 - Existing int/lag() parameters keep working unchanged (Priority: P1)

A visual saved before this feature existed has a parameter with no declared type and a measure using it as `lag()`'s periods argument, exactly as spec 009 shipped it. After this feature ships, that visual, its measure, and any dashboard views built on it continue to load, query, and display exactly as before, with no developer action required.

**Why this priority**: Nothing about this feature is allowed to regress the feature it's extending. This must hold before any new capability is trusted.

**Independent Test**: Re-run every existing automated test and manual scenario from spec 009's quickstart against the post-change system; all must pass unchanged, and a pre-existing saved visual with an untyped parameter must open and query identically.

**Acceptance Scenarios**:

1. **Given** a parameter declaration saved with no `type` field, **When** it is loaded, validated, or queried, **Then** it is treated in every respect as though it were declared `type: int`.
2. **Given** a measure using `lag(x, param('name'))` where `name` is an untyped (implicitly `int`) parameter, **When** the measure is queried, **Then** it behaves exactly as it did under spec 009.
3. **Given** the full pre-existing automated test suite from before this feature, **When** it is run after this feature ships, **Then** every test still passes without modification to its expected behavior.

---

### User Story 3 - Mismatched parameter type is caught with a clear error (Priority: P2)

A developer declares a `string`-typed parameter and, by mistake or experimentation, tries to use it as `lag()`'s periods argument (which requires a positive integer). The system rejects this clearly at the point the measure is authored or queried, the same way a literal string would already be rejected there — never silently coercing the string, never producing an obscure downstream engine error.

**Why this priority**: Without this, generalizing to more types creates a new, confusing failure mode (a parameter that "sometimes" works depending on incidental value contents) instead of a clean capability. It's a correctness guard on Stories 1 and 2, not new end-user capability on its own.

**Independent Test**: Declare a `string` parameter, write `lag(revenue, param('string_param'))`, and confirm the save/compile is rejected with an error naming the type mismatch — independent of any dashboard or sharing behavior.

**Acceptance Scenarios**:

1. **Given** a `string`-typed parameter, **When** a measure uses it as `lag()`'s periods argument, **Then** the save/compile is rejected with an error explaining the position requires an integer.
2. **Given** a `float`-typed parameter whose declared values happen to all be whole numbers (e.g. `[1.0, 2.0, 3.0]`), **When** it's used as `lag()`'s periods argument, **Then** it is still rejected — the declared type governs eligibility, not whether a particular value would numerically work.
3. **Given** an `int`-typed parameter used as `lag()`'s periods argument, **When** its currently-resolved value is not a positive integer (e.g. its default is `0`), **Then** it is rejected exactly as a literal `0` already is today — this existing rule is unchanged by the type generalization.

---

### User Story 4 - Dashboard sharing/conflict detection accounts for parameter type (Priority: P2)

Two visuals on the same dashboard each declare a same-named parameter. If one declares it `int` and the other `string` (even if their value lists happen to look similar, e.g. `[1,2,3]` vs `["1","2","3"]`), the dashboard treats this as a conflicting definition — exactly like a values or default mismatch already is — and refuses to let both visuals coexist, rather than silently merging or arbitrarily picking one visual's type.

**Why this priority**: Extends spec 009's already-shipped sharing/conflict mechanism to the new dimension (type) the feature introduces; valuable but strictly dependent on Stories 1-2 existing first.

**Independent Test**: Declare a same-named parameter on two visuals with different types (or the same type but different values/default, to confirm the pre-existing check is untouched), attempt to add both to one dashboard, and confirm the conflict is blocked with an error naming the parameter.

**Acceptance Scenarios**:

1. **Given** two visuals declaring a same-named parameter with different types, **When** an attempt is made to add both to one dashboard, **Then** the action is blocked with an error identifying the conflicting parameter.
2. **Given** two visuals declaring a same-named parameter with the same type, same values, and same default, **When** both are added to one dashboard, **Then** they are treated as identical and share one control, exactly as spec 009 already provides.
3. **Given** a dashboard saved before this feature (all parameters implicitly `int`), **When** it is opened after this feature ships, **Then** its existing sharing/conflict state is unchanged (an implicit `int` on both sides still compares equal to another implicit/explicit `int`).

---

### User Story 5 - Viewers get a type-appropriate control (Priority: P3)

A viewer opens a visual or dashboard with a `string`-typed parameter and sees its declared text options to choose from (not numbers); a visual with a `float`-typed parameter lets them pick a decimal value from its declared list. Selecting a value re-runs the query exactly as an `int` parameter's toggle already does.

**Why this priority**: Rounds out the feature with the same viewer-facing polish spec 009 already has for `int` — necessary for the feature to feel complete, but the underlying capability (Stories 1-3) is meaningful even before this UI polish lands.

**Independent Test**: Open a visual with a declared `string` parameter and confirm the control shows its declared text values (not attempting numeric parsing/sorting), and that selecting one re-runs the query using that value.

**Acceptance Scenarios**:

1. **Given** a visual with a declared `string` parameter, **When** the viewer opens it, **Then** the control shows the parameter's declared string values as selectable text, defaulting to the declared default.
2. **Given** a visual with a declared `float` parameter, **When** the viewer opens it, **Then** the control shows the parameter's declared decimal values, and selecting one re-runs the query with that value.
3. **Given** a viewer's selection, **When** the value is sent with the query, **Then** it is validated against the parameter's declared value list exactly as an `int` parameter's selection already is (FR-010, unchanged mechanism, now type-aware).

### Edge Cases

- A parameter's `values` list contains a value that "looks" convertible but doesn't match the declared type exactly (e.g. the string `"1"` in an `int`-typed list): rejected as a type mismatch, never silently coerced.
- A `string`-typed parameter's value contains characters that resemble DSL syntax or SQL-injection-style content (e.g. quotes, semicolons): remains completely inert — it is only ever compared for allowlist membership and inserted as a literal constant, never interpreted as code or syntax, exactly like any other string constant the DSL already handles.
- `param('a') > param('b')`: two different parameter references inside one expression are both resolved independently through the same mechanism; no special restriction on referencing more than one parameter in an expression.
- A parameter declared with only one value in its type-appropriate list: still valid, matching spec 009's existing allowance for this.
- A visual saved under spec 009 (no `type` field) is opened and edited after this feature ships: it displays and behaves as an `int` parameter without requiring the developer to do anything to "upgrade" it.
- A developer declares a parameter's default using a value of the wrong type for its own `values` list (e.g. `type: string`, `values: ["a","b"]`, `default: 1`): rejected the same way a default outside the values list already is, now also catching the type mismatch.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Developers MUST be able to declare a parameter's type as one of `int`, `float`, or `string`.
- **FR-002**: System MUST reject a parameter declaration whose `values` list contains any entry that does not match its declared type.
- **FR-003**: System MUST reject a parameter declaration whose `default` is not both a member of `values` and of the declared type (extends spec 009's FR-002 to also check type).
- **FR-004**: A parameter declaration with no explicit `type` MUST be treated as `type: int` in every validation, resolution, comparison, and sharing rule — preserving full backward compatibility with visuals and dashboards saved under spec 009.
- **FR-005**: Developers MUST be able to reference a declared parameter via `param('name')` anywhere a literal/constant is currently legal in the measure DSL — including but not limited to comparisons, `if_()`'s predicate and both branches, `coalesce()`'s arguments, `where()`'s predicate, and `cast()`'s value argument — not only `lag()`'s periods argument.
- **FR-006**: `param()` MUST remain illegal as `cast()`'s type-name argument; that argument stays a fixed literal string naming a dtype.
- **FR-007**: Wherever a `param()` reference is used in a DSL position with an inherent type requirement (e.g. `lag()`'s periods argument requires an integer), System MUST validate the referenced parameter's declared type is compatible with that position and reject with a clear error if it is not — a non-`int`-typed parameter used as `lag()`'s periods argument MUST be rejected the same way an incompatible literal already is there.
- **FR-008**: The pre-existing rule that `lag()`'s periods argument must resolve to a positive integer remains unchanged and applies identically whether the value came from a literal or from an `int`-typed parameter.
- **FR-009**: A measure referencing any parameter, in any DSL position, MUST remain saveable only as a visual-scoped (inline) measure; saving or promoting such a measure into the shared model measure library MUST be rejected (extends spec 009's FR-007, no longer limited to window-mode measures).
- **FR-010**: System MUST validate, for every query, that any parameter value supplied with the request both names a declared parameter and is a member of that parameter's declared value list under its declared type, rejecting the query without executing it if not (extends spec 009's FR-010 to be type-aware).
- **FR-011**: Viewers MUST be able to select a value for a `string`- or `float`-typed parameter via a control appropriate to its type, with the same toggle-and-rerun behavior `int`-typed parameters already provide.
- **FR-012**: Two parameter definitions are considered identical, for dashboard sharing/conflict purposes, only if their name, type, value list (as a set), and default all match exactly — extending spec 009's FR-014 so a type mismatch alone is sufficient to make two same-named parameters conflict.
- **FR-013**: System MUST reject a measure that references, via `param()`, a parameter name not declared on the same visual, with an error naming the unresolved parameter, regardless of which DSL position the reference appears in (extends spec 009's FR-006).
- **FR-014**: Wiring a parameter's value into a dashboard or visual *filter* remains explicitly out of scope for this feature; no such mechanism is added.

### Key Entities

- **Parameter (extended)**: As spec 009, plus a required `type` (`int` | `float` | `string`; absent on legacy data implies `int`). Its `values` list and `default` must all match this type.
- **Typed Value Set**: The invariant that a parameter's `values` list is homogeneous in its declared type — enforced at declaration time, not just at query time.
- **Parameterized Measure (extended)**: As spec 009, now referencing `param()` from any legal DSL position, not only inside `lag()`.
- **Shared Parameter / Parameter Conflict (extended)**: As spec 009; definition equality now includes `type` as a compared field.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A developer can declare a `float` or `string` parameter and use it in a comparison, `if_()`, `coalesce()`, or `where()` expression and see it work end-to-end (control appears, changing it changes displayed values) — the same experience already proven for `int` parameters in `lag()`.
- **SC-002**: 100% of parameter declarations with a type/value mismatch (a wrongly-typed entry in `values`, or a wrongly-typed `default`) are rejected at save time, before ever reaching a query.
- **SC-003**: 100% of `param()` references used in a position with an inherent type requirement that resolve to an incompatible parameter type are rejected with a clear error — none are silently coerced or passed through to the underlying query engine.
- **SC-004**: Every dashboard, visual, and measure saved before this feature shipped continues to load, query, and display identically afterward — zero regressions, verified by the full pre-existing automated test suite passing unchanged.
- **SC-005**: Attempting to share a same-named parameter with a mismatched type across two visuals on one dashboard is blocked with the same reliability and clarity spec 009 already provides for values/default mismatches.

## Assumptions

- **Date is deferred**: the measure DSL has no date literal or date cast target today; adding a `date` parameter type is a natural future extension once that groundwork exists, not part of this feature.
- **Filters stay out of scope**: letting a parameter's value drive a dashboard/visual filter (as opposed to a measure expression) is a distinct, larger feature touching a different subsystem — not built here.
- **No boolean type**: not requested and not added; `cast()`'s existing `"bool"` cast target is unrelated to parameter types.
- **No implicit cross-type coercion**: a parameter's declared type must exactly match what a DSL position requires (e.g. an `int`-typed parameter is never silently treated as satisfying a `float`-shaped position or vice versa) — keeps validation and error messages predictable rather than requiring a coercion matrix.
- **Backward compatibility is unconditional**: every parameter, measure, visual, and dashboard saved under spec 009 must continue to work with zero required migration or re-authoring, by treating an absent `type` as `int` everywhere.
- **Per-type UI affordances**: the declaration editor, viewer toggle, and measure-lab parameter picker will present input appropriate to each type (e.g. a delimited list editor for strings vs. a numeric list for int/float) — exact widget design is left to planning, not specified here.
