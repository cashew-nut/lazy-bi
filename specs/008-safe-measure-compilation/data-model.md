# Data Model: Safe Measure Compilation

## Measure (unchanged shape, new compilation rule)

Lives in `app/semantic.py::Measure` (dataclass, unchanged fields): `name, label, expr_source, format, description, frame_source, frame_emits`.

- **Compilation rule (new)**: `expr()` calls `compile_measure(self.expr_source, schema, alias=self.name)` when `frame_source` is `None`; otherwise it keeps calling `compile_expr` (unchanged legacy path), reachable only because the whole `Measure` came from a YAML file written through the authenticated save endpoint.
- **Validation timing (unchanged)**: still validated at model load time (`_parse_model`) and at save time — validate-on-save is not new behavior, it is the existing pattern the new compiler plugs into.

## Inline measure (request-scoped dict, new restriction)

Shape from `POST /query`'s `inline_measures: list[dict]`, unchanged wire format `{name, expr, frame?, frame_emits?}` — but `frame`/`frame_emits` are now a **hard rejection** at `engine.run_query`, not an accepted code path. `expr` always compiles via `compile_measure`.

## MeasureCompileError

New exception in `app/measure_dsl.py`, subclassing `ValueError` (matching `ModelError`'s existing subclass-of-`Exception` pattern so callers can catch-and-400 the same way `semantic.ModelError` already is caught in `app/api/query.py` and `app/api/models.py`).

## Measure provenance record (new)

New SQLite table `measure_provenance` (see research.md R4 for full DDL). Fields:

| Field | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | matches existing tables' id convention |
| `model` | TEXT | model name the measure belongs to |
| `measure` | TEXT | measure name |
| `action` | TEXT | `create` \| `update` \| `delete` |
| `expr` | TEXT, nullable | DSL text snapshot; NULL for `delete` |
| `frame` | TEXT, nullable | frame snippet snapshot, if any |
| `frame_emits` | TEXT, nullable | JSON-encoded list |
| `author` | TEXT | self-declared label from the `X-Author` header |
| `version` | INTEGER | 1 + previous max version for this (model, measure) pair |
| `created_at` | TEXT | ISO8601, matching existing tables |

**Relationships**: Logically keyed by `(model, measure)`, not a foreign key to any other table (models are files, not DB rows — no join target exists, matching the existing `publications.dashboard_id`-without-`FOREIGN KEY` precedent in `store.py`).

**State transitions**: Append-only. A `create` row always has `version = 1` for that `(model, measure)`. A `delete` row's presence means the measure no longer exists in the YAML; a subsequent `create` for the same name starts a fresh version sequence from `MAX(version) + 1` (not reset to 1), so version numbers stay monotonically increasing per name even across a delete/recreate cycle — this is a deliberate choice to keep "version" meaning "the Nth save ever," not "saves since last delete."

## Auth credential (new, config-level, not a DB entity)

`config.API_KEY: str` (from `CI_API_KEY` env var, default `""` = unconfigured = fail closed). Not modeled as a database row — there is exactly one shared secret, no per-user table, per the maintainer's confirmed minimal choice.
