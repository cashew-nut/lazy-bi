# API Contract Changes

All existing endpoints keep their current request/response shape unless
listed below. No breaking changes — every addition is either a new endpoint
or an additive field on an existing response.

## New: dimension bundle endpoints (`app/api/dimensions.py`)

Mirrors the read/write shape of `app/api/models.py`, minus anything
measure-specific (bundles have no measures) and minus the in-app-editor-only
pieces the spec marks out of scope (no `/dimensions/validate` live-typing
endpoint for v1 — hand-authored YAML, per spec Assumptions).

| Route | Behavior |
|---|---|
| `GET /api/dimensions` | List loaded bundles: name, label, description, and each dataset's name + source + dimension count. Analogous to `GET /api/models`. |
| `POST /api/dimensions/reload` | Re-read `dimensions/*.yaml`, then re-resolve every model's imports against the fresh bundles (i.e. this also re-resolves `registry.models`, not just the bundles dict — see data-model.md's `Registry.reload_all()`). Analogous to `POST /api/models/reload`, but with the wider blast radius the dependency direction requires. |
| `GET /api/dimensions/{name}/yaml` | Raw YAML text of one bundle, for anyone hand-editing it out-of-band who wants to fetch/confirm current content. Analogous to `GET /api/models/{name}/yaml`. |
| `PUT /api/dimensions/{name}/yaml` | Validate + write a bundle's YAML, then reload (same cascade as `POST /api/dimensions/reload`). Analogous to `PUT /api/models/{name}/yaml`. |

**Deliberately not added in v1** (consistent with spec Assumptions — no
editor UI required): `POST /api/dimensions` (create-from-template), `DELETE
/api/dimensions/{name}`, a live-validation endpoint. These are natural
follow-ups if/when the model editor UI is extended to author bundles, not
needed for the CLI/hand-authored-YAML workflow this spec targets.

## Changed: `GET /api/models` and `Model.to_public()`

Adds one field, `imports`, listing each resolved import in summary form:

```jsonc
{
  "name": "sales",
  // ...unchanged existing fields...
  "imports": [
    {"bundle": "geography", "anchor_dataset": "regions", "datasets": null},
    // datasets: null means "whole bundle" (the default); otherwise the
    // explicit subset list from the model's YAML
  ]
}
```

Existing `dimensions` field is unchanged in shape — it already lists
`{name, label, type, description, spine, geo}` per dimension; imported
dimensions appear in this same list, indistinguishable from native ones
(spec FR-009), which is the whole point. Nothing currently parsing this
field needs to change.

## Changed: `GET /api/explorer`

The matcher-building step currently does:

```python
sources = [("source", m.source)] + [(f"join: {j.name}", j.source) for j in m.joins]
```

per model. Extended to also include, per model, every bundle dataset source
reachable through that model's resolved imports:

```python
sources = (
    [("source", m.source)]
    + [(f"join: {j.name}", j.source) for j in m.joins]
    + [(f"import: {binding.bundle.name}.{ds}", binding.bundle.datasets[ds].source)
       for binding in m.import_bindings for ds in binding.included_datasets]
)
```

Response shape is unchanged (`role` strings simply gain a new `"import:
{bundle}.{dataset}"` form alongside the existing `"source"` and `"join:
{name}"` forms) — this is the fix for the cross-cutting issue noted in
plan.md: without it, files backing a bundle dataset would show up as
`unmapped` in the Data explorer even though a fact model imports them,
which would directly contradict [004](../../004-studio-portal-data-explorer/spec.md)'s
FR-012 ("an object matching no model's glob MUST be visibly flagged as
unmapped") — bundle-sourced files **do** match a model's (transitive)
glob now, so they must not be flagged.

## Unchanged: `POST /api/query`, visuals, dashboards, publications

Imported dimensions are queried by name exactly like native ones — the
query request/response shape (`{model, dimensions, measures, filters, sort,
limit}` → `{columns, rows, row_count, elapsed_ms}`) needs no change, and
neither does anything persisted (visuals/dashboards reference dimension
*names*, which resolve the same way regardless of origin).
