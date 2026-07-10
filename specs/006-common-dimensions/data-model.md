# Data Model: Common Dimensional Models

Extends `app/semantic.py`. Existing entities (`Source`, `Join`, `Spine`, `Geo`,
`Dimension`, `Measure`, `Model`) are unchanged in shape; `Model` gains one new
field. Everything below is additive.

## New Entities

### Dataset

A single source + the dimensions it exposes, living inside a `DimensionBundle`
— the bundle-scoped equivalent of what a fact model is today, minus measures.

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Unique within its bundle (e.g. `accounts`, `opportunities`). |
| `source` | `Source` | Same as `Model.source` — reused as-is. |
| `dimensions` | `dict[str, Dimension]` | Same `Dimension` dataclass fact models already use — reused as-is, including `spine`/`geo`. |
| `joins` | `list[DatasetJoin]` | Joins from *this* dataset to *other datasets in the same bundle*. |

**Validation rules**:
- `name` must be unique within its bundle.
- `dimensions` follow the exact same validation as a `Model`'s dimensions today (unknown keys, spine requires `type: time`, etc.).
- A dataset MUST NOT declare measures — the YAML shape simply has no `measures:` key at this level (enforces spec FR-004 structurally, not just by convention).

### DatasetJoin

A join from one dataset to a sibling dataset in the same bundle. Structurally
identical to today's `Join`, except the right-hand side is a dataset *name*
(resolved within the bundle) instead of an inline `Source`.

| Field | Type | Notes |
|---|---|---|
| `to` | `str` | Name of the sibling dataset in the same bundle. |
| `left_on` / `right_on` | `list[str]` | Same shorthand rules as today's `Join` (`on:` sets both when the key is named the same on both sides; bare `on:` YAML-1.1 boolean quirk handled identically). |
| `how` | `str` | `left` (default) \| `inner` — same as today. |

**Validation rules**:
- `to` must reference another dataset declared in the same bundle (error names the bundle + the bad reference otherwise).
- The graph formed by all `DatasetJoin`s in a bundle MUST be acyclic (FR-011).
- A dataset's own dimension names, once merged with every dataset it can reach via `DatasetJoin`s, MUST NOT collide across datasets (FR-011) — checked at bundle load time, independent of which fact model eventually imports it.

### DimensionBundle

The top-level unit declared in a `dimensions/*.yaml` file.

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Globally unique (parallel namespace to `Model.name`). |
| `label` | `str` | Display name. |
| `description` | `str` | Optional. |
| `datasets` | `dict[str, Dataset]` | Keyed by `Dataset.name`. |
| `origin` | `Path` | The yaml file it was loaded from — same pattern as `Model.origin`, needed for hot-reload and any future editor. |

**Validation rules**: see `DatasetJoin` above (acyclic graph, no cross-dataset dimension-name collisions). A bundle with zero datasets is invalid.

### Import

Declared on a `Model`, records a fact model's reference to a bundle.

| Field | Type | Notes |
|---|---|---|
| `bundle` | `str` | Name of the `DimensionBundle` to import. |
| `anchor_dataset` | `str` | Which dataset in the bundle the fact model's own source connects to. |
| `left_on` / `right_on` | `list[str]` | Fact model's own column(s) → the anchor dataset's key column(s). Same shorthand (`on:`) as `Join`. |
| `how` | `str` | `left` (default) \| `inner`. |
| `datasets` | `Optional[list[str]]` | Explicit subset of bundle dataset names to include. `None` (absent in YAML) means "all datasets in the bundle" (FR-006's default). |

**Validation rules**:
- `bundle` must name a bundle that is currently loaded (checked at *resolution* time, not parse time — see below).
- `anchor_dataset` must exist in the target bundle.
- Every name in `datasets`, if given, must exist in the target bundle (FR-007, edge case in spec).
- The dimensions reachable from `anchor_dataset` (respecting `datasets` if given) MUST NOT collide by name with the fact model's own natively-declared dimensions **or** with dimensions from another `Import` on the same model — collisions are load-time errors (FR-011), except native-vs-imported, where native wins (FR-010) rather than erroring.

## Extended Entity

### Model (extended)

| Field | Type | Notes |
|---|---|---|
| `imports` | `list[Import]` | New. Defaults to empty — every existing model YAML is valid unchanged. |
| `dimensions` | `dict[str, Dimension]` | **Unchanged type.** After import resolution, this dict contains both natively-declared and imported dimensions, indistinguishable to every consumer downstream of `Model.dimension(name)` (FR-009). An `Dimension` gains no new field to mark its origin in the *public* shape; origin is tracked separately (see below) only where the engine needs it to build the join chain. |

**Why merge into the same dict rather than keep imports separate**: `engine.run_query`, the builder, `to_public()`, and the model editor's schema/completions all key off `model.dimension(name)` today. Keeping imported dimensions in a parallel structure would mean touching every one of those call sites to check "is it native or imported" — precisely the kind of duplicated plumbing constitution Principle I exists to avoid. A second, internal-only structure (see `ImportBinding` below) carries just enough to let `engine.scan()` build the right joins, without leaking into the public dimension surface.

### ImportBinding (internal, engine-facing only)

Not part of the YAML shape — computed once at import-resolution time (model
load / hot-reload), attached to the resolved `Model` for `engine.scan()` to
consume. Not exposed via `to_public()` beyond a simple `imports: [...]` summary
(bundle names + anchor) for API/debuggability.

| Field | Type | Notes |
|---|---|---|
| `import_spec` | `Import` | The originating declaration. |
| `bundle` | `DimensionBundle` | Resolved reference (not just the name). |
| `included_datasets` | `list[str]` | The resolved set after applying any subset — BFS-reachable datasets from `anchor_dataset`, intersected with `datasets` if given. |
| `dimension_owners` | `dict[str, str]` | Maps each imported dimension name → the dataset name it came from, so `engine.scan()` knows which joined-in columns a given dimension's `column` resolves against. |

## Resolution Flow (load / hot-reload time, not query time)

1. `load_dimension_bundles(dimensions_dir)` parses every `dimensions/*.yaml` into a `DimensionBundle`, validating each in isolation (acyclic joins, no intra-bundle collisions). Produces `dict[str, DimensionBundle]`.
2. `load_models(models_dir)` parses every `models/*.yaml` as today, *plus* its new `imports: list[Import]` (structurally parsed, not yet resolved — a model file must remain parseable even if referenced bundles aren't loaded yet, e.g. during isolated unit tests).
3. `resolve_imports(model, bundles) -> Model` — for each `Import` on the model: validate the bundle/anchor/subset exist, BFS the bundle's dataset graph from `anchor_dataset` (pruned to `datasets` if given), collect each reachable dataset's dimensions, check for collisions (native-wins, imported-imported errors), and merge the survivors into `model.dimensions`. Attaches the internal `ImportBinding`s used by `engine.scan()`.
4. `Registry.reload_all()` (renamed/extended from `reload_models()`) calls steps 1-3 in order: bundles first, then models, then resolve each model's imports against the freshly-loaded bundles. This is the hot-reload path FR-012 requires — editing a bundle and reloading re-resolves every importing model automatically, no changes to those models' own files.

## Query-Time Behavior (`engine.scan`)

`scan(model)` today: base source → apply each `model.joins` in order. Extended:
after `model.joins`, for each `ImportBinding` on the model, build the bundle's
combined lazy frame (its own datasets joined per the bundle's `DatasetJoin`s,
restricted to `included_datasets`, walked from `anchor_dataset`) and join that
single combined frame into the running lazy frame using the `Import`'s own
`left_on`/`right_on`/`how`. Every join added — inside the bundle and at the
anchor — is a plain lazy `.join()`, so pushdown behaves exactly as it does for
today's `model.joins` (Principle II, unchanged).
