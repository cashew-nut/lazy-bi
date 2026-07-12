# Contract: Model-measure authoring API (Tier 1)

All routes below live in `app/api/models.py`. Read/query routes are unchanged and require no auth. Mutating routes require the new `require_measure_author` dependency (`app/auth.py`).

## Auth

Header-based, checked on every mutating route:

- `X-API-Key: <secret>` — must match `config.API_KEY` (constant-time compare). If `config.API_KEY` is unset/empty, every request is rejected with `401` regardless of header (fail closed when unconfigured).
- `X-Author: <label>` — required, non-empty; becomes the `author` value on the provenance row. Free text, not verified — a known limitation (see spec.md Assumptions).

Missing/incorrect key → `401 Unauthorized`. Missing/empty author with a correct key → `400 Bad Request` (distinct from an auth failure, since the key was valid).

## Routes

### `POST /models/{name}/measures` (existing route, behavior extended)

Body: `MeasureIn { name, expr, label?, format?, description?, frame?, frame_emits? }` — `frame`/`frame_emits` newly accepted here (model-measure path only).

- Requires auth (new).
- Validates `expr` via `compile_measure` (schema = `engine.scan(model).collect_schema()`) when `frame` is absent; via `compile_expr` + `validate_frame` (existing functions) when `frame` is present.
- On success: appends to YAML (existing `append_measure_yaml`), hot-reloads (existing), **and** writes a `measure_provenance` row with `action="create"`, `author=<X-Author>`.
- Response: unchanged (`registry.models[name].to_public()`), per existing behavior.

### `PUT /models/{name}/measures/{measure_name}` (new)

Body: same `MeasureIn` shape (name in body must match path or is rejected 400).

- Requires auth.
- Measure must already exist (404 otherwise — symmetry with `get_model`'s 404 pattern).
- Re-validates the new expression the same way as create.
- Rewrites the measure's YAML block in place (new helper alongside `append_measure_yaml`, e.g. `replace_measure_yaml`) — full re-render of that one measure's YAML entry, not a byte-diff patch, mirroring how `append_measure_yaml` already renders one measure via `yaml.dump`.
- Writes a `measure_provenance` row with `action="update"`.

### `DELETE /models/{name}/measures/{measure_name}` (new)

- Requires auth.
- Measure must exist (404 otherwise).
- Removes the measure's block from the YAML (new helper), hot-reloads.
- Writes a `measure_provenance` row with `action="delete"`, `expr=NULL`.
- No separate "deactivate" state is introduced — YAML has no soft-delete concept today (a measure either has a YAML entry or it doesn't), and adding an `is_active` flag with no corresponding read-path behavior would be unused surface. This is a deliberate scoping decision, recorded here rather than silently dropped from the original brief's suggested endpoint list.

### `GET /models/{name}/measures/{measure_name}/history` (new, read-only, no auth)

Returns the `measure_provenance` rows for `(model=name, measure=measure_name)`, newest first — the "review + version control" read surface, available to anyone who can already read the model (no authoring credential needed to *view* history, consistent with FR-010's "reading never requires the credential").

## Error shapes (unchanged convention)

All errors use FastAPI's existing `HTTPException(status_code, detail=str)` pattern already used throughout `app/api/models.py` — no new error envelope introduced.
