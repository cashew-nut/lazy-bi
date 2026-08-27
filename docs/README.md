# Documentation

Start with [**Solution Architecture**](architecture.md) — it maps how the
whole system fits together and links out to every page below.

| Page | Covers |
|---|---|
| [Solution Architecture](architecture.md) | Request flow, process model, the trust boundary, deployment topology — start here |
| [Semantic Layer](semantic-layer.md) | The YAML contract: models, dimensions, measures, joins, dimension bundles |
| [Query Engine](query-engine.md) | Semantic query → one SQL statement, the SQL allowlist, S3 latency tuning, instant-mode extracts |
| [Auth & Security](auth-and-security.md) | Identity, sessions, tokens, roles, CSRF, the trust-boundary principle |
| [Storage & Runtime](storage-and-runtime.md) | Configuration, the runtime registry, SQLite persistence, the demo/primary store split |
| [Pipelines](pipelines.md) | Hosted SQL transformations: execution, materialization, lineage |
| [Sandbox](sandbox.md) | Scratch SQL notebooks and the coding agent |
| [Agents & MCP](agents-and-mcp.md) | The Skill/Agent abstractions and the MCP server at `/mcp` |
| [Conversational Analytics](conversational-analytics.md) | Chat, the multi-provider LLM client, the Composer, self-learning model memories |
| [API Layer](api-layer.md) | Every HTTP route, grouped by router |
| [Frontend](frontend.md) | The no-build vanilla-JS architecture and design system |

Design-history detail behind individual features lives in
`specs/NNN-feature-name/` at the repo root (spec, plan, data model,
research notes); these pages are the current-state reference, not the
history.
