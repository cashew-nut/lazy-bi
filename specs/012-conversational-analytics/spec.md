# Feature Specification: Conversational Analytics

**Feature Branch**: `012-conversational-analytics`

**Created**: 2026-07-13

**Status**: Draft

**Input**: User description: "Conversational analytics over the semantic model: let a user ask natural-language questions (e.g. \"what were sales by region last quarter\") in a chat interface and get back an answer grounded in the existing semantic layer (models/*.yaml) and the existing POST /api/query engine — dimensions, measures, filters, sort, limit. The assistant translates the question into a valid semantic query against a chosen model, runs it through the existing query engine (no new data access path, no bypassing the semantic layer), and returns a natural-language answer plus the underlying result (table/number) so the user can see what was actually queried. Should support follow-up/clarifying questions when the model, dimension, or measure is ambiguous, and should refuse or explain when a question can't be answered from the declared semantic model. Scope is Q&A / conversational querying only — building new saved visuals or dashboards from a prompt is a separate future feature, and this feature should be built so that future prompt-to-dashboard and dashboard-analyst features can reuse its NL-to-query core later. Respect existing session auth/RBAC — conversational queries run with the caller's own role/permissions, not an elevated one."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Ask a plain-language question and get a grounded answer (Priority: P1)

A signed-in user types a business question in plain language (e.g. "what were sales by region last quarter") into a chat interface. The assistant identifies the relevant semantic model, dimensions, measures, and filters, runs the equivalent query through the existing query engine, and replies with a short natural-language answer plus the exact table/number the answer is based on.

**Why this priority**: This is the core value of the feature — without it there is no conversational analytics capability at all. Every other story is a refinement of this loop.

**Independent Test**: Sign in as a viewer, ask a question that maps cleanly to one declared model/dimension/measure combination, and confirm the chat reply's stated answer matches the returned table/number, and that the same result can be reproduced by building the equivalent query by hand in the existing query builder.

**Acceptance Scenarios**:

1. **Given** a signed-in user on the chat interface, **When** they ask a question that maps unambiguously to one model's declared dimensions/measures, **Then** the assistant returns a natural-language answer and the underlying result table/number, both derived from a single call to the existing query engine.
2. **Given** a returned answer, **When** the user inspects it, **Then** they can see which model, dimensions, measures, filters, and sort/limit were actually used to produce it.
3. **Given** two different users with different roles, **When** they ask the same question, **Then** each receives results scoped to what their own role is permitted to see (no elevation of privilege via the chat path).

---

### User Story 2 - Ask a follow-up question that refines the previous one (Priority: P2)

A user asks a first question, gets an answer, and then asks a short follow-up ("now break that down by month" or "just the top 5") without repeating the full context. The assistant carries forward the model and relevant prior context and produces an updated, still-grounded answer.

**Why this priority**: Real analytical exploration is iterative; a one-shot Q&A tool without follow-up support would feel like the existing query builder with extra steps. This is what makes it "conversational."

**Independent Test**: Ask an initial question, then a follow-up that only makes sense in light of the first (e.g., "and last quarter?"), and confirm the second answer correctly reuses the first question's model/dimensions/measures with the follow-up's adjustment applied.

**Acceptance Scenarios**:

1. **Given** a prior answered question in the same conversation, **When** the user sends a short follow-up that references it implicitly, **Then** the assistant resolves the reference using the prior turn's model/dimensions/measures/filters and returns a correctly adjusted answer.
2. **Given** a follow-up that changes the topic entirely (unrelated to the prior question), **When** the user sends it, **Then** the assistant treats it as a new question rather than forcing an incorrect reuse of prior context.

---

### User Story 3 - Assistant asks a clarifying question instead of guessing wrong (Priority: P2)

A user asks a question that is genuinely ambiguous — it could map to more than one model, or a term in the question could refer to more than one declared dimension or measure. Rather than silently picking one interpretation, the assistant asks a short clarifying question and waits for the user's choice before running any query.

**Why this priority**: A wrong-but-confident answer is worse than no answer for a BI tool — it erodes trust in every subsequent answer. This is essential to the feature being safe to rely on, but the product is still usable without it if most questions are unambiguous in the demo models.

**Independent Test**: Ask a question using a term that matches dimensions/measures in more than one declared model (or more than one measure within a model), and confirm the assistant asks a specific clarifying question naming the candidates rather than guessing, and that answering the clarification produces the expected grounded result.

**Acceptance Scenarios**:

1. **Given** a question whose terms match declared entities in more than one model, **When** the user submits it, **Then** the assistant asks which model/entity was meant, presenting the real candidate names, before running any query.
2. **Given** a clarifying question was asked, **When** the user answers it, **Then** the assistant proceeds using that answer and does not re-ask the same clarification.

---

### User Story 4 - Assistant declines what the semantic layer can't answer (Priority: P1)

A user asks something that cannot be expressed as a semantic query — it needs a raw column that isn't declared as a dimension or measure in any model, arbitrary computation outside the declared measures, or data the semantic layer simply doesn't have. The assistant explains what it can't do and why, without attempting a raw data-access shortcut.

**Why this priority**: This is the safety boundary for the whole feature. Since the assistant runs with the caller's real permissions and only through the declared semantic layer, a clean, honest refusal is what keeps the feature inside the product's existing trust model (only declared dimensions/measures are ever queryable — see the project's semantic-layer principle).

**Independent Test**: Ask for something that requires a column or computation not declared in any model (e.g. a raw source field never exposed as a dimension), and confirm the assistant refuses with a specific, honest explanation instead of returning a fabricated or under-the-hood raw-query answer.

**Acceptance Scenarios**:

1. **Given** a question that has no mapping to any declared dimension/measure/model, **When** the user submits it, **Then** the assistant states plainly that it can't answer from the available semantic models, without executing any query outside the declared semantic layer.
2. **Given** a question that asks the assistant to run arbitrary code, SQL, or bypass the semantic layer, **When** the user submits it, **Then** the assistant refuses and does not execute it.
3. **Given** a user without permission to query a given model, **When** they ask a question that would require that model, **Then** the assistant declines the same way the existing query builder would for that role, without leaking the model's existence/schema beyond what that role can already see.

---

### Edge Cases

- What happens when a question would require joining data across two models that have no declared join between them? The assistant must decline (or offer the closest single-model answer) rather than fabricate a join.
- What happens when the underlying query engine call fails or times out (e.g., malformed generated query, transient data-source error)? The user sees a clear failure message tied to that turn, not a fabricated answer, and can retry.
- What happens when a question maps correctly but returns an empty result set (e.g., filters exclude all rows)? The assistant reports that the query ran successfully and returned no matching data, distinct from a refusal.
- What happens when a user pastes an extremely long or off-topic message (not a business question at all)? The assistant declines to treat it as a query rather than guessing at a semantic mapping.
- What happens when a follow-up question's implied context (model/dimensions/measures from a prior turn) no longer applies because the model changed on the server since the conversation began? The assistant re-validates against the current model before running the query and surfaces a clarification if the prior context is now invalid.
- What happens when a viewer-role user's follow-up would require a role they don't hold (e.g. the underlying model became admin-only)? The assistant declines the same way the direct query path would.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide a chat-style interface where a signed-in user can submit natural-language questions and receive natural-language answers.
- **FR-002**: System MUST translate each in-scope question into a semantic query (model, dimensions, measures, filters, sort, limit) expressed only in terms of entities already declared in the semantic layer (`models/*.yaml`) — never a raw column, table, or arbitrary expression outside what's already declared.
- **FR-003**: System MUST execute every such query through the existing query engine/query API — no new or parallel data-access path is introduced by this feature.
- **FR-004**: System MUST show the user, alongside every natural-language answer, the underlying result (the table or number the answer was computed from) so the answer is independently verifiable.
- **FR-005**: System MUST run every conversational query under the requesting user's own authenticated session and role — a conversational query MUST NOT return or expose data, models, or capabilities that user's role could not already reach through the existing UI/API.
- **FR-006**: System MUST ask a targeted clarifying question — naming the real candidate models/dimensions/measures — when a question is ambiguous between more than one reasonable interpretation, and MUST wait for the user's answer before running a query.
- **FR-007**: System MUST decline to answer, with a specific explanation, any question that cannot be expressed using the declared dimensions/measures/models — including requests for raw-column access, cross-model joins with no declared join, or arbitrary code/SQL execution — and MUST NOT execute anything outside the semantic layer to attempt it anyway.
- **FR-008**: System MUST support short follow-up questions within one conversation that implicitly reuse the model/dimensions/measures/filters from a prior turn in that same conversation, adjusted by what the follow-up asks for.
- **FR-009**: System MUST re-validate any reused prior-turn context (model, dimensions, measures) against the current semantic layer and the user's current permissions before running a follow-up query, rather than trusting stale context.
- **FR-010**: System MUST distinguish, in its response, between "the query ran and returned no matching rows" and "this question could not be answered" — these are different outcomes and must not look the same to the user.
- **FR-011**: System MUST surface a clear, turn-scoped error to the user when the underlying query execution fails, without fabricating an answer in its place.
- **FR-012**: System's generated semantic queries MUST be logged with enough detail (question, resolved model/dimensions/measures/filters, requesting user) to audit what was actually run, consistent with the project's existing audit logging for other authenticated actions.
- **FR-013**: System MUST persist each user's conversations (questions, resolved queries, results, and answers) so they can return to their own past conversations later; a user MUST NOT be able to view another user's conversations unless that access is already granted by their role today.
- **FR-014**: System MUST let a user optionally narrow a conversation to one or more explicitly chosen semantic models before or during chatting; when models are explicitly chosen, the assistant MUST only resolve questions against that chosen scope instead of inferring across all accessible models.
- **FR-015**: System MUST document, for deployers, what data (question text, schema, and/or result values) is sent to any third-party AI service used for NL-to-query translation or answer generation.

### Key Entities

- **Conversation**: A sequence of question/answer turns between one user and the assistant. Conversations are persisted (like visuals and dashboards) so a user can return to their own chat history later; each conversation belongs to exactly one user and is subject to the same access rules as that user's other saved data.
- **Turn**: One question and its corresponding answer within a conversation — includes the user's raw question text, the resolved semantic query (model/dimensions/measures/filters/sort/limit), the query result, and the generated natural-language answer text.
- **Clarification exchange**: A turn where the assistant's "answer" is itself a question back to the user (naming candidate models/dimensions/measures), paired with the user's follow-up choice that resolves it.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user with no prior exposure to the query builder can get a correct answer to a straightforward business question (single model, unambiguous dimension/measure) in under 30 seconds from typing the question.
- **SC-002**: Every answer the assistant gives is independently verifiable — 100% of answers are accompanied by the exact underlying result data, with no answer presented without its grounding data.
- **SC-003**: The assistant never returns data a user's role could not already access through the existing query builder — verified by attempting conversational queries against every role tier and confirming no over-exposure occurs in any case.
- **SC-004**: When a question cannot be mapped to the declared semantic layer, the assistant declines rather than guesses in 100% of such cases — measured against a fixed test set of out-of-scope questions (raw-column requests, cross-model joins with no declared join, code/SQL injection attempts).
- **SC-005**: For questions with a genuinely ambiguous mapping (matching more than one model/dimension/measure), the assistant asks a clarifying question rather than silently guessing in at least 95% of cases in evaluation testing.
- **SC-006**: Users successfully complete a 2+ turn follow-up exchange (initial question plus at least one contextual follow-up) that resolves correctly without having to restate full context, in at least 90% of attempted follow-ups in evaluation testing.

## Assumptions

- The chat interface is a new surface within the existing authenticated application (alongside the query builder, dashboards, and studio/portal views) — not a standalone product or an unauthenticated public endpoint.
- "Natural-language question in, natural-language answer + grounding data out" is delivered via some form of language-model-assisted translation from question to semantic query; the specific translation approach is an implementation decision for the planning phase, not this spec.
- The natural-language translation step may send question text, semantic-model schema (dimension/measure names and descriptions, plus non-framed measures' DSL formulas — which may name a raw source column, though never raw source *data* outside of query results the user is already entitled to see), and query result values to a third-party AI service for NL-to-query translation and answer generation. What is sent, and to which provider, must be documented so a deployer can assess it against their own data-handling requirements.
- By default the assistant infers which semantic model(s) a question maps to automatically, across every model the requesting user can access, asking a clarifying question when that inference is ambiguous (per FR-006). Users must also be able to explicitly narrow a conversation to one or more chosen models up front (similar to selecting a model in the existing query builder), which removes that ambiguity for every question asked within that scope.
- This feature is scoped to answering questions (Q&A), not to creating or modifying saved visuals, dashboards, or model YAML — those remain separate, future capabilities that may later reuse this feature's question-to-semantic-query translation.
- Existing session-based authentication and the existing viewer/author/admin role checks are reused as-is; this feature does not introduce a new identity or permission system.
- The set of semantic models a conversation can draw from is exactly the set the signed-in user already has permission to query today.
