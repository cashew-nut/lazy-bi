# CASH_INTELLIGENCE

Lightweight BI over data files in S3. DuckDB reads parquet, CSV, Delta Lake
and Iceberg sources **in place** — only the columns and row groups a query
needs leave the bucket — aggregates them, and returns results to a
cyberpunk query-builder UI. A YAML **semantic layer** is the only contract
the UI, API, and any LLM ever see: sources, joins, dimensions and measures
are declared once and never bypassed. **Pipelines** materialize new sources
into the bucket with full lineage; a **Sandbox** gives you scratch SQL
notebooks and a coding agent; **Chat** answers business questions by
proposing — and always re-validating — a semantic query, never raw SQL.

Everything a person or an LLM authors is SQL, and every fragment of it is
parsed, allowlisted, and re-serialized from a validated AST before it can
reach a database connection — there is no `eval` anywhere in the query or
measure path.

## Documentation

**Start with [docs/architecture.md](docs/architecture.md)** for how the
system fits together, then follow the page for whichever part you're
touching:

| | |
|---|---|
| [Solution Architecture](docs/architecture.md) | Request flow, process model, the trust boundary, deployment |
| [Semantic Layer](docs/semantic-layer.md) | The YAML contract: models, dimensions, measures, joins, bundles |
| [Query Engine](docs/query-engine.md) | Semantic query → SQL, the SQL grammar, S3 latency tuning, instant mode |
| [Auth & Security](docs/auth-and-security.md) | Identity, sessions, tokens, roles, CSRF, the trust boundary |
| [Storage & Runtime](docs/storage-and-runtime.md) | Configuration, the runtime registry, SQLite persistence |
| [Pipelines](docs/pipelines.md) | Hosted SQL transformations, materialization, lineage |
| [Sandbox](docs/sandbox.md) | Scratch SQL notebooks and the coding agent |
| [Agents & MCP](docs/agents-and-mcp.md) | The Skill/Agent abstractions and the MCP server at `/mcp` |
| [Conversational Analytics](docs/conversational-analytics.md) | Chat, the multi-provider LLM client, the Composer |
| [API Layer](docs/api-layer.md) | Every HTTP route, grouped by router |
| [Frontend](docs/frontend.md) | The no-build vanilla-JS architecture and design system |

Design history behind individual features (spec, plan, data model,
research) lives in `specs/NNN-feature-name/`; project principles are in
`.specify/memory/constitution.md`.

## Run the demo

**Docker (recommended):**

```bash
docker compose up                        # demo mode on http://127.0.0.1:8080
CI_BUCKET=my-lake docker compose up      # + your own bucket, demo still on
docker compose --profile minio up        # + MinIO-backed instance on :8081
```

The default service seeds an in-process S3 emulator on first start with a
demo dataset per supported format (parquet, csv, Delta, Iceberg). Point
`CI_BUCKET` at a real bucket to read your own data alongside the demo — see
[Storage & Runtime](docs/storage-and-runtime.md) for the full
credential/endpoint reference, and `.env.example` for every setting with
inline documentation.

**Local (no Docker):**

```bash
python3 -m venv .venv          # Python 3.10+
.venv/bin/pip install -r requirements.txt
cp .env.example .env           # optional: LLM settings/secrets, gitignored
./run.sh                       # or: .venv/bin/uvicorn app.main:app --port 8080
```

**Tests:**

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/    # ~90s: sql grammar, semantic, engine, store, API
```

**Signing in:** everything requires an account — there's no anonymous mode.
On first start with no accounts, a bootstrap `admin` is created and its
random password is printed **once** in the startup log; sign in with it,
then create your own accounts under **ACCOUNT**. See
[Auth & Security](docs/auth-and-security.md) for roles, sessions, and
personal access tokens.

**Conversational analytics, the Composer, and the sandbox coding agent**
are off until `CI_LLM_API_KEY` is set (copy `.env.example` to `.env` and
fill it in). Any OpenAI- or Anthropic-compatible endpoint works — see
[Conversational Analytics](docs/conversational-analytics.md) for the
provider list and exactly what each surface sends once configured.

## Project layout

```
app/            FastAPI backend — see the docs pages above for the module map
  api/          one router per resource (app/api/*.py)
  static/       frontend: vanilla ES modules, hand-rolled SVG charts, no build step
models/*.yaml           semantic models (the editable contract)
dimensions/*.yaml       dimension bundles shared across models
pipelines/*.yaml        hosted SQL transformations
agents/*.yaml           declared Agents for the MCP server
tests/                  pytest: semantic, engine, store, API, pipelines
benchmarks/             the latency harness behind docs/query-engine.md's numbers
docs/                   the documentation set linked above
specs/                  per-feature design history (spec/plan/data-model)
Dockerfile, docker-compose.yml
```
