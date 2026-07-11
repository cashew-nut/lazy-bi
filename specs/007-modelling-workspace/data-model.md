# Phase 1 Data Model: Modelling Workspace

This feature introduces **no new persisted entities**. The semantic layer's existing dataclasses (`app/semantic.py`) remain the contract; model/bundle YAML files remain the only persisted store. What follows is (a) the one new *transient, read-only* shape the dataset endpoint returns, and (b) the frontend state additions, with their ephemeral-vs-persisted classification per Constitution V.

## New transient shapes (read-only, not persisted)

### Dataset (a grouped bucket location the picker offers)

Derived on each `GET /api/datasets` call from a bucket object walk — never stored.

| Field | Type | Notes |
|-------|------|-------|
| `key` | string | The grouping key = common directory prefix (e.g. `sales/orders`). Stable id for the group. |
| `path` | string | Suggested `source.path` glob, e.g. `s3://cash-intel/sales/orders/*.parquet`. What the picker writes. |
| `format` | `"parquet" \| "csv" \| "delta"` | Inferred from object extensions / `_delta_log` presence. Must be one of `semantic.SOURCE_FORMATS`. |
| `object_count` | int | Number of objects under the prefix. |
| `bytes` | int | Total size (for display), summed from the walk. |
| `format_ambiguous` | bool | True when the prefix mixes extensions; picker shows a warning but still allows selection. |
| `models` | list of `{name, role}` | Which loaded models already read this location (reuses the Explorer's matcher logic). Informational only — does not forbid selection (FR-009 edge case). |
| `objects` | list of `{key, size, format}` | Individual objects for drill-down; selecting one writes an exact-key `source.path` instead of the glob. |

**Validation rules**:
- `format` must be a supported source format; a prefix with no recognizable data extension is omitted from the dataset list (or listed with `format_ambiguous=true` and a null suggested format) rather than producing an invalid `source`.
- Empty bucket → empty `datasets` list (picker shows an empty state, FR-009).
- The endpoint performs **no** data scan — object metadata only (Constitution II).

### DatasetColumns (columns of a chosen source)

Not a new shape — this is the **existing** `POST /api/models/validate` response `columns` field: `[{name, dtype}]`, present when the draft source is reachable, `null` + `schema_error` when not. The editor already consumes it; the picker and intellisense read from it. Documented here only to make the dependency explicit (FR-008).

## Existing entities (unchanged, referenced)

These are defined in `app/semantic.py` and are **not modified** by this feature; the guided affordances only read/write the YAML that parses into them:

- **Model** — `name, label, description, source, joins, dimensions, measures, imports`. The dataset picker fills `source`; import fills `imports` (`dimension_imports:` YAML). (FR-006, FR-010)
- **Source** — `path, format`. The single block the dataset picker writes. (FR-007)
- **Dimension** — `name, column, label, type, spine?, geo?`. Column-name contexts for intellisense are its `column`/key fields. Dimensions are **column references, not expressions** — this is why intellisense offers bare column names here. (Research Decision 3)
- **Measure** — `name, label, expr_source, format`. The **only** expression field; intellisense offers full polars completion inside its `expr:` value, live-validated by the existing parse. (FR-013, FR-015)
- **DimensionBundle / Dataset(bundle) / Import / ImportBinding** — the common-model machinery the guided import targets, unchanged. (FR-010, FR-011)

## Frontend state additions (ephemeral — Constitution V)

Added to `app/static/js/state.js`. **All session-only; none persisted.** A page reload resets them, which is the intended proof that in-progress editing never leaks to disk (FR-021).

| State | Type | Lifecycle | Persisted? |
|-------|------|-----------|------------|
| `view` value `"modelling"` | string | replaces `"explorer"` in the view enum | n/a (renamed) |
| `editor.dirty` | bool | set on `#yaml-editor` input; cleared on save / revert / open | **No** — ephemeral |
| `editor.columns` | `[{name,dtype}]` | last validate response's columns, for completion | **No** — derived cache |
| `datasets` cache | list of Dataset | fetched for the picker; refreshed on demand | **No** — derived cache |

**Explicit ephemeral/persisted decision (Constitution V)**: the dirty flag, the columns cache, and the datasets cache are throwaway view state. The **only** persisted artifacts remain the model/bundle YAML files, written exclusively on an explicit save. Navigating away with `editor.dirty === true` triggers a confirm; discarding drops the in-memory edit and nothing touches disk.

## State transitions — editor dirty flag

```text
open(model|bundle|new)  → dirty = false        (original loaded)
input into #yaml-editor → dirty = true
save (valid)            → dirty = false         (YAML written, hot-reload)
revert                  → dirty = false         (textarea reset to original)
leave while dirty       → confirm()
                            ├─ ok     → discard in-memory edit, proceed  (nothing persisted)
                            └─ cancel → stay in editor
page reload while dirty → edit lost              (proves non-persistence)
```

## Traceability

| Requirement | Data-model element |
|-------------|--------------------|
| FR-006/007 (pick dataset → source) | Dataset shape; picker writes `Source.path/format` |
| FR-008 (real columns) | DatasetColumns = validate `columns` |
| FR-009 (context, empty bucket) | Dataset `models`/`object_count`/empty list |
| FR-010/011 (guided import) | Import / ImportBinding (unchanged) |
| FR-013/015 (intellisense, live validity) | Measure.expr_source + column contexts; validate parse |
| FR-017 (single doc) | No structured-form entity — YAML text is sole state |
| FR-021 (ephemeral edits) | `editor.dirty` transitions above |
