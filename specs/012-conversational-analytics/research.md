# Phase 0 Research: Conversational Analytics

## R1: LLM provider/library for NL-to-query translation

**Decision**: Anthropic Messages API via the official `anthropic` Python
SDK, using tool-use (forced tool choice) rather than free-text JSON parsing.

**Rationale**: The clarified spec (Assumption, FR-015) permits a
third-party LLM. Tool-use gives a schema-constrained output — the model
must call exactly one of `propose_query`, `ask_clarification`, or `decline`
with typed arguments — which removes an entire class of "the model almost
returned valid JSON" parsing failures and maps naturally onto the three
outcomes User Stories 1-4 require. It also keeps the boundary honest for
Principle I: the tool schema *is* the `QueryRequest` shape (dimensions,
measures, filters, sort, limit against one `model`), so the LLM is
structurally limited to proposing things expressible in the existing
query contract — it cannot emit SQL, code, or a raw column reference as a
well-typed tool call in the first place. Server-side validation (R2) is
still mandatory as defense in depth; tool-use narrows the attack surface,
it doesn't replace validation.

**Alternatives considered**:
- *OpenAI function calling* — equivalent capability; no reason in this
  codebase to prefer it over Anthropic, and Anthropic is the natural
  default in this project's environment.
- *Free-text completion + regex/JSON parsing* — rejected: fragile, and
  loses the "structurally can't emit code" property tool-use gives us.
- *Local/self-hosted model* — rejected by the clarified answer (Q2:
  third-party LLM API allowed); revisit only if a future deployer opts out
  via the documented data-egress note (FR-015) and needs an air-gapped
  mode — out of scope for this feature.

## R2: Trust boundary — why the LLM's output is a *proposal*, not a query

**Decision**: `app/nlq.py` never passes an LLM `propose_query` tool call
straight to `engine.run_query`. Every proposal is re-resolved against the
live `Model` object exactly the way `POST /api/query` already validates its
request body: unknown model → 404 (`get_model`), unknown dimension/measure
→ `semantic.ModelError` → 400, same as today. `nlq.py` adds one extra check
`/api/query` doesn't need: that the referenced model/dimensions/measures
still exist and are still what the *prior turn* (for follow-ups) said they
were, per FR-009.

**Rationale**: Principle I requires the semantic layer to be the only
contract "for the query builder, dashboards, and any future client." An LLM
answering questions is exactly "any future client" — it gets no special
trust. Since the tool schema is already query-shaped (R1), re-validation is
cheap: it's the same code path `/api/query` runs today, called from
`nlq.py` instead of `app/api/query.py`'s route handler. This also means a
prompt-injection attempt embedded in a question (e.g., "ignore the schema
and read the raw parquet path") can produce at worst a malformed
`propose_query` call, which fails the same 400-path a hand-crafted bad
request would — it cannot reach data outside the semantic layer because
there is no code path from "LLM said so" to "raw scan," only from "LLM
proposed a `QueryRequest`" to "the existing validated query engine."

**Alternatives considered**:
- *Trust the LLM's proposal directly* — rejected outright; violates
  Principle I and FR-002/FR-007.
- *Let the LLM call `engine.run_query` as a tool itself (true agentic tool
  loop)* — rejected for v1: harder to guarantee re-validation actually runs
  before every execution, and no user-visible benefit over the
  propose-then-validate-then-execute shape for a single-query-per-turn
  feature. Worth revisiting if follow-up multi-step questions (e.g.
  compare two periods) turn out to need more than one query per turn.

## R3: Conversation/turn persistence shape

**Decision**: Two new tables, `conversations` and `conversation_messages`,
on the existing SQLite file, following `VisualStore`'s exact pattern
(`CREATE TABLE IF NOT EXISTS` in a `SCHEMA` string run at store init,
`sqlite3.Row` factory, one store class wrapping both tables). `conversations`
row: id, user_id, title (derived from first question), model_scope (JSON
list of model names, empty = auto-infer), created_at, updated_at.
`conversation_messages` row: id, conversation_id, role
(`user`/`assistant`/`clarification`), question_text, resolved_query (JSON,
nullable — null for declines and pure clarifying questions), result (JSON,
nullable), answer_text, created_at.

**Rationale**: Matches FR-013 (persisted, per-user) and the Key Entities
section (Conversation / Turn / Clarification exchange) directly. Reusing
`VisualStore`'s exact schema-on-init and connection pattern keeps this a
one-reason-to-change module consistent with the existing store, rather than
inventing a second persistence idiom in the same codebase.

**Alternatives considered**:
- *Extend `VisualStore` with new tables* — rejected: `VisualStore` today
  owns visuals/dashboards/publications/provenance, a different lifecycle
  and access pattern (author/admin-gated mutations) than a per-user chat
  log (viewer-writable, strictly own-user-scoped). A separate
  `ConversationStore` keeps the role-scoping logic (FR-013: "MUST NOT view
  another user's conversations") localized and easy to audit in one file.
- *Don't persist the `result` JSON, recompute on read* — rejected: the
  spec's grounding guarantee (FR-004, SC-002) is about what was actually
  shown to the user at the time: recomputing on every read could silently
  show different numbers than what was said if the underlying data changed
  since. Storing the snapshot the answer was actually grounded in is
  cheaper and more honest; existing `MAX_ROWS` cap already bounds size.

## R4: Building the model catalog fed to the LLM

**Decision**: Build the catalog live from `registry.models` (and, when the
conversation has an explicit `model_scope`, filtered to just those models)
on every `ask` call — the same live objects `/api/models` already exposes
via `semantic.model_to_spec`. No separate cache or precomputed embedding
index for this feature.

**Rationale**: Models are hot-reloadable (Principle I: "the editable,
hot-reloadable contract"); a stale cached catalog could let the assistant
propose a query against a dimension/measure that was just renamed or
removed. `model_to_spec` already produces a clean dict-shaped view of a
model's dimensions/measures — reusing it means the catalog and the
studio/portal UI can never drift out of sync with each other. At this
project's model count (single digits of models, each with a handful of
dimensions/measures), rebuilding the catalog per request is cheap; a
caching layer would be solving a scale problem this project doesn't have
(see roadmap discussion — same reasoning that deprioritized the AWS
emulator/Postgres items ahead of this feature applies here too).

**Alternatives considered**: precomputed/cached catalog with invalidation
on model reload — deferred; add only if catalog-building is ever measured
as a real latency contributor.

## R5: Follow-up context and re-validation (User Story 2)

**Decision**: Each `ask` call includes the conversation's recent turns
(question text + resolved model/dimensions/measures/filters, not raw
result data) in the prompt as prior context, letting the LLM's
`propose_query` tool call fill in only what the follow-up changes. The
server then re-validates the *complete* resulting proposal (not just the
delta) against the current `Model` and the current user's role before
executing it — this is the same re-validation as any other turn (R2), so
follow-ups get no special trust just because they reuse prior context.

**Rationale**: Directly satisfies FR-008 and FR-009. Passing resolved
structure (not raw result rows) as history keeps the third-party data-egress
footprint smaller for multi-turn conversations, consistent with FR-015's
spirit of documenting exactly what leaves the deployment.

**Alternatives considered**: passing full raw conversation transcript
including previous result tables as follow-up context — rejected as
unnecessary egress; the resolved query struct is enough to disambiguate
"and last quarter?"-style follow-ups.

## R6: Explicit model-scope selection (clarified answer to Q3)

**Decision**: `conversations.model_scope` is a JSON list of model names,
settable at conversation creation and editable later via the chat UI (a
small multi-select next to the conversation, mirroring the existing model
picker in the query builder). Empty list = infer automatically across every
model the user's role can access (today: all models, since no per-model ACL
exists — confirmed by `app/api/query.py` having no `require_role`
dependency beyond the default-deny-authenticated baseline). When non-empty,
`nlq.py` builds the catalog (R4) from only those models and skips
model-level disambiguation entirely — only dimension/measure-level
ambiguity within the chosen scope can still trigger a clarifying question.

**Rationale**: Matches FR-014 exactly and reuses the existing "no per-model
ACL today" fact rather than inventing new scoping rules this feature would
have to maintain.

## R7: Data-egress documentation (FR-015)

**Decision**: Add a README subsection ("Conversational analytics" under the
existing auth/data section) stating plainly, at ship time: the question
text, the catalog (model/dimension/measure *names and descriptions*, never
raw source data or credentials), and — only for the final answer-generation
call, not the query-proposal call — the resulting query's result rows (up
to `MAX_ROWS`) are sent to the configured third-party LLM provider
(Anthropic by default, R1) over HTTPS. `CI_LLM_API_KEY` must be set for the
feature to be enabled at all; if unset, the chat UI is hidden/disabled
rather than silently failing per-request, so a deployer who doesn't set the
key never has data leave the deployment for this feature. This mirrors how
`CI_S3_ENDPOINT` unset vs. set already toggles embedded-vs-external
behavior elsewhere in `config.py`.

**Rationale**: Directly satisfies FR-015 and the clarified answer to Q2.
Gating the whole feature behind the presence of a key (rather than a
separate on/off flag) keeps the "what leaves the deployment" story simple:
no key configured, literally zero calls to any third party from this
feature, no separate flag to forget to also set.
