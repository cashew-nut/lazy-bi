# Data Model: Visual Parameters for Measures

No new SQLite tables or columns. Everything below lives inside the
existing `spec` JSON blob on `visuals` and the existing `items` JSON blob
(`views[]`) on `dashboards` — both are already free-form JSON columns that
tolerate additive shape changes without migration (see `app/store.py`).

## Parameter (declaration)

Declared inside a visual's `spec.query.parameters` list (sibling of the
existing `spec.query.inline_measures`).

| Field     | Type        | Rules |
|-----------|-------------|-------|
| `name`    | string      | Snake-case-ish identifier, unique within the visual's `parameters` list. Must be a legal DSL identifier (same `_check_identifier` rule already applied to measure/column names). |
| `values`  | list[int]   | Non-empty. Duplicates are de-duplicated on save (treated as a set), order otherwise preserved for control rendering. |
| `default` | int         | Must be a member of `values`. |

v1 only supports the `int` value type (see `research.md` §1 / spec
Assumptions) — there is no `type` field yet because there is exactly one
type; adding one is a forward-compatible, additive change if a second type
is ever needed.

**Validation**: enforced wherever a visual is saved (`POST/PUT
/api/visuals[/{id}]`) and wherever a query is run (`POST /api/query`) —
same "no separate migration, validated on every write/use" posture as
`inline_measures` today.

## Parameterized Measure

Not a new entity — an existing `inline_measures` entry (`{name, label,
expr, format}`) whose `expr` contains one or more `param('name')` calls.
Distinguished purely by structural inspection of `expr`
(`measure_dsl.referenced_parameter_names`), not by a new field.

**Constraint** (FR-007): `referenced_parameter_names(expr)` non-empty ⇒
this measure can only ever be saved via `POST/PUT /api/visuals[/{id}]`
(inside a visual's `spec.query.inline_measures`), never via `POST/PUT
/api/models/{name}/measures`.

## Query-time parameter selection

Travels with the query request itself (mirrors `inline_measures` — see
`research.md` §4), not looked up server-side:

```
query.parameters: [{name, values, default}]   # the declarations, resent each call
query.parameter_values: {name: int}            # the caller's current picks; missing name -> default
```

The engine reduces these two into one `resolved: dict[str, int]` per
query (every declared name present, each value either the caller's
validated pick or the declared default), which is the only thing handed
to `measure_dsl.compile_measure(..., parameter_values=resolved)`.

**Validation** (FR-010, enforced in `app/engine.py` before any measure
compiles):
- Every name in `parameter_values` must appear in `parameters`.
- Every value in `parameter_values` must be a member of that parameter's
  `values`.
- Any violation rejects the whole query (400) — nothing partially runs.

## Dashboard View (extended)

Existing shape, `app/store.py` / `dashboards.items` JSON:

```
{
  "items": [{"visual_id": int, "w": 1|2}],
  "views": [
    {
      "name": str,
      "filters": [...],                 # unchanged
      "parameters": {name: int}         # NEW — same shape as parameter_values
    }
  ],
  "active_view": int
}
```

`parameters` is optional per view; a view with no key (all pre-existing
saved dashboards) behaves as `{}` — every parameter on every tile falls
back to its own declared default, identical to today's no-parameter
behavior (FR-017).

## Shared Parameter (derived, not stored)

Computed at render/save time by scanning every tile's visual's
`spec.query.parameters`, grouped by `name`:

- **Group of 1** (only one visual on the dashboard declares this name):
  ordinary, independent parameter — its own control, its own entry in
  `view.parameters`.
- **Group of 2+, all definitions identical** (`values` as a set + `default`
  all match — FR-014): a *shared* parameter. One control. One
  `view.parameters[name]` entry applied to every visual in the group.
- **Group of 2+, definitions differ**: a **Parameter Conflict** (see
  below) — never silently split or merged.

This is derived on the fly from the tiles currently on the dashboard; nothing
about "which visuals share a parameter" is itself persisted — only the
resulting selected value(s) are (in `view.parameters`), exactly as cross-
tile filter push-down already works via `dashDimUnion()`/`tileFilters()`.

## Parameter Conflict (validation state, not stored)

Raised, never persisted: two visuals on the same dashboard declare a
same-named parameter whose `values`/`default` don't match exactly.

- Blocks adding the second visual to the dashboard (tile-add time).
- Blocks saving the dashboard if it would otherwise result in this state
  (save time — covers direct API use and races the client can't see).
- Error payload names the parameter and both visuals, per FR-015/SC-004.
