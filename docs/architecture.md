# Solution architecture

CASH_INTELLIGENCE is a single FastAPI service (one container, one process) that
turns files already sitting in S3 into a governed BI layer: a YAML semantic
layer describes sources/joins/dimensions/measures, DuckDB compiles each
request into one statement and reads the bucket in place (only the columns
and row groups a query needs leave S3), and everything a person authors —
measures, pipelines, sandbox notebooks — is SQL, parsed and allowlisted before
it ever reaches a connection. State that must survive a restart (visuals,
dashboards, users/sessions, pipeline run history, saved notebooks) lives in a
single SQLite file on a mounted volume; state that shouldn't (query results)
never touches disk at all.

This doc is a deeper, diagrammed companion to the ASCII sketch and
[Project layout](../README.md#project-layout) in the README — read those
first for the byte-level detail; this is the shape.

## Components

```mermaid
flowchart TB
    Browser["Browser SPA (vanilla JS ES modules)<br/>Studio · Portal · Modelling · Dashboards<br/>Chat · Composer · Sandbox · Account"]

    subgraph FastAPI["FastAPI app — app/main.py"]
        direction TB
        AuthMW["AuthMiddleware<br/>cookie session or Bearer token<br/>default-deny; viewer/author/admin RBAC"]
        ApiRouters["/api/* routers<br/>auth · query · models · dimensions · visuals<br/>dashboards · pipelines · sandbox · chat · composer · explorer"]
        McpServer["/mcp — MCP server<br/>tools/list · tools/call over Streamable HTTP"]
        AuthMW --> ApiRouters
        AuthMW --> McpServer
    end

    subgraph Registry["Registry — app/registry.py (in-memory, hot-reloadable)"]
        direction TB
        Models["Semantic models<br/>models/*.yaml"]
        DimBundles["Dimension bundles<br/>dimensions/*.yaml"]
        PipelineDefs["Pipelines & layers<br/>pipelines/*.yaml"]
        Agents["Agents<br/>agents/*.yaml"]
    end

    subgraph Engine["Query engine"]
        direction TB
        Semantic["semantic.py<br/>YAML → Model / Dimension / Measure / Join"]
        EngineCore["engine.py<br/>semantic query → one DuckDB statement"]
        SqlGrammar["sqlgrammar.py<br/>parse → allowlist → re-serialize"]
        Semantic --> EngineCore
        SqlGrammar --> EngineCore
    end

    subgraph Duck["DuckDB runtime — duck.py"]
        DuckConn["one process-wide connection<br/>httpfs · avro · iceberg · delta<br/>listing cache · pinned-source cache"]
    end

    subgraph Object["Object storage"]
        direction TB
        S3Real["Your S3 bucket (AWS, by region)"]
        S3Demo["Embedded moto emulator<br/>(seeded demo bucket)"]
        MinIO["MinIO (docker compose --profile minio)"]
    end

    subgraph Sqlite["SQLite — /data/cash_intel.db"]
        direction TB
        VisualStore["VisualStore<br/>visuals · dashboards · publications"]
        AuthStore["AuthStore<br/>users · sessions · tokens · audit"]
        ConvMemStore["Conversation · Memory stores"]
        PipelineStore["PipelineStore<br/>run history"]
        SandboxStore["SandboxStore<br/>saved notebooks"]
        LocalStores["Local model / bundle / pipeline stores<br/>(in-app editing)"]
    end

    subgraph Workers["Background workers — one subprocess run at a time"]
        direction TB
        PipelineJobs["pipeline_jobs.py<br/>FIFO run worker (thread)"]
        PipelineRunner["pipeline_runner.py<br/>subprocess: runs pipeline SQL"]
        Materialize["materialize.py<br/>replace / upsert + delete handling"]
        SandboxRunner["sandbox_runner.py<br/>subprocess: runs notebook cells"]
        PipelineJobs --> PipelineRunner --> Materialize
    end

    subgraph LLM["Conversational analytics — optional, needs CI_LLM_API_KEY"]
        direction TB
        LlmClient["llmclient.py<br/>provider-neutral wire adapter"]
        Nlq["nlq.py / llm.py — chat"]
        ComposerMod["composer.py — notebook composer"]
        SandboxAgent["sandbox_agent.py — sandbox coding agent"]
        External["Anthropic · Bedrock · OpenAI · Azure<br/>or any compatible endpoint"]
        Nlq --> LlmClient
        ComposerMod --> LlmClient
        SandboxAgent --> LlmClient
        LlmClient --> External
    end

    Browser -->|HTTPS| AuthMW
    ApiRouters --> Registry
    ApiRouters --> EngineCore
    ApiRouters --> Sqlite
    ApiRouters --> PipelineJobs
    ApiRouters --> SandboxRunner
    ApiRouters --> LLM
    McpServer --> Agents
    McpServer -->|invoke_skill, same RBAC| ApiRouters

    EngineCore --> DuckConn
    PipelineRunner --> DuckConn
    SandboxRunner --> DuckConn
    SandboxAgent --> DuckConn

    DuckConn -->|predicate + projection pushdown| Object
    Materialize -->|writes new objects| Object
```

**Why one DuckDB connection.** Parquet-metadata caching, DuckDB's external
file cache and its keep-alive HTTP connections to S3 all live on the
*instance*, not the query — a connection per request would start cold every
time, so every reader (the query engine, pipeline runs, sandbox cells, the
sandbox agent) takes a short-lived cursor off one process-wide connection
instead (`app/duck.py`).

**Why one SQLite writer.** The demo bucket's S3 emulator and the SQLite store
both assume a single writer, which is why the Docker image runs one uvicorn
worker by design (`Dockerfile`) — scale out only against an external S3
endpoint, not by adding workers to this container.

**Why a subprocess for pipelines and sandbox runs.** A Python thread can't be
killed; a runaway or infinite-loop query can. Both run as a short-lived
`python -m app.<runner>` subprocess so a hard timeout is a real kill, and a
bad run can never take the FastAPI process down with it.

## Request flow: a query

```mermaid
sequenceDiagram
    autonumber
    actor U as Browser (Studio/Dashboard)
    participant MW as AuthMiddleware
    participant R as POST /api/query
    participant E as engine.py
    participant G as sqlgrammar.py
    participant D as DuckDB (duck.py)
    participant S3 as S3 / object storage

    U->>MW: {model, dimensions, measures, filters, sort, limit}<br/>cookie session or Bearer token
    MW->>MW: resolve identity — CSRF header required for cookie mutations
    MW->>R: request.state.user set
    R->>E: run_query(model, body)
    E->>G: validate + re-serialize each measure/filter expression
    G-->>E: allowlisted AST → SQL fragment (author's text never embedded raw)
    E->>D: one compiled statement (CTEs + joins)
    D->>S3: read only the needed columns / row groups (httpfs)
    S3-->>D: parquet / csv / delta / iceberg data
    D-->>E: aggregated rows
    E-->>R: result rows
    R-->>U: JSON response
```

## Request flow: a pipeline run

```mermaid
sequenceDiagram
    autonumber
    actor A as Admin (Pipelines UI)
    participant R as POST /api/pipelines/:name/run
    participant PJ as pipeline_jobs.py (FIFO worker)
    participant PR as pipeline_runner.py (subprocess)
    participant D as DuckDB
    participant M as materialize.py
    participant S3 as S3 bucket
    participant PS as PipelineStore (sqlite)

    A->>R: trigger run
    R->>PJ: enqueue
    PJ->>PR: spawn subprocess, JSON job spec on stdin
    PR->>D: run the pipeline's SQL against views over its sources
    D-->>PR: result rows
    PR->>M: materialize(result, write_options)
    M->>S3: replace / upsert (+ delete handling)
    PR-->>PJ: one JSON result line on stdout
    PJ->>PS: record run (status, rows, lineage synced onto the target model)
```

## Deployment topologies

All four run the same image (`Dockerfile`) with different env/volumes —
nothing about the code path changes between them:

| Topology | Command | Object storage |
|---|---|---|
| Local demo | `docker compose up` | embedded moto emulator only |
| Demo + your bucket | `CI_BUCKET=my-lake docker compose up` | emulator (demo models) + your AWS bucket, side by side |
| Demo + MinIO | `docker compose --profile minio up` | MinIO container, second app instance on :8081 |
| No Docker | `./run.sh` | same env vars, read from `.env` |

`models/`, `dimensions/` and `pipelines/` are bind-mounted from the host in
every Docker topology, so editing a YAML file — or saving one from the
in-app editor, which writes through the same `local_*` SQLite stores — is
picked up on the next `Registry.reload_all()` with no rebuild.

## Security posture that shapes the diagram

- **Default-deny at the edge.** `AuthMiddleware` guards every `/api` and
  `/mcp` route up front; a route can't opt out by forgetting a dependency,
  and the MCP handshake itself requires a valid identity — there is no
  anonymous entry point.
- **No SQL passthrough, anywhere authoring happens.** Measures, pipeline SQL
  and sandbox cells are parsed into an AST and checked against a fail-closed
  allowlist (`sqlgrammar.py`); the author's literal text never reaches the
  DuckDB connection. Table functions (`read_parquet`, `delta_scan`, …) are
  structurally unreachable from a measure — only an admin-authored pipeline
  can name a bucket path directly.
- **Credentials never cross the emulator boundary.** The demo bucket's S3
  emulator uses its own dummy key pair; a real bucket's credentials
  (`AWS_PROFILE`/SSO, static keys, or the instance/task role) are scoped to
  that bucket alone.
