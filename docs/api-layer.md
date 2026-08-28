# API Layer

**Source:** `app/api/*.py` (17 routers, ~3,326 lines), aggregated under
`/api` by `app/api/__init__.py`'s `api_router`.

One router module per resource, each a plain `fastapi.APIRouter()` with its
own Pydantic request models. This page is the route map; for *why* a given
route behaves the way it does, follow the link to the module doc that owns
it. Every route requires a signed-in identity except `POST /api/auth/login`
and `GET /api/health` — see [Auth & Security](auth-and-security.md) for the
middleware that enforces that centrally, the CSRF header cookie-authenticated
mutations must carry, and the full role matrix
(`tests/test_role_matrix.py` / `specs/011-session-auth-rbac/contracts/auth-api.md`).

**Shared conventions across every router:**

- **`app/api/deps.py`** — `get_model(name)`/`get_bundle(name)`/
  `get_pipeline(name)` are the 404-or-return helpers every router that
  addresses one by name uses.
- **The locked-object 403 pattern** — a built-in (git-tracked) model,
  dimension bundle, or pipeline 403s on any *structural* mutation
  (`_forbid_if_locked` in `models.py`/`dimensions.py`/`pipelines.py`); a
  freshly-created *local* one doesn't. See
  [Storage & Runtime → Locked vs. local objects](storage-and-runtime.md#locked-vs-local-objects).
- **The reload-or-400 pattern** — every YAML write calls
  `registry.reload_all()` immediately afterward and turns a `ModelError`/
  `PipelineError` into a 400, so an edit that breaks something is refused
  and reported rather than silently applied.
- **SSE routes** (`chat.py`'s `/ask/stream` and `/panel/ask/stream`,
  `composer.py`'s `/compose/stream`, `sandbox.py`'s `/agent/stream`) all
  emit the same wire shape (`event: <kind>\ndata: <json>\n\n`) and end with
  one terminal `response` event carrying exactly what the non-streaming
  form of the same action would have returned.
- **Auditing** — every mutation of consequence (`app/authstore.py`'s
  `record_audit`) writes an append-only row: actor, action, target. No
  browsing API is exposed for it on purpose.

## Auth (`app/api/auth.py`)

| Route | Role | Notes |
|---|---|---|
| `POST /api/auth/login` | public | Identical 401 for unknown username and wrong password, real hash check on both paths — no username oracle. |
| `POST /api/auth/logout` | any | Revokes the presented session. |
| `GET /api/auth/me` | any | |
| `POST /api/auth/password` | any | Revokes every *other* session for the account. |

## Users & tokens (`app/api/users.py`)

| Route | Role | Notes |
|---|---|---|
| `GET`/`POST /api/users` | admin | No self-signup. |
| `PATCH /api/users/{id}` | admin | Role/active/password changes; refuses to demote/deactivate the last active admin (`LastAdminError` → 409). Deactivation or a password reset revokes every session the account holds. |
| `GET`/`POST /api/tokens` | any (own) | The token secret is returned **once**, at creation. |
| `DELETE /api/tokens/{id}` | any (own) | |
| `GET`/`PUT /api/users/me/theme` | any (own) | One of the 4 fixed theme ids — see [Frontend](frontend.md#themes). |

## Semantic models (`app/api/models.py`)

| Route | Role | Notes |
|---|---|---|
| `GET /api/models` | any | `Model.to_public()` for every loaded model. |
| `POST /api/models/reload` | admin | Re-reads `models/*.yaml`. |
| `POST /api/models/validate` | any | Parse-check + source-column introspection; never persists. |
| `POST /api/models/generate` | author | Guided-form spec → canonical YAML → the same validate response, in one call. |
| `GET /api/models/{name}/spec` | any | Model YAML re-parsed into the form-editable spec shape. |
| `POST /api/models` | admin | Always creates a **local** model — never writes into the git-tracked `models/` directory. |
| `GET`/`PUT /api/models/{name}/yaml` | any / admin | `PUT` 403s on a locked model. |
| `DELETE /api/models/{name}` | admin | 403 on a locked model. |
| `GET /api/models/{name}/schema` | any | Post-join source columns with dtypes — feeds the measure editor's completion list. |
| `POST /api/measures/check` | any | Live ✓/✗ feedback while a measure is still being typed, without needing a saved model. |
| `POST /api/models/{name}/measures` | **author** | Appends a measure (comment-preserving text surgery — [Semantic Layer](semantic-layer.md#provenance-locking-and-text-preserving-edits)). Works even on a *locked* model — the lock is structural-only. |
| `PUT`/`DELETE /api/models/{name}/measures/{measure}` | author | Only on a single-fact-table model — see `_single_fact_or_400`. |
| `GET /api/models/{name}/measures/{measure}/history` | any | Append-only provenance (`measure_provenance` table). |
| `GET /api/models/{name}/dimensions/{dim}/values` | any | Distinct values for filter pickers (`engine.dimension_values`). |

## Dimension bundles (`app/api/dimensions.py`)

Mirrors the models router, minus anything measure-specific (a bundle's
datasets never declare measures — see
[Semantic Layer](semantic-layer.md#dimension-bundles-shared-dimensions)).

| Route | Role |
|---|---|
| `GET /api/dimensions` | any |
| `POST /api/dimensions/reload` | admin |
| `POST /api/dimensions/validate` | any |
| `POST /api/dimensions/generate` | author |
| `GET /api/dimensions/{name}/spec` | any |
| `POST /api/dimensions` | admin |
| `GET`/`PUT /api/dimensions/{name}/yaml` | any / admin |
| `DELETE /api/dimensions/{name}` | admin — 409 if any loaded model still imports it |

## Datasets & explorer (`app/api/datasets.py`, `app/api/explorer.py`)

Read-only bucket discovery, reused by the Modelling workspace's source
picker and its landing-page overview — see
[Semantic Layer → Dataset discovery helpers](semantic-layer.md#dataset-discovery-helpers).

| Route | Role | Notes |
|---|---|---|
| `GET /api/datasets` | any | Every object across every browsable bucket, grouped into pickable datasets, tagged with which models already read them. |
| `GET /api/datasets/schema` | any | Columns of an arbitrary source path — feeds relationship pickers before any model exists. |
| `POST /api/datasets/local` | author | Multipart upload (`.csv`/`.parquet`) into `local/<name>/…`, unmodeled, ready to build a model on. |
| `DELETE /api/datasets/local/{name}` | author | |
| `GET /api/explorer` | any | Same bucket walk as `/datasets`, shaped for the data-overview pane. |
| `GET /api/health` | public | Liveness probe + LLM configuration echo (provider, model choices, thinking-capable models) — see [Conversational Analytics](conversational-analytics.md). Also reports this replica's `node`, `role` and `clustered`, so two identical requests returning two node ids confirms the load balancer is spreading — see [Scaling](scaling.md). |
| `GET /api/cluster` | admin | The deployment's own state: the live node roster with roles, the change generations each node has observed, and every held lock with its holder and expiry. The questions a load-balanced `/api/health` cannot answer — is the worker running, is a replica a generation behind, is a pipeline target locked by a node that died. See [Scaling](scaling.md#8-observability). |

## Query (`app/api/query.py`)

| Route | Role | Notes |
|---|---|---|
| `POST /api/query` | any | Runs a semantic query (`engine.run_query`) — see [Query Engine](query-engine.md). |
| `POST /api/query/extract` | any | The same query as an Arrow IPC stream a dashboard tile re-aggregates client-side (`extract.build`) — see [Query Engine → Instant mode](query-engine.md#instant-mode-appextractpy). A 200 with `{"fallback": {...}}` JSON is a *routine* answer, not an error; the response's content type (Arrow stream vs. JSON) is how a caller tells them apart. |

## Visuals & dashboards (`app/api/visuals.py`, `app/api/dashboards.py`)

| Route | Role | Notes |
|---|---|---|
| `GET`/`POST /api/visuals` | any / author | `_validate_visual_spec` checks declared parameters (type-aware — `engine.PARAM_TYPES`) and that any inline measure referencing one only names a declared parameter. |
| `PUT`/`DELETE /api/visuals/{id}` | author | |
| `GET`/`POST /api/dashboards` | any / author | `_check_param_conflicts`: two tiles on one dashboard may not declare a same-named parameter with different types/values/defaults. |
| `GET /api/dashboards/{id}` | any | Resolves every tile to its visual in one call. |
| `PUT`/`DELETE /api/dashboards/{id}` | author | |
| `POST /api/publish` | author | Puts a dashboard in the portal under a slash-separated folder path. |
| `DELETE /api/publish/{id}` | author | Unpublish. |
| `GET /api/portal` | any | Every published dashboard, with folder + tile count. |

## Notebooks (`app/api/notebooks.py`)

| Route | Role |
|---|---|
| `GET /api/notebooks` / `GET /api/notebooks/{id}` | any |
| `POST`/`PUT`/`DELETE /api/notebooks[/{id}]` | author |

The server treats a notebook's `html` as opaque text on the CRUD path
itself — see [Conversational Analytics → The Composer](conversational-analytics.md#the-composer-appcomposerpy)
for where sanitization actually happens (on the *compose* path, before a
draft is ever handed back to be saved through these same routes).

## Model memories (`app/api/memories.py`)

| Route | Role |
|---|---|
| `GET /api/models/{name}/memories` | any (a memory's whole purpose is to be merged into a catalog every user already sees) |
| `POST`/`PATCH`/`DELETE /api/models/{name}/memories[/{id}]` | admin |

See [Conversational Analytics → Self-learning model memories](conversational-analytics.md#self-learning-model-memories-appmemorystorepy).

## Pipelines

| Route | Role | Notes |
|---|---|---|
| `GET /api/pipelines` | any | Each with its latest-run summary. |
| `POST /api/pipelines/validate` | any | Parse-checks the SQL — **never executes it**. |
| `POST /api/pipelines/reload` | admin | |
| `POST /api/pipelines` | admin | Always creates a **local** pipeline. |
| `GET`/`PUT /api/pipelines/{name}/yaml` | any / admin | Name is immutable — a `PUT` renaming it is a 400. |
| `DELETE /api/pipelines/{name}` | admin | 409 while a run is pending; marks the target model's `pipeline_lineage:` section orphaned. |
| `POST /api/pipelines/{name}/run` | admin | 202; 409 if this pipeline already has a run pending. |
| `GET /api/pipelines/{name}/runs` / `GET /api/runs/{id}` | any | |
| `GET /api/pipelines/{name}/lineage/suggest` | any | Pass-through suggestions by name-matching schemas — never auto-saved. |
| `GET`/`PUT /api/lineage/layers` | any / admin | `PUT` 409s on removing a layer still referenced by a pipeline. |
| `GET /api/lineage/graph` | any | The read-only lineage DAG payload. |

Full detail: [Pipelines](pipelines.md).

## Sandbox

| Route | Role |
|---|---|
| `GET /api/sandbox/notebooks[/{id}]` | any |
| `POST`/`PUT`/`DELETE /api/sandbox/notebooks[/{id}]` | admin |
| `POST /api/sandbox/run` | admin |
| `POST /api/sandbox/convert` | admin — text-only, never executes anything |
| `POST /api/sandbox/agent/stream` | admin — 503 unless `CI_LLM_API_KEY` is set |

Full detail: [Sandbox](sandbox.md).

## Chat

| Route | Role | Notes |
|---|---|---|
| `GET`/`POST /api/conversations` | viewer | 503 unless LLM-configured. |
| `GET`/`PATCH`/`DELETE /api/conversations/{id}` | viewer (owner-scoped) | |
| `POST /api/conversations/{id}/ask` | viewer | Non-streaming ask. |
| `POST /api/conversations/{id}/ask/stream` | viewer | SSE twin — identical persisted result. |
| `POST /api/conversations/{id}/messages/{msg_id}/pin` | **author** | Persists an answered turn as a saved visual, optionally onto a (new or existing) dashboard. |
| `POST /api/chat/panel/ask/stream` | viewer | The modelling workspace's ephemeral inline chat — scoped to exactly one model, nothing persisted. |

Full detail: [Conversational Analytics](conversational-analytics.md).

## Composer

| Route | Role |
|---|---|
| `GET /api/composer/context` | author — 503 unless LLM-configured |
| `POST /api/composer/compose/stream` | author — 503 unless LLM-configured |

Full detail: [Conversational Analytics → The Composer](conversational-analytics.md#the-composer-appcomposerpy).

## Query request shape

The one body shape `POST /api/query`, `/api/query/extract`, and (indirectly,
via a validated `ProposeQuery`) chat all converge on:

```jsonc
{
  "model": "sales",
  "dimensions": [{"name": "order_date", "grain": "1mo"}, "region"],
  "measures": ["revenue", "margin_pct"],
  "inline_measures": [{"name": "revenue_yoy", "expr": "..."}],
  "filters": [{"field": "segment", "op": "in", "values": ["corpo", "solo"]}],
  "sort": {"by": "revenue", "desc": true},
  "limit": 1000,
  "parameters": [{"name": "threshold", "type": "float", "values": [10, 50.5, 100], "default": 50.5}],
  "parameter_values": {"threshold": 100}
}
```

`filters[].op` ∈ `eq ne gt gte lt lte in not_in contains`; a date/time
value is either a fixed ISO date or a [relative-date token](query-engine.md#filters-and-relative-dates).
