# Feature Specification: Visual Parameters for Measures

**Feature Branch**: `claude/visual-parameter-declarations-6mztd3`

**Created**: 2026-07-12

**Status**: Draft

**Input**: User description: "As a dashboard developer, I want to declare a named parameter list on a visual, with an optional default value, so that measures on that visual can reference the parameter and viewers can toggle the measure's behavior without editing the visual. Example: declare `period_list = [1,2,3,4]` default `1`; a measure can be written as `lag(<measure>, param('period_list'))`; the viewer gets a control to pick among the declared values, which re-runs the query with the selected value. If a dashboard contains a visual with a parameter, the parameter should be saveable to a dashboard view. If two visuals share a parameter with the same definition, the dashboard should push the selection down to both; if the definitions differ, the dashboard should not allow both visuals together."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Declare a parameter and write a parameter-aware measure (Priority: P1)

A dashboard developer, editing a visual, declares a named parameter (e.g. `period_list`) with a fixed list of allowed values (`[1, 2, 3, 4]`) and a default (`1`). They then write a measure that references it, e.g. `lag(revenue, param('period_list'))`. The measure saves successfully to the visual and, when the visual is queried with no explicit selection, computes using the default value.

**Why this priority**: Without the ability to declare a parameter and reference it from a measure, nothing else in this feature exists. This is the authoring foundation everything else builds on.

**Independent Test**: On a single visual, declare a parameter and save a `lag(..., param('name'))` measure referencing it; confirm it saves and the query it produces uses the declared default.

**Acceptance Scenarios**:

1. **Given** a visual with no declared parameters, **When** the developer declares a parameter with a name, a non-empty list of values, and a default drawn from that list, **Then** the parameter is saved as part of the visual.
2. **Given** a visual with a declared parameter `period_list`, **When** the developer writes a measure `lag(revenue, param('period_list'))`, **Then** the measure compiles and saves successfully, scoped to that visual.
3. **Given** a visual with a declared parameter, **When** the developer tries to declare a default value that is not a member of the parameter's value list, **Then** the declaration is rejected with a clear error.
4. **Given** a visual with a declared parameter, **When** the developer writes a measure referencing a parameter name that is not declared on that visual, **Then** the measure is rejected with a clear error naming the unknown parameter.
5. **Given** a visual with a declared parameter, **When** the developer writes a measure that uses `param()` anywhere other than `lag()`'s second argument (e.g. inside a comparison or another function), **Then** the measure is rejected with a clear error.

---

### User Story 2 - Toggle a parameter while viewing a visual (Priority: P1)

A dashboard viewer looks at a visual whose measure references a parameter. They see a control (e.g. a dropdown) listing the parameter's declared values, initially showing the default. They pick a different value and the visual recomputes and redraws using that value, without editing the visual or writing any expression.

**Why this priority**: This is the actual end-user value of the feature — the whole point of declaring a parameter is that someone other than the developer can change it. It's testable independently of dashboards or sharing.

**Independent Test**: Open a visual with a parameter-referencing measure standalone (not on a dashboard), change the parameter control, and confirm the displayed values change to match the newly selected value.

**Acceptance Scenarios**:

1. **Given** a visual with a declared parameter and a measure referencing it, **When** the visual is first opened, **Then** the parameter control shows the declared default and the measure's displayed value matches a query run with that default.
2. **Given** a visual with its parameter control open, **When** the viewer selects a different declared value, **Then** the visual's query re-runs using the newly selected value and the display updates accordingly.
3. **Given** a query request that supplies a parameter value that is not a member of that parameter's declared list, **When** the query is submitted, **Then** the request is rejected and no value is substituted into the underlying query.

---

### User Story 3 - Save a parameter selection to a dashboard view (Priority: P2)

A dashboard developer places a visual with a declared parameter onto a dashboard, picks a non-default value on it, and saves the current state as a named dashboard view (the same mechanism already used to save filter selections). Reopening that view later restores the same parameter selection on the visual.

**Why this priority**: Extends the existing, already-relied-upon "saved view" concept to parameters — valuable but depends on Story 1 and 2 existing first.

**Independent Test**: On a dashboard with one parameterized visual, pick a non-default parameter value, save a named view, reload the dashboard, switch to that view, and confirm the visual shows the saved value rather than its default.

**Acceptance Scenarios**:

1. **Given** a dashboard tile backed by a visual with a declared parameter, **When** the developer selects a value and saves the dashboard's current state as a named view, **Then** the selected value is stored as part of that view.
2. **Given** a saved view containing a parameter selection, **When** a user opens the dashboard and switches to that view, **Then** the associated visual's parameter control reflects the saved value and its query uses that value.
3. **Given** a dashboard view was saved before a parameter existed on a visual, **When** the view is loaded after the parameter is added, **Then** the visual falls back to the parameter's declared default without error.

---

### User Story 4 - Two visuals share one parameter control (Priority: P2)

A dashboard has two visuals that each independently declare a parameter with the same name and the exact same definition (same values, same default). Instead of two separate controls, the dashboard shows one shared control; changing it updates both visuals' queries together.

**Why this priority**: Delivers the "push the selection down to both" half of the sharing behavior described in the story — a natural, valuable extension once single-visual parameters work, but not required for the feature to be useful on its own.

**Independent Test**: Build a dashboard with two visuals that each declare an identically-defined `period_list` parameter and reference it in a measure; confirm only one control appears on the dashboard and changing it re-runs both visuals' queries with the new value.

**Acceptance Scenarios**:

1. **Given** two visuals on the same dashboard, each declaring a parameter with the same name, same value list, and same default, **When** the dashboard is viewed, **Then** a single shared control is shown instead of two independent ones.
2. **Given** the shared control from the scenario above, **When** the viewer changes its value, **Then** both visuals' queries re-run using the new value.
3. **Given** the shared control, **When** the current selection is saved to a dashboard view, **Then** reloading that view restores the shared value to both visuals.

---

### User Story 5 - Dashboard blocks conflicting parameter definitions (Priority: P3)

A dashboard developer tries to add a second visual to a dashboard that already has a visual with a same-named parameter, but the two parameters' value lists (or defaults) differ. The dashboard refuses to allow both visuals together and clearly explains which parameter conflicts and how.

**Why this priority**: A safety/correctness guard rather than new capability — important so the feature never silently does the wrong thing, but it only matters once sharing (Story 4) already exists.

**Independent Test**: Attempt to add a visual declaring `period_list = [1,2,3]` default `1` to a dashboard that already has a visual declaring `period_list = [1,2,3,4]` default `1`; confirm the action is blocked with an error naming `period_list` and describing the mismatch.

**Acceptance Scenarios**:

1. **Given** a dashboard containing a visual with a declared parameter, **When** the developer tries to add a second visual whose same-named parameter has a different value list or default, **Then** the dashboard rejects the action with an error identifying the conflicting parameter and both visuals.
2. **Given** a dashboard save attempt (e.g. via direct edit of dashboard contents) that would result in two same-named, differently-defined parameters coexisting, **When** the save is submitted, **Then** it is rejected the same way as the add-tile case.
3. **Given** two visuals with a same-named, differently-defined parameter that are rejected from coexisting, **When** the developer renames the parameter on one of them so the names no longer match, **Then** both visuals are allowed on the dashboard, each keeping its own independent control.

### Edge Cases

- A visual declares two parameters with the same name: rejected as a duplicate declaration.
- A parameter's value list contains duplicate entries: the duplicates are ignored (treated as a set of allowed values), not an error.
- A measure references a parameter using `param()` correctly, but the visual's declared parameter is later deleted while the measure still references it: the measure is treated as invalid until the parameter is restored or the measure is fixed, and it fails clearly rather than silently defaulting.
- A developer attempts to promote a parameter-referencing measure from a visual into the shared model measure library: rejected, with an error explaining that parameterized measures are visual-only.
- A dashboard view was saved while two visuals shared an identical parameter definition; afterward, one visual's definition is edited so they diverge: the next time the dashboard is opened or saved, the conflict is surfaced rather than silently keeping the stale shared value.
- A visual's declared parameter has only one value in its list (control has nothing to toggle to): still valid, just not very useful; not an error.
- Removing one of two visuals that were sharing a pushed-down parameter: the remaining visual keeps working with its own independent control and its own saved/default value.
- In read-only portal (viewer) mode, a viewer changes a parameter value: the visual updates locally for that viewing session but the change is not written back to the saved dashboard, matching how filter/view changes already behave in portal mode today.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Developers MUST be able to declare one or more named parameters on a visual, each consisting of a name, a non-empty list of allowed values, and a default value.
- **FR-002**: System MUST reject a parameter declaration whose default value is not a member of its own value list.
- **FR-003**: System MUST reject a visual declaring two parameters with the same name.
- **FR-004**: Developers MUST be able to reference a declared parameter's current value from within a measure on the same visual via an explicit `param('name')` reference.
- **FR-005**: `param()` is valid, for this iteration, only as the second (`periods`) argument to the `lag()` measure function; System MUST reject `param()` used in any other position.
- **FR-006**: System MUST reject a measure that references, via `param()`, a parameter name not declared on the same visual, with an error naming the unresolved parameter.
- **FR-007**: A measure that references any parameter, directly or through a sibling measure it depends on, MUST be saveable only as a visual-scoped measure; System MUST reject any attempt to save or promote such a measure into the shared, model-level measure library.
- **FR-008**: Viewers MUST be able to select a value for a visual's declared parameter, from its declared list, via a control shown on the visual.
- **FR-009**: System MUST use a parameter's declared default value for any query where the viewer has not made an explicit selection.
- **FR-010**: System MUST validate, for every query, that any parameter value supplied with the request is a member of that parameter's declared value list, and MUST reject the query without executing it if the value is not a member.
- **FR-011**: Dashboard developers MUST be able to save the currently selected value(s) of a visual's parameter(s) as part of a named dashboard view, alongside the view's existing saved filters.
- **FR-012**: Loading a dashboard view MUST restore each parameter selection saved in that view to its associated visual(s), falling back to the declared default for any parameter the view has no saved value for.
- **FR-013**: When two or more visuals on the same dashboard declare a parameter with the same name and an identical definition, the system MUST treat them as one shared parameter: present a single control and apply the selected value to every visual sharing it.
- **FR-014**: Two parameter definitions are considered identical, for purposes of FR-013, only when their name, set of allowed values, and default value all match exactly; any difference in any of those makes them distinct.
- **FR-015**: When two visuals on the same dashboard declare a parameter with the same name but definitions that are not identical per FR-014, the system MUST prevent both visuals from coexisting on that dashboard, and MUST report which parameter conflicts and how.
- **FR-016**: The conflict check in FR-015 MUST be enforced both when a visual is added to a dashboard and when a dashboard's contents are saved.
- **FR-017**: A visual with no declared parameters, and a dashboard with no shared parameters, MUST behave exactly as they do today — this feature MUST NOT change existing filter/view behavior when parameters are not in use.

### Key Entities

- **Parameter**: Declared on a visual. Has a name (unique within that visual), an ordered list of allowed values, and a default value that is a member of that list.
- **Parameterized Measure**: A visual-scoped (inline) measure whose expression contains one or more `param()` references; ineligible for promotion to the shared model measure library.
- **Dashboard View (extended)**: The existing named, saveable dashboard state (today: a set of filters) gains a saved set of parameter selections, keyed by parameter name.
- **Shared Parameter**: The set of visuals on one dashboard whose same-named parameter declarations are identical per FR-014; they are presented and controlled as one.
- **Parameter Conflict**: The state where two visuals on the same dashboard declare a same-named but non-identical parameter; a blocking condition, never a silently-resolved one.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A dashboard developer can go from "no parameter" to "a working, viewer-toggleable measure" on a single visual using only the parameter declaration and measure-expression editing surfaces already used for authoring measures today — no separate tooling required.
- **SC-002**: 100% of query requests carrying a parameter value outside that parameter's declared list are rejected before reaching the query engine; none silently fall back to a substituted or default value.
- **SC-003**: On a dashboard where two visuals share an identical parameter definition, a single interaction (one control change) updates results on both visuals — the viewer never has to set the same logical value twice.
- **SC-004**: Attempting to combine two visuals with a same-named but differently-defined parameter on one dashboard is blocked every time, with the specific mismatch (which values or default differ) surfaced to the developer immediately, before any bad state is saved.
- **SC-005**: A dashboard view reproduces the exact parameter selections it was saved with, every time it is reopened, with the same reliability as the existing saved-filter behavior.

## Assumptions

- **v1 DSL surface**: `param()` is valid only as `lag()`'s `periods` argument — the only existing measure construct with a literal-integer argument a parameter could plausibly drive. `running_total()` (which takes no such argument today), aggregate-mode expressions, and other functions (`if_()`, `cast()`, comparisons) are out of scope until a concrete need for a parameterized value elsewhere is identified.
- **v1 value type**: parameter values are integers, matching the only current consumer (`lag()`'s shift count). String/float/date-typed parameters are a natural future extension but are out of scope here.
- **Authoring permissions**: parameters are declared and edited by whoever can already edit the visual; no new, separate permission tier is introduced for parameters beyond existing visual edit/save access. Promoting a measure to the shared model library remains gated by the existing authenticated model-measure-authoring flow, which parameterized measures simply cannot use.
- **Definition equality is by value, not identity**: two visuals that independently declare the same name/values/default are treated as identical even though they were authored separately and have no other link between them.
- **Persisted vs. ephemeral dashboard state**: parameters follow the same persisted, named-view mechanism already used for filters. Ephemeral, never-saved dashboard state (cross-filtering, focus mode) is unaffected and out of scope for parameter push-down.
- **Portal (read-only viewer) mode**: parameter selections behave like existing filter/view selections in portal mode — changeable locally for that viewing session, never written back to the saved dashboard.
