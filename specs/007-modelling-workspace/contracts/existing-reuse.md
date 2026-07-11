# Contract: Existing endpoints reused as-is

This feature is mostly a frontend re-composition over a backend that already exposes the needed operations. The following endpoints are **reused unchanged**; the Modelling workspace and elevated editor call them exactly as Studio does today.

## Fact models — `app/api/models.py`

| Endpoint | Used for |
|----------|----------|
| `GET /api/models` | Model list in the Modelling workspace + Studio's `#model-select` |
| `GET /api/models/{name}/yaml` | Open a model in the editor (raw YAML, FR-016) |
| `POST /api/models/validate` | Live validation + column suggestions (see completion.md) |
| `POST /api/models` | Create a new model from editor YAML (FR-019 name-collision 409) |
| `PUT /api/models/{name}/yaml` | Save edits (hot-reload) |
| `DELETE /api/models/{name}` | Delete (FR-020 warning is a client-side confirm) |
| `GET /api/models/{name}/schema` | Post-join columns (measure-lab completion; still available) |
| `POST /api/models/{name}/measures` | Measure-lab "save to model" (unchanged) |

## Common models (dimension bundles) — `app/api/dimensions.py`

| Endpoint | Used for |
|----------|----------|
| `GET /api/dimensions` | Common-model list + import panel |
| `GET /api/dimensions/{name}/yaml` | Open a common model in the editor |
| `POST /api/dimensions/validate` | Live validation + per-dataset columns |
| `POST /api/dimensions` | Create a common model |
| `PUT /api/dimensions/{name}/yaml` | Save edits |
| `DELETE /api/dimensions/{name}` | Delete (409 when still imported — FR-020) |

## Data overview — `app/api/explorer.py`

| Endpoint | Used for |
|----------|----------|
| `GET /api/explorer` | The datasets↔models overview table, now shown **inside** Modelling instead of as a separate mode (FR-005) |

## Notes

- No request/response schema of any reused endpoint changes. FR-019 (name collision) and FR-020 (delete guards) are already enforced server-side (`409` with a clear message); the guided flow only needs to surface those messages, which `editor.js` already does.
- The **only** new backend surface is `GET /api/datasets` (contracts/datasets.md) and the optional `GET /api/completion/methods` (contracts/completion.md).
