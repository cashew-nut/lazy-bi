# Storage & Runtime

**Source:** `app/config.py` (714 lines) · `app/registry.py` (194 lines) ·
`app/store.py` (344 lines) · `app/s3.py` (162 lines) · `app/emulator.py`
(37 lines) · `app/seed.py` (568 lines) · `app/localmodelstore.py` /
`app/localbundlestore.py` / `app/localpipelinestore.py`

This page covers the plumbing everything else in the app depends on:
where settings come from, what runtime state the process holds, how the
platform talks to S3, and what it persists about itself in SQLite.

## Configuration (`app/config.py`)

Everything defaults to a **fully local demo**: an embedded S3 emulator, the
bundled `models/`/`dimensions/` directories, and `cash_intel.db` in the
project root. Settings are read from the environment, optionally seeded
from a `.env` file first.

**`.env` loading** (`_load_env_file()`) is a deliberate literal parser, not
a shell — a value is taken exactly as written (quotes stripped, no
expansion), so an API key containing `$`, `#`, backticks or spaces needs no
escaping. It runs **before** anything else reads `os.environ`, and — this
is the one surprising bit — **a `.env` entry overrides an already-exported
shell variable of the same name**, not the other way around. That's so
editing the file is always enough to change behavior, with no `unset` of
some earlier experiment's `export` required; the trade-off (a stray `.env` a
deployer didn't expect can override a real deployed secret) is accepted
deliberately. `CI_ENV_FILE=` (set empty) skips file loading entirely — what
the test suite does, so a developer's own `.env` can never change what a
test run sees. Startup prints which settings a `.env` entry actually
overrode (names only, never values).

### The two-store model

```python
@dataclass(frozen=True)
class Store:
    name: str        # "demo" | "primary"
    endpoint: str    # "" = real AWS, addressed by region
    region: str
    demo: bool
```

Two logical object stores, deliberately kept separate:

- **The demo store** holds `DEMO_BUCKET` (`cash-intel` by default) — the
  bucket every shipped `models/*.yaml`/`dimensions/*.yaml` names by absolute
  path. Served by the embedded emulator (`app/emulator.py`) unless
  `CI_DEMO_S3_ENDPOINT` points somewhere else.
- **The primary store** holds `BUCKET` (`CI_BUCKET`) and every path any
  model, pipeline or notebook names that isn't the demo bucket — the one
  real credentials open, and the only one this app ever treats as durable.

While `CI_BUCKET` is unset (or equals `DEMO_BUCKET`), `demo_store() ==
primary_store()` — one store, zero configuration, exactly how it behaved
before this split existed. The moment `CI_BUCKET` names something else,
`stores_split()` becomes true: the demo bucket keeps its own emulator (so
the demo catalog keeps answering) while every other path goes to the real
endpoint with real credentials. `store_for(bucket)` / `store_for_path(path)`
are the one rule applied everywhere a bucket is touched — nothing demo-shaped
is ever written into a real account, and nothing the demo needs depends on
one being configured.

**Credential resolution** (`resolve_credentials()`) holds **one boto3
credential resolver per process**, not per call — constructing a fresh
`boto3.Session` re-parses `~/.aws/config` and re-runs the whole provider
chain (an SSO token read, `credential_process`, an STS `AssumeRole` round
trip), which measures 150ms+ on a corporate-auth laptop, and this sits on
the hottest path there is (every DuckDB cursor checks store secrets, and
every model save re-validates every measure through a cursor each). A
credential botocore can refresh itself (SSO, assume-role, an instance role)
is held indefinitely; a static one is re-checked on a slow cadence
(`_BOTO_STATIC_TTL = 900s`) in case the file behind it rotates. A **failed**
resolution is never cached, so `aws sso login` mid-process is picked up on
the very next call.

### Key settings

| Concern | Variables | Notes |
|---|---|---|
| Object stores | `CI_BUCKET`, `CI_S3_ENDPOINT`, `CI_BUCKET_PREFIX`, `CI_DEMO`, `CI_DEMO_BUCKET`, `CI_DEMO_S3_ENDPOINT`, `CI_LIST_MAX_KEYS` | Leave `CI_S3_ENDPOINT` unset for real AWS (addressed by region); set it only for MinIO/LocalStack/a vendor. |
| AWS credentials | `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`, `AWS_REGION` | `AWS_PROFILE` (SSO-capable) takes priority over static keys when both are set; neither set falls through to boto3's own chain (instance/task role). |
| DuckDB runtime | `CI_DUCKDB_PATH` (default `:memory:`), `CI_DUCKDB_THREADS`, `CI_DUCKDB_MEMORY_LIMIT`, `CI_HTTP_RETRIES`, `CI_HTTP_TIMEOUT_MS` | Thread/memory limits are left to DuckDB's own host-derived defaults unless set — matters in a memory-capped container. See [Query Engine](query-engine.md). |
| Caching | `CI_SCHEMA_CACHE_TTL`, `CI_BOUNDS_CACHE_TTL`, `CI_SOURCE_CACHE_TTL`, `CI_SOURCE_CACHE_MAX_BYTES`, `CI_SOURCE_CACHE_MAX_RESIDENT_BYTES` | See [Query Engine → duck.py](query-engine.md#duckpy-one-connection-tuned-for-a-real-object-store). |
| Instant mode | `CI_EXTRACT_MAX_ROWS`, `CI_EXTRACT_MAX_BYTES` | See [Query Engine → Instant mode](query-engine.md#instant-mode-appextractpy). |
| Sessions | `CI_SESSION_IDLE_DAYS`, `CI_SESSION_MAX_DAYS`, `CI_COOKIE_SECURE` | See [Auth & Security](auth-and-security.md). |
| LLM provider | `CI_LLM_API_KEY`, `CI_LLM_BASE_URL`, `CI_LLM_PROVIDER`, `CI_LLM_MODEL`, `CI_LLM_MODEL_CHOICES`, `CI_LLM_THINKING_MODELS`, … | See [Conversational Analytics](conversational-analytics.md). |
| Pipelines / sandbox | `CI_PIPELINES_DIR`, `PIPELINE_TIMEOUT_DEFAULT`/`_MAX`, `SANDBOX_TIMEOUT_DEFAULT`/`_MAX`, `SANDBOX_ROW_LIMIT` | See [Pipelines](pipelines.md), [Sandbox](sandbox.md). |
| Directories | `CI_MODELS_DIR`, `CI_DIMENSIONS_DIR`, `CI_DB_PATH`, `CI_LOCAL_DATA_DIR`, `CI_AGENTS_DIR` | All default to project-root-relative paths; the Docker image points them at `/srv/...` and `/data/...` (see the Dockerfile). |

Every setting with a meaningful default is read through `_env`/`_int`/
`_float`/`_bool`/`_csv` helpers that treat an **empty** value as *unset*
rather than *empty-string* — this matters because `${VAR:-}` in
`docker-compose.yml` (or a bare `KEY=` in `.env`) produces an empty string
for a variable nobody actually set, and reading that literally would turn
"leave this alone" into "set the S3 endpoint to ''".

## The runtime registry (`app/registry.py`)

`Registry` (one instance, `registry`, imported everywhere) is the process's
live state: loaded `models`, `dimension_bundles`, `pipelines`, `layers`,
`agents`, and every SQLite-backed store (`VisualStore`, `AuthStore`,
`ConversationStore`, `MemoryStore`, `PipelineStore`, `SandboxStore`, and the
three local-object stores). `Registry.init()` (called once from
`app/main.py`'s lifespan) opens every store and does the first
`reload_all()`; every subsequent model/pipeline/bundle edit calls
`reload_all()` again to hot-reload.

**`reload_all()`'s load order matters**: dimension bundles before models
(a model's imports validate against already-loaded bundles), layers before
pipelines (a pipeline's `layer:` refs validate against them), pipelines
after models (target→model lineage matching needs models loaded).

### Locked vs. local objects

Every model, bundle and pipeline is either:

- **Built-in** (`locked=True`) — parsed from the git-tracked
  `models/`/`dimensions/`/`pipelines/` directory, `origin` set to its file
  `Path`. Structural changes (create/rename/delete, or a raw-YAML `PUT`)
  403 even for an admin — that catalog only changes by editing a file and
  committing it.
- **Local** (`locked=False`) — created through the API, persisted as a row
  in `local_models`/`local_bundles`/`local_pipelines` (in `cash_intel.db`,
  gitignored), `origin=None`. Freely renamable/editable/deletable, and
  survives a restart because it's a real database row — it never becomes
  something `git status` would notice.

`reload_all()` merges the two: built-in objects load first, then each
local-store row is parsed and added **only if its name doesn't already
belong to a built-in (or earlier local) object** — a name a locked object
already owns always wins. A hand-corrupted local row (fails to parse) is
skipped rather than sinking the whole reload; a *built-in* model failing to
resolve, by contrast, is a real codebase bug and re-raises — the two
failure modes are handled differently on purpose. `registry.read_model_text()`/
`write_model_text()` (and the bundle/pipeline equivalents) branch on
`locked` to read/write the right backing store, so callers (the measure
lab, the pipeline-lineage writer) never need to know which kind of object
they're touching.

**Deliberately no cache-clearing on reload.** A model/bundle/pipeline edit
changes YAML, not a byte in the bucket — every S3-derived cache entry
(`app/cache.py`, `app/duck.py`) is keyed on what actually determines its
answer (a source path, a rendered scan's SQL, or a per-object instance
token), so a model that repoints its source is simply a new key that
misses on its own. Clearing here is what used to make the authoring loop
cold — every save re-listing and re-pinning every source against a real
endpoint. The call sites that *do* need to invalidate (a pipeline run
writing new bytes, a dataset upload/delete) call `cache.clear()` +
`duck.invalidate()` explicitly — see
[Pipelines](pipelines.md) and `app/api/datasets.py`.

## SQLite persistence (`app/store.py` — `VisualStore`)

One SQLite file, `cash_intel.db`, one class per feature area (this
project's convention throughout: schema-on-init `SCHEMA` string,
`sqlite3.Row` factory, a small typed wrapper class). `VisualStore` alone
owns:

| Table | Holds |
|---|---|
| `visuals` | Saved query+chart specs (`{query, chartType, ...}` as JSON). |
| `dashboards` | Ordered tiles (`{visual_id, w}`), named filter-set **views**, `active_view`, and the `instant` opt-in flag (specs/016). |
| `publications` | Which dashboards are exposed in the portal, and under which slash-path folder. |
| `notebooks` | Freeform HTML narrative pages (name + body-fragment `html`). |
| `measure_provenance` | Append-only audit log of every model-measure create/update/delete — author, expression snapshot, version. |

Other feature areas get their own store class on the same database file:
`AuthStore` (users/sessions/tokens/audit — [Auth & Security](auth-and-security.md)),
`ConversationStore` (chat history), `MemoryStore` (chat-learned model facts),
`PipelineStore` (run history), `SandboxStore` (saved notebooks), and the
three `local*store.py` classes above — see
[Conversational Analytics](conversational-analytics.md),
[Pipelines](pipelines.md), [Sandbox](sandbox.md) for each.
Schema migrations are additive and guarded (`PRAGMA table_info` checked
before `ALTER TABLE ADD COLUMN`), so an existing database upgrades in place
across every feature that shipped after the table already existed.

## S3 access (`app/s3.py`)

A shared boto3 client factory: `client(bucket)` resolves which store owns
`bucket` (`config.store_for`) and hands back a cached client. **Clients are
cached by `(store, region, credentials)`**, not by store alone — constructing
one costs ~130ms the first time (botocore parses the service model) and
~6ms after, which is fine for an admin action but not on the query path;
keying on the resolved credentials (rather than static config) is what lets
a rotating SSO credential still get a fresh client the moment it actually
changes, while a static-key deployment builds its client exactly once.

`bucket_region()` resolves a bucket's *real* AWS region via
`GetBucketLocation` (falling back to the `x-amz-bucket-region` error header
when that call is denied — common for a read-only identity), cached once
per process. This matters more than it sounds: SigV4 signs into a specific
region, so a bucket outside `AWS_REGION` is refused outright rather than
merely slow, and the failure otherwise surfaces as a bare HTTP redirect
DuckDB reports as an opaque error.

`browsable_buckets()` returns the `(bucket, prefix)` pairs the Modelling
workspace's dataset picker and the explorer walk — normally one, two when a
real store is configured alongside the demo one. `walk(bucket, prefix,
limit)` is the bounded listing every such walk uses: capped at
`CI_LIST_MAX_KEYS` (20,000 default) and reports itself truncated past that,
since S3 hands back keys 1,000 per round trip and an unbounded walk of a
real bucket is a page that never finishes rather than merely a slow one.

## The embedded demo emulator (`app/emulator.py`)

A `moto.server.ThreadedMotoServer`, started in-process
(`start_if_embedded()`) whenever the demo store's endpoint is the built-in
loopback address `127.0.0.1:9600` (`config.EMBEDDED_EMULATOR`) — the
default, and still true even when a real bucket is configured for
everything else, which is precisely what keeps the built-in demo catalog
answering next to real data rather than 404ing against an account that's
never heard of it. It's in-memory: everything written to it is gone on
restart, which is why `app/seed.py` reseeds the whole demo bucket from
scratch on every start.

## Seeding (`app/seed.py`)

Runs at startup, each step independently idempotent:

- **`seed_bucket()`** — generates and uploads the demo datasets into
  `config.DEMO_BUCKET` on the demo store, **only if the bucket is empty**:
  `sales/<year>.parquet` (a multi-file parquet glob, ~60k order lines),
  `marketing/spend.parquet`, `ref/products.csv` + `ref/regions.csv` +
  `ref/territories.csv` (csv lookups, the latter two backing the
  `geography` dimension bundle), `logistics/shipments` (Delta Lake),
  `support/tickets` (Iceberg), `subscriptions/subs.parquet` (the spine
  demo), and `ref/calendar.parquet` (the standalone date table backing the
  `how: between` demo — see
  [Semantic Layer](semantic-layer.md#point-in-time-spines-and-calendar-imports)).
  One dataset per supported source format, deterministic
  (`random.Random(2077)`), so the same demo data is generated every time.
  Also uploads `data_cache/` (big optional datasets — see
  `app/load_taxi.py`) and `raw_data/` (small, git-committed sample files a
  deployer wants pre-loaded but unmodeled).
- **`restore_local_uploads()`** — puts every file a user uploaded through
  `POST /api/datasets/local` back into the bucket. Only runs against an
  **ephemeral** store (the embedded emulator): a real bucket already
  remembers what was written to it, so re-uploading on every start there
  would resurrect anything deliberately deleted and re-pay for bytes
  already present. The durable copy lives on local disk
  (`CI_LOCAL_DATA_DIR`, gitignored) either way.
- **`seed_bootstrap_admin()`** — first-run only: when zero accounts exist,
  creates the `admin` account with a random password, printed **once** to
  the startup log, never again once any account exists — so a production
  database can never regress to a well-known credential.
- **`seed_notebook_demo()`** — first-run only: builds a small set of saved
  visuals and a dashboard around the `sales` model, then composes them into
  one sample notebook demonstrating tabs, a collapsible, and an embedded
  dashboard view — see [Frontend → Notebooks](frontend.md).

## Local (non-git) model/bundle/pipeline stores

`app/localmodelstore.py`, `app/localbundlestore.py`,
`app/localpipelinestore.py` are near-identical thin SQLite wrappers (one
table each: `local_models`/`local_bundles`/`local_pipelines`, `name TEXT
PRIMARY KEY` + `yaml TEXT`), keyed by the name an object was **created**
under — which can drift from the name declared inside its own YAML after a
rename (`update()` takes an optional `new_name` to move the row's key along
with it, mirroring the same quirk a file's basename has relative to its own
`name:` field). `localpipelinestore.py` additionally holds the single
deployment-wide `layers.yaml` document as one row (a `PUT` there always
replaces the whole ordered list, so there's no per-layer merge to reason
about).
