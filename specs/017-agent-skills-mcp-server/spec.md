# Feature Specification: Agent & Skills Framework with MCP Server

**Feature Branch**: `017-agent-skills-mcp-server`

**Created**: 2026-08-04

**Status**: Draft

**Input**: User description: "Agentic architecture for the platform: a first-class concept of an "Agent" with declared Skills/Tools it can use, generalizing the ad hoc tool-calling patterns already used by the conversational analytics translator (app/nlq.py + app/llm.py) and the sandbox coding agent (app/sandbox_agent.py). On top of that, add an MCP (Model Context Protocol) server so external MCP-compatible clients (e.g. Claude Desktop, Claude Code, other agent hosts) can access the platform's agents/skills over the MCP protocol, reusing the platform's existing session/token auth and RBAC (viewer/author/admin) rather than inventing a new auth scheme."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Query the BI platform from an external AI tool (Priority: P1)

An analyst using an external MCP-compatible AI client (e.g. Claude Desktop, Claude Code, or another agent host) connects to the platform and asks a business question in plain language ("what was Q2 revenue by region?"). The client discovers and invokes a query skill; the platform resolves the question against the declared semantic layer — the same catalog and rules already enforced for the embedded chat — and returns structured results to the external client, without the analyst opening the platform's browser UI.

**Why this priority**: This is the reason the feature exists — bringing the platform's data access into the AI tools people already work in, extending the value the embedded conversational analytics already proves out to any MCP-capable client.

**Independent Test**: Connect a real MCP client to the server with valid credentials, ask a natural-language question answerable from an existing declared model, and confirm structured, correct results come back — fully testable without either of the other stories.

**Acceptance Scenarios**:

1. **Given** an authenticated MCP connection with viewer role, **When** the client asks a question answerable from a declared model, **Then** it receives the resolved query and its data, consistent with what the embedded chat returns for the same question.
2. **Given** an authenticated MCP connection, **When** the client asks a question ambiguous between two models/dimensions/measures, **Then** it receives a clarification request naming the real candidates, never a guess.
3. **Given** an authenticated MCP connection, **When** the client asks something the semantic layer cannot answer (needs a raw column, an undeclared join, or arbitrary code), **Then** it receives a clear decline explaining why — never raw data, a fabricated answer, or code execution.

---

### User Story 2 - Discover what's available before asking (Priority: P2)

Before asking a question, an external client lists the Agents/Skills available to it and, for the query skill, the catalog of models/dimensions/measures it is permitted to query — so an agent host can decide what it's capable of asking, and a developer configuring a new MCP client can see what's exposed.

**Why this priority**: Discovery is what makes the protocol self-describing and is required for any real MCP client to function usefully in practice, but the platform is already valuable with only Story 1 if the client happens to know what to ask in advance.

**Independent Test**: Connect with credentials at each role tier and confirm the listed Agents/Skills and catalog contents differ according to role, without invoking any skill.

**Acceptance Scenarios**:

1. **Given** an authenticated connection, **When** the client requests the list of available skills, **Then** only skills whose required role the identity satisfies are listed.
2. **Given** a connection authenticated as a lower-privilege identity, **When** it requests the skill list, **Then** higher-privilege-only skills are absent from the list entirely, not merely rejected if invoked.

---

### User Story 3 - Govern which skills an agent may use, declaratively (Priority: P3)

A platform admin defines or edits which skills a given agent is allowed to use — analogous to editing a model YAML today — without changing application code, so the capabilities reachable through a given agent can be tuned per deployment as new skills are added.

**Why this priority**: Valuable for operating the platform safely as it grows, but the platform delivers its core value (Stories 1-2) even with a fixed, code-defined agent/skill set on day one.

**Independent Test**: Change an agent's declared skill list and, after a reload, confirm the MCP server's exposed capability set changes accordingly, without a code change.

**Acceptance Scenarios**:

1. **Given** an agent's declared skill list is edited to remove a skill, **When** the platform reloads the declaration, **Then** external clients using that agent can no longer see or invoke the removed skill.

---

### Edge Cases

- What happens when an MCP client attempts to connect without valid credentials? (Must be rejected the same way an unauthenticated API request is today — no anonymous access.)
- What happens when an MCP client's authenticated role is insufficient for a skill it tries to invoke directly (bypassing discovery)? (Must be declined with a clear reason, mirroring existing role-enforcement responses — never silently succeed because discovery was skipped.)
- What happens when a skill's input describes a model, dimension, or measure that no longer exists or was renamed since the client last saw the catalog? (Must be re-validated against the live semantic model and rejected with a clear reason, never executed against stale assumptions.)
- How does the system behave when multiple external clients hold concurrent MCP sessions at once?
- What happens when an external client disconnects or abandons the connection mid-invocation?
- What happens when a skill depends on a capability that is itself off by configuration (e.g. no LLM API key configured, mirroring today's conversational analytics)? (The MCP surface must reflect that the capability is unavailable, not behave as if it silently works.)
- What happens when an invoked skill's underlying query would return a very large result? (Must be bounded the same way existing query paths already bound result size.)
- What happens when an identity exceeds its rate limit on LLM-backed skill invocations? (Must receive a clear, typed rejection identifying the limit, not a hang, silent drop, or generic error.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The platform MUST provide a declared concept of an "Agent" — a named, described bundle of the Skills it is permitted to use — analogous to how a model YAML declares the dimensions/measures it exposes.
- **FR-002**: The platform MUST provide a catalog of "Skills," each with a name, description, typed input, typed output, and a minimum platform role (viewer/author/admin) required to invoke it.
- **FR-003**: The platform MUST expose declared Agents and their Skills over an MCP server so external MCP-compatible clients can discover and invoke them.
- **FR-004**: Every MCP client connection MUST authenticate using the platform's existing identity system (session or per-user API token) — no new or parallel auth scheme is introduced.
- **FR-005**: A skill MUST be visible and invokable to an MCP client only when the connection's authenticated identity role meets or exceeds that skill's required role, mirroring existing RBAC enforcement exactly (viewer/author/admin).
- **FR-006**: The existing conversational-analytics translator (propose query / ask clarification / decline / show last query) MUST be re-expressed as Skills under this framework rather than continuing to exist as a separate, undeclared tool-calling implementation.
- **FR-007**: Every skill invocation MUST be re-validated against the live semantic model and platform state before it executes — a client-declared or LLM-declared input is never trusted outright, exactly as today's query re-validation already works.
- **FR-008**: Every skill invocation MUST be attributable after the fact to the authenticated identity that made it, at the same fidelity as the platform's existing audit logging for authored actions.
- **FR-009**: The platform MUST provide a discovery capability so a connecting client can list the Agents/Skills available to it and their schemas before invoking any of them.
- **FR-010**: The first Agent built on this framework MUST be the existing conversational-analytics capability, re-expressed as Skills (see FR-006). The sandbox coding agent is explicitly OUT of scope for this feature and MUST NOT be reachable through the MCP server — admin-only, unsandboxed code execution stays exactly as gated as it is today (Constitution Principle VI is not reopened by this feature).
- **FR-011**: This feature MUST expose read/query skills only — discovery, question-answering/query-proposal, and catalog listing. Skills that persist state or trigger side effects (saving a visual or dashboard, triggering a pipeline run, authoring a model measure) MUST NOT be exposed via MCP in this feature; they are explicitly deferred to a future increment.
- **FR-012**: The MCP server MUST be reachable as a remote, multi-tenant service hosted alongside the platform's existing web application (the same deployment, not a separate per-user local process), so multiple external clients can hold concurrent authenticated connections the same way multiple browser/API clients do today.
- **FR-013**: The platform MUST enforce a per-identity rate limit on skill invocations that call the LLM backend, so a single session or token cannot drive unbounded Anthropic API cost or load through the new external MCP surface; a caller exceeding the limit MUST receive a clear, typed rejection rather than a silent delay or failure.

### Key Entities

- **Agent**: A named, described bundle of Skills a client can address as a unit (e.g. "Analytics Agent"). Carries no privilege of its own beyond the calling identity's role — it does not let a caller reach a skill their own role wouldn't otherwise permit.
- **Skill**: One typed, invokable capability (e.g. "propose a query," "list models," "ask a clarifying question"). Has a name, description, input/output shape, and a minimum required role. Generalizes the existing tool-calling patterns in the conversational-analytics translator and the sandbox coding agent into one reusable shape.
- **Skill Invocation**: A single call from an external client to a skill, attributable to an authenticated identity, with a re-validated outcome (success, clarification, or decline) — the audit unit for this feature.
- **MCP Connection**: An authenticated session between an external MCP client and the platform, scoped to the role of the identity that authenticated it.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An external AI client can obtain a correct answer to a business question against the platform's data, entirely from outside the platform's browser UI, at latency comparable to the embedded conversational analytics chat today.
- **SC-002**: 100% of skill invocations are enforced against the caller's actual role — no capability is reachable through an external MCP client that the same identity could not already reach through the existing authenticated web UI or API.
- **SC-003**: A new external client can go from holding valid credentials to successfully listing available capabilities and completing one query using only standard MCP client configuration — no custom integration code.
- **SC-004**: Every skill invocation can be traced after the fact to the identity that made it, with the same fidelity as the platform's existing audited actions.
- **SC-005**: Adding a new skill to an existing agent's declaration, or removing one, changes what an external client can discover and invoke without any code change to the agent framework itself.
- **SC-006**: No single identity can drive unbounded LLM-backed skill invocations through the MCP surface — a caller that exceeds its rate limit is stopped with a clear rejection, not served indefinitely.

## Assumptions

- Reuses the platform's existing session/token identity system and viewer/author/admin roles; this feature does not introduce a new authentication or authorization scheme.
- The MVP re-platforms conversational analytics (the existing query-translation capability) as the first Agent built on this framework, since it is already shaped as a bounded, typed tool-calling flow. The sandbox coding agent is explicitly excluded from this feature (see Clarifications) — its admin-only, unsandboxed execution model stays untouched, and reaching it via MCP would require deliberately reopening Constitution Principle VI in a dedicated future feature, not as a side effect of this one.
- A skill's output is the same class of structured result the platform's existing query paths already return — this feature does not introduce a new response shape for query data.
- The MCP server is additive: the existing browser UI and REST API remain the platform's primary interfaces and are unchanged by this feature.
- The feature targets the platform's existing deployment model (single Docker image, single uvicorn worker) — no new scaling assumption is introduced.

## Clarifications

### Session 2026-08-04

- Q: Which existing capability becomes the first Agent/Skills implementation, and is the sandbox coding agent (admin-only, unsandboxed code execution) part of what an external MCP client can reach in this feature? → A: Conversational analytics only. The sandbox coding agent is out of scope for this feature and stays unreachable via MCP; extending it there would require a dedicated future feature that explicitly reopens Constitution Principle VI.
- Q: Does this feature expose only read/query skills, or also mutating skills (saving a visual, triggering a pipeline run, authoring a model measure)? → A: Read/query-only. Mutating skills (save, trigger, author) are explicitly deferred to a future increment.
- Q: What is the MCP server's connection/deployment shape — a local process an external client launches per user (stdio), a remote multi-tenant server alongside the existing FastAPI app (HTTP/SSE), or both? → A: Remote, multi-tenant, hosted alongside the existing FastAPI app — the same deployment, authenticating each connection like any other API caller.
- Q: LLM-backed skills (query proposal) call the Anthropic API per invocation; opening that to external MCP clients is a new surface for repeated/abusive calls to run up cost. Should this feature add per-identity rate limiting on LLM-backed skill invocations? → A: Yes — add a per-identity (session/token) rate limit on LLM-backed skill invocations as part of this feature.
