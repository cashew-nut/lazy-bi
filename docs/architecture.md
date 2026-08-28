# Solution Architecture

Lazy BI ("Cash Intelligence") is a lightweight BI platform that queries files
directly in S3 — no warehouse, no ETL step required to start — through a
YAML-declared semantic layer, a single-connection DuckDB engine, a FastAPI
backend, and a build-free vanilla-JS frontend. This page is the map: what the
system is built from, how a request moves through it, and where the trust
boundary sits. Each linked page below is the detailed reference for one part.

## The core idea

1. **Files stay in the bucket.** DuckDB reads parquet, CSV, Delta and Iceberg
   sources *in place* via `httpfs`/`delta`/`iceberg` extensions. Only the
   columns and row groups a query needs leave S3 — that pushdown property is
   the product's reason for existing (see
   [Query Engine](query-engine.md)).
2. **The semantic layer is the only contract.** Every dimension and measure
   the UI, API or an LLM can use is declared in a model YAML file first
   (`models/*.yaml`, `dimensions/*.yaml`). Nothing downstream ever touches a
   raw column directly (see [Semantic Layer](semantic-layer.md)).
3. **Everything authored is SQL, and none of it is trusted as text.** A
   measure's `expr:`, a complex measure's `from:` block, a pipeline's `sql:`,
   a sandbox cell — all SQL, all parsed by DuckDB's own parser into an AST,
   checked node-by-node against a fail-closed allowlist, and **re-emitted
   from the validated AST** rather than the author's own text. There is no
   `eval`, `exec` or `compile` anywhere in the query or measure path (see
   [Query Engine](query-engine.md#the-sql-grammar)).
4. **Two deliberate escape hatches carry real I/O reach**, and both are
   gated on that reach, not on code execution: pipelines and sandbox
   notebooks may call table functions (`read_parquet`, `delta_scan`,
   `COPY … TO`) and so can read/write arbitrary bucket paths. Authoring *and
   running* either requires the **admin** role (see
   [Pipelines](pipelines.md), [Sandbox](sandbox.md), and the trust-boundary
   note in [Auth & Security](auth-and-security.md)).

## Request flow

```
┌────────────────────────────────────────────────────────────────────────┐
│ Browser — vanilla ES modules, no bundler, no build step                │
│   Studio · Modelling · Portal · Chat · Sandbox · Account · Composer    │
└──────────────────────────────┬─────────────────────────────────────────┘
                                │ fetch() — session cookie or Bearer token,
                                │ JSON / SSE / Arrow IPC
                                ▼
┌──────────────────────────────────────────────────────────────────────--┐
│ FastAPI process (app/main.py)                                          │
│                                                                          │
│  AuthMiddleware — default-deny on /api and /mcp (app/auth.py)          │
│         │                                                                │
│         ▼                                                                │
│  api/*.py routers  (one per resource — see api-layer.md)                │
│    │            │              │                  │                     │
│    ▼            ▼              ▼                  ▼                     │
│  semantic     pipelines /   skills / agents /   conversational          │
│  query        sandbox       MCP  (/mcp)          (chat, Composer,       │
│  (/api/query,  (admin-gated,                      sandbox agent)        │
│  visuals,      subprocess-                            │                 │
│  dashboards)   isolated)                               │                 │
│    │              │              │                     │ propose only —│
│    ▼              │              └── invoke_skill() ──►│ never executes │
│  engine.py         │                  wraps the SAME     ▼                │
│  (semantic query   │                  ask→resolve→run  nlq.resolve()     │
│   → one SQL         │                  path as chat     re-validates      │
│   statement)         │                                  against the      │
│    │                  ▼                                  LIVE model,     │
│    │           subprocess: pipeline_runner.py /           then calls     │
│    │           sandbox_runner.py — killable,              engine.run_query│
│    │           isolated from the main process                │           │
│    ▼                  │                                       │           │
│  sqlgrammar.py         │                                       │           │
│  (allowlist AST         │                                      │           │
│   compiler — every        │                                    │           │
│   authored SQL              │                                  │           │
│   fragment passes here)      │                                 │           │
│    │                          │                                │           │
│    ▼                          ▼                                ▼           │
│  duck.py — ONE process-wide DuckDB connection: S3 secrets, object cache,  │
│  external-file cache, source-listing/pin cache (app/cache.py)             │
└──────────┬──────────────────────────────────────────────┬───────────────--┘
           │ httpfs / delta / iceberg (projection +         │ sqlite3
           │ predicate pushdown)                            │ (single writer)
           ▼                                                 ▼
   S3 bucket(s): demo bucket (embedded moto server,    cash_intel.db: visuals,
   in-memory) + optional real bucket, side by side —   dashboards, publications,
   parquet / csv / Delta / Iceberg. Pipelines write     users/sessions/tokens/
   here too (replace/upsert).                            audit, conversations,
                                                          model memories, pipeline
                                                          run history, sandbox
                                                          notebooks, local
                                                          (non-git) models/
                                                          bundles/pipelines
```

Two things this diagram is trying to make visible:

- **Every read path funnels through `duck.py`'s one connection**, whichever
  router triggered it — a browser's `/api/query`, chat's re-validated
  proposal, or a pipeline/sandbox subprocess's own cursor. There is no second
  way to reach the bucket.
- **The LLM never runs a query.** Every LLM-backed surface (chat, MCP's
  `ask_question`, the Composer, the sandbox coding agent) produces a
  *proposal* — a typed, unvalidated tool call — that a plain-Python
  re-validation step checks against live server state before anything
  executes. See [Conversational Analytics](conversational-analytics.md) and
  [Agents & MCP](agents-and-mcp.md).

## Process model

**One process by default; N when you need them.** Everything below describes
the default, single-process shape — still what you get with no configuration,
and still the shape to run until a single container is saturated. Which of
these properties are *facts* and which are *assumptions* that stop holding at
two replicas — and what the code now does about the latter — is
[Scaling](scaling.md).

- **One DuckDB connection per process.**
  `app/duck.py` opens exactly one process-wide `duckdb.DuckDBPyConnection`;
  every caller gets a short-lived `cursor()` off it. This is load-bearing,
  not an optimization detail: DuckDB's parquet-metadata cache, external file
  cache and keep-alive HTTP connections all live on the *instance*, so a
  second connection starts cold and a connection-per-query makes every one
  of them useless (see [Query Engine](query-engine.md)). Note the scope:
  *per process*, not per deployment. DuckDB is embedded, so each replica is
  a complete, independent query engine — which is why scaling the read path
  out is nearly free, and why the cost that is real is N cold caches rather
  than any contention (see
  [Scaling §2](scaling.md#2-duckdb-the-process-is-the-query-engine)).
- **The embedded demo S3 server** (`app/emulator.py`, a `moto`
  `ThreadedMotoServer`) runs in-process on `127.0.0.1:9600` whenever the demo
  store's endpoint is that loopback address — the default, and still true
  even when a real bucket is also configured (see
  [Storage & Runtime](storage-and-runtime.md)).
- **Two kinds of subprocess do the platform's untrusted-reach work in
  isolation:** `python -m app.pipeline_runner` and
  `python -m app.sandbox_runner`. Neither module is ever imported into the
  main process — a runaway query or an infinite loop in admin-authored SQL
  can only take down a subprocess the parent kills on timeout, never the
  app itself. See [Pipelines](pipelines.md) and [Sandbox](sandbox.md).
- **Pipeline runs never overlap on a target.** A run *writes* a shared bucket
  path, so two runs of the same pipeline must not interleave. That used to be
  guaranteed by there being one FIFO worker thread — an argument that holds
  only for one process. The queue is now the `pipeline_runs` table itself: a
  run is claimed by one atomic UPDATE and holds a lease-backed lock named for
  its target while it executes (`app/pipeline_jobs.py`, `app/cluster.py`), so
  the guarantee reads the same with one worker or three. Sandbox runs are
  read-only, so they answer synchronously with no queue.
- **SQLite is a single-writer store**, `cash_intel.db`, holding everything
  the platform itself needs to remember — never business data. The bucket
  is the one and only source of truth for the data being analyzed. It opens
  in WAL mode with a busy timeout (`app/sqlitedb.py`), which is what lets one
  writer and several readers be several *processes* on one host; across hosts
  the store classes want Postgres behind them (see
  [Scaling §6](scaling.md#6-state-what-has-to-be-shared)).
- **Coordination between processes is explicit, and inert when there is only
  one.** `app/cluster.py` holds this process's role (`CI_ROLE`), the
  lease-based locks that replace "there is only one of me", and the change
  generations replicas watch each other with. Unclustered — the default — a
  lock is a `threading.Lock`, the generations are local integers, and no
  watcher thread starts.

## The module map

| Doc | Source files | What it covers |
|---|---|---|
| [Semantic Layer](semantic-layer.md) | `app/semantic.py`, `models/*.yaml`, `dimensions/*.yaml` | The YAML contract: models, dimensions, measures, joins, dimension bundles, multi-fact-table models, spines |
| [Query Engine](query-engine.md) | `app/engine.py`, `app/duck.py`, `app/sqlgrammar.py`, `app/extract.py`, `app/cache.py` | Semantic query → one SQL statement; the SQL allowlist; S3 latency tuning; instant-mode Arrow extracts |
| [Auth & Security](auth-and-security.md) | `app/auth.py`, `app/authstore.py`, `AuthMiddleware` in `app/main.py` | Identity, password hashing, sessions/tokens, roles, CSRF, the trust-boundary principle |
| [Storage & Runtime](storage-and-runtime.md) | `app/config.py`, `app/registry.py`, `app/store.py`, `app/s3.py`, `app/emulator.py`, `app/seed.py`, `app/local*store.py` | Env-driven config, the runtime registry, SQLite persistence, the demo/primary store split, seeding |
| [Pipelines](pipelines.md) | `app/pipelines.py`, `app/pipeline_runner.py`, `app/pipeline_jobs.py`, `app/pipelinestore.py`, `app/materialize.py` | Hosted SQL transformations: YAML shape, subprocess execution, the FIFO worker, materialization modes, lineage |
| [Sandbox](sandbox.md) | `app/sandbox.py`, `app/sandbox_runner.py`, `app/sandbox_agent.py`, `app/sandboxstore.py` | Scratch multi-cell SQL notebooks, the coding-agent seam, convert-to-pipeline |
| [Agents & MCP](agents-and-mcp.md) | `app/skills.py`, `app/agents.py`, `app/skills_analytics.py`, `app/mcpserver.py` | The Skill/Agent abstractions and the MCP server mounted at `/mcp` |
| [Conversational Analytics](conversational-analytics.md) | `app/llm.py`, `app/llmclient.py`, `app/nlq.py`, `app/composer.py`, `app/memorystore.py`, `app/conversationstore.py` | Chat's translate→re-validate→execute loop, the multi-provider LLM client, the Composer, self-learning model memories |
| [API Layer](api-layer.md) | `app/api/*.py` | Every HTTP route, grouped by router, with roles and request/response shapes |
| [Scaling & Deployment](scaling.md) | `app/cluster.py`, `app/clusterstore.py`, `app/sqlitedb.py`, `app/pipeline_jobs.py`, `deploy/` | What "single process" actually coupled, how each coupling is broken, and the manifests for running it horizontally scaled |
| [Frontend](frontend.md) | `app/static/js/*`, `app/static/*.css`, `design.md` | The no-build vanilla-JS architecture: router, state, chart dispatch, the design system |

## Data at rest

Two stores, deliberately kept apart:

- **The bucket** (S3 or S3-compatible) is the source of truth for business
  data — parquet, CSV, Delta and Iceberg — and the only thing pipelines
  write to. A **demo store** (the embedded emulator, or wherever
  `CI_DEMO_S3_ENDPOINT` points) always serves the seven built-in demo models;
  a **primary store** serves everything else. While `CI_BUCKET` is unset
  they're the same store — the zero-config path. The moment a real bucket is
  configured they separate, so the demo catalog and a paying account can
  never write into each other. Full detail in
  [Storage & Runtime](storage-and-runtime.md).
- **`cash_intel.db`** (SQLite, gitignored) holds everything the platform
  persists about itself: users/sessions/tokens/audit
  (`app/authstore.py`), visuals/dashboards/publications/notebooks/measure
  provenance (`app/store.py`), conversations (`app/conversationstore.py`),
  model memories (`app/memorystore.py`), pipeline run history
  (`app/pipelinestore.py`), sandbox notebooks (`app/sandboxstore.py`), and
  *locally-authored* models/bundles/pipelines that never touch the
  git-tracked `models/`/`dimensions/`/`pipelines/` directories
  (`app/local*store.py`).

## The trust boundary, summarized

The constitution's Principle VI ("Trusted-Config Security Boundary Is
Explicit, Never Silently Widened") is the load-bearing security decision in
this codebase, and every module above answers to it:

- **Structural YAML** (sources, joins, dimension declarations, pipeline
  target/materialization) is trusted, developer/admin-authored configuration
  — the same trust level as application code — and is gated by role where it
  can widen reach (raw model/pipeline YAML routes require **admin**).
- **Every authored *expression*** — a measure's `expr:`, its `from:` block, a
  chat-proposed inline measure — is parsed and re-serialized by
  `app/sqlgrammar.py`'s allowlisting compiler, regardless of who supplied it
  or whether it's saved or query-time-only. That structural incapability to
  run arbitrary code is what lets an unauthenticated visual author's inline
  measure be exactly as safe as a saved model measure — see
  [Query Engine](query-engine.md#the-sql-grammar).
- **Pipeline `sql:` and sandbox cells** are the one place table functions
  (and therefore arbitrary bucket I/O) are reachable. That reach — not code
  execution, which SQL doesn't offer — is exactly what the **admin** gate on
  authoring *and running* both measures. Process isolation (a killable
  subprocess) is a crash/timeout safety net on top, not the trust boundary
  itself.

Full detail, including the session/role/CSRF model, is in
[Auth & Security](auth-and-security.md).

## Deployment topology

- **A single Docker image** (`python:3.12-slim`), built once. One container
  is the default and the right size until it is saturated; `CI_ROLE` splits
  the same image into `web` (serves HTTP, scale to N) and `worker` (drains
  the pipeline queue) when it is not. Manifests for both — Kubernetes,
  ECS/Fargate, and a runnable local three-replica compose profile — are in
  [`deploy/`](../deploy/README.md), and the reasoning is in
  [Scaling](scaling.md).
- **Scaling out requires an external S3 endpoint** (real AWS, or a shared
  MinIO/LocalStack), never the embedded emulator: it is in-memory and
  per-process, so each replica would serve its own demo bucket and answer the
  same query differently. A clustered process refuses to start on that
  configuration rather than doing it quietly (`app/cluster.py`'s preflight),
  along with a `CI_DUCKDB_PATH` pointing at a file, which two replicas cannot
  share.
- **State lives outside the image**: the SQLite volume (`/data`) and the
  host-mounted `models/`, `dimensions/`, `pipelines/` directories, which
  `docker-compose.yml` bind-mounts so editing a YAML file on the host takes
  effect on the next reload with no rebuild.
- **DuckDB extensions ship as pinned wheels**
  (`duckdb-extension-httpfs`/`delta`/`iceberg`/`avro` in `requirements.txt`),
  loaded from disk by `app/duck.py`, never installed over the network at
  runtime — the Docker build asserts every one of them loads
  (`RUN python -c "... duck.loaded_extensions() ..."`), so a wheel/engine
  version mismatch fails the build instead of failing silently in
  production.
- **Two Compose profiles** beyond the default (demo-mode) service: `minio`
  stands up a MinIO container and a second app instance pointed at it, for
  exercising the "real S3-compatible endpoint" path locally.

## Testing

The `tests/` suite (pytest, ~14.8k lines) exercises semantic-model parsing,
engine behavior against a real moto-emulated bucket (not mocks), SQLite
store CRUD, the full API surface via FastAPI's `TestClient`, pipelines, the
SQL grammar's allowlist, and the auth/role matrix
(`tests/test_role_matrix.py` pins every route's minimum role against
`specs/011-session-auth-rbac/contracts/auth-api.md`). The project's
constitution (`.specify/memory/constitution.md`, Principle III) requires
every feature to ship with tests alongside it — a bug found in manual
verification gets a regression test, not just a fix.

## Where the deep detail lives

The `specs/NNN-feature-name/` directories hold the design history behind
each major feature — spec, plan, data model, and (where applicable) research
notes and API contracts — for anyone who wants the *why* behind a decision
this page or its linked pages only state as a conclusion. `README.md` at the
repo root is now a short pointer to this documentation set; start there for
how to run the app, and here for how it's built.
