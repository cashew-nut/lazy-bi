# Conversational Analytics

**Source:** `app/llm.py` (623 lines) · `app/llmclient.py` (731 lines) ·
`app/nlq.py` (675 lines) · `app/composer.py` (563 lines) ·
`app/measurewriter.py` (1,096 lines) · `app/memorystore.py` (191 lines) ·
`app/conversationstore.py` (214 lines)

Every LLM-backed surface in this codebase — Chat, the modelling panel's
inline chat, the Composer, the measure writer, MCP's `ask_question` skill,
and (a related but separate seam) the [sandbox coding agent](sandbox.md) —
follows one rule without exception: **the model's output is a proposal,
never a decision.** A typed, *unvalidated* result comes back from the LLM
call; a plain-Python step re-checks it against live server state before
anything executes, saves, or renders. This page covers the seam that
enforces that rule for query answering (`nlq.py`/`llm.py`), for page
composition (`composer.py`) and for measure authoring
(`measurewriter.py`), plus the provider-neutral client all three are built
on (`llmclient.py`).

**Off entirely unless configured** — every route this page describes 503s
unless `CI_LLM_API_KEY` is set (or `CI_LLM_PROVIDER=bedrock`, which
authenticates by IAM role instead of a key). There's no separate feature
flag to forget: the key's presence *is* the flag.

## The provider-neutral client (`app/llmclient.py`)

The one module that knows which **wire format** a configured endpoint
speaks. Every seam above it (`llm.py`, `sandbox_agent.py`, `composer.py`)
builds the same request shape and consumes the same event stream — adding
a provider never touches a prompt, a tool schema, or a validation path.

```python
@dataclass(frozen=True)
class ChatRequest:
    model: str
    max_tokens: int
    system: str
    tools: list[dict]           # Anthropic-shaped {name, description, input_schema}
    prompt: str
    force_tool: str | None = None   # None = "call *some* tool"
    cache_system: bool = False       # explicit cache breakpoint on the Anthropic wire
    thinking: bool = False

class LLMClient(Protocol):
    def call(self, req: ChatRequest) -> ToolCall: ...
    def stream(self, req: ChatRequest) -> Iterator[ClientEvent]: ...
```

Every caller asks for exactly one thing — *call one of these tools,
with these arguments* — with **no multi-turn tool loop and no assistant
message history**. That narrow contract is what makes "point it at a URL
and a key" actually work: there's nothing provider-specific left to port
beyond auth and request shape.

**Two wire formats cover everything**: `AnthropicClient` (Anthropic's
Messages API, forced tool use, and `AnthropicBedrock` for native AWS
SigV4 auth) and `OpenAIClient` (chat-completions — deliberately not the
newer Responses API, since chat-completions is what every third-party
gateway actually implements). `resolve_provider(base_url, override,
api_version)` picks between them by URL, since a URL is the one thing a
deployer reliably has:

| Host pattern | Provider |
|---|---|
| unset | `anthropic` (the historical default) |
| `*.anthropic.com` | `anthropic` |
| `CI_LLM_API_VERSION` set | `azure` (an api-version is an Azure-only concept — the one signal that survives a gateway hiding the real hostname) |
| `*.azure.com` / `*.azure-api.net` | `azure` |
| `bedrock-runtime.*` | `bedrock` (native SigV4), or `openai` if the path is Bedrock's own `/openai/…` surface |
| anything else | `openai` (by far the most common third-party gateway dialect: vLLM, Ollama, LiteLLM, OpenRouter, Together, Groq, …) |

`CI_LLM_PROVIDER` overrides detection outright for the one case a hostname
truly can't express: an Anthropic-format gateway on a neutral host.

**Streaming partial tool arguments** works differently per wire — the
Anthropic SDK hands over an already-parsed partial dict per event; the
OpenAI wire streams raw JSON *string* fragments, which `parse_partial_json`/
`_repair_json` reassemble into the same growing dict shape (closing an
incomplete JSON document at the last cleanly-closable point, keeping a
partially-written *string value* — the interesting case for a half-written
`html` or `source` field — but discarding a half-written key or number,
which carry nothing worth showing). This is what makes the Composer's page
appear as it's written and the sandbox agent's code fill in live on either
provider, not just Anthropic's.

**Two OpenAI-wire quirks absorbed here, invisibly to every caller**:
reasoning models rename `max_tokens` → `max_completion_tokens`, guessed
from the model id (`_REASONING_MODEL_RE`) and self-corrected on the
provider's first 400 (`_swap_token_param`); and `max_completion_tokens` is
one pooled budget for *reasoning* tokens and the *answer*, so
`CI_LLM_REASONING_TOKENS` (default 8192) is added on top there — without
that headroom, a reasoning model can spend its whole allowance thinking and
never reach the tool call, which `_no_tool_call()` reports as exactly that
diagnosis rather than a bare "model did not call any tool."

**`key_fingerprint(api_key)`** (`len=32 sha256=dfd5e83e`-shaped) exists for
the one failure a 401 can't describe on its own: a key that's correct at
its source and damaged in transit (shell `$` expansion, wrapping quotes
kept as part of the value, a stray line break). Logged on every auth
failure and printed at startup (`app/main.py`'s `_llm_banner`), so
comparing it against the key at its source turns "but that key works
elsewhere" into a one-line check instead of a guessing game.

## Chat: translate → re-validate → execute

### The translator (`app/llm.py`)

`LLMTranslator` forces the model to call exactly one of four tools, so the
result is always a typed decision:

| Tool | Meaning |
|---|---|
| `propose_query` | Answer with a semantic query against exactly one declared model. |
| `ask_clarification` | The question is genuinely ambiguous between real candidates — name them, never invent one. |
| `show_last_query` | The user wants to *see* the query definition behind a prior answer, not new data. |
| `decline` | Can't be answered from the declared catalog (needs a raw column, code, an undeclared join, or isn't a business question). |

Every tool schema is **built from the engine's own vocabulary**, not
hand-copied: `filters[].op`'s enum is `sorted(engine.FILTER_OPS)`,
`grain`'s enum is `TIME_GRAINS`, and the relative-date grammar in the
prompt is `engine.RELATIVE_DATE_SYNTAX` verbatim — so what the model is
told it may write can never drift from what `nlq.resolve()` actually
accepts. `propose_query` additionally lets the model define an **ad-hoc
inline measure** (`inline_measures`) for a calculation nothing declared —
a running total, a period-over-period change — as a SQL window expression
over `w` reading one of the model's own already-declared measures, using
the identical grammar model authors use (see
[Query Engine → The SQL grammar](query-engine.md#the-sql-grammar)).

Every tool also optionally carries `memories` — see
[Self-learning model memories](#self-learning-model-memories-appmemorystorepy)
below.

`ModelCatalogEntry` is what the LLM actually sees per model: name, label,
description, and per dimension/measure its declared `synonyms` (merged with
any chat-*learned* ones) plus, for a categorical dimension, up to
`SAMPLE_VALUES_LIMIT` (200) real stored values, and for a non-`from:`
measure its literal SQL formula. `PriorTurn` carries a conversation's
recent resolved structure (never raw result rows) forward as follow-up
context, so "break this down by quarter" can be answered as a
`propose_query` built on the previous turn rather than starting over.

### Re-validation (`app/nlq.py`)

```python
def resolve(question, catalog, prior_context, models, translator, scope=None) -> Decision:
    raw = translator.translate(question, catalog, prior_context)   # RawToolCall — unvalidated
    return _dispatch(raw, catalog, prior_context, models, scope or [])
```

`_dispatch()` routes to one of four validators, each checked against the
**live** `models` dict (never the catalog snapshot the LLM saw, which can
be stale by the time the response arrives):

- **`_validate_propose_query`** — the named model must exist and be in
  scope; every dimension/measure/filter field must resolve on the live
  model (`model.dimension()`/`model.measure()`, which raise `ModelError`
  on an unknown name); every filter `op` must be one of `engine.FILTER_OPS`;
  a time filter's value must satisfy `engine.date_value_error()`. Any
  failure downgrades to a `Decline` naming the reason — **never an
  exception that reaches an executed query.** Inline measures get their
  own check (`_validate_inline_measures`): must be a window expression
  (`sqlgrammar.is_window_expr`) over an already-declared measure, never a
  raw column or another inline measure.
- **`_validate_ask_clarification`** — every named candidate must be a real
  model/dimension/measure name from the catalog; an empty result after
  filtering is itself a `Decline`.
- **`_validate_show_last_query`** — the referenced prior turn was already
  validated when it was first proposed, so this only checks that one
  exists to show.
- **Casing safety net** (`_correct_categorical_filter_casing`) — an
  `eq`/`ne`/`in`/`not_in` filter value is corrected against the model's own
  real sample values case-insensitively before the query runs, in case the
  model's own wording-to-value conversion (steered by the system prompt)
  falls short.

A `Decision` is `ProposeQuery | AskClarification | Decline | ShowQuery` —
`handle_decision()` (shared by the HTTP route and the MCP skill — see
[Agents & MCP](agents-and-mcp.md)) persists the assistant's turn, executes a
`ProposeQuery` through `engine.run_query()` (the **identical** code path
`POST /api/query` uses), and writes one audit-log entry naming the outcome.
`summarize()` produces the answer's natural-language grounding text via a
**template**, not a second model call — cheaper, faster, and trivially
guaranteed to only ever describe what the result actually contains.

### Streaming

`resolve_streaming()` mirrors `resolve()` but yields every `StreamEvent`
from `translator.translate_streaming()` as it happens (thinking deltas, the
proposed tool name, its args taking shape) for live display, then yields
exactly one `Decision` at the end — produced by the **same** `_dispatch()`
`resolve()` uses. Streaming is purely a UI convenience; it can never let a
caller trust anything the non-streaming path wouldn't.

### Self-learning model memories (`app/memorystore.py`)

Any tool call may also carry `memories`: durable, **model**-scoped facts
learned from that one exchange — never facts about the person asking. Two
kinds, a deliberately closed vocabulary (`MEMORY_KINDS = ("synonym",
"note")`):

- **`synonym`** — the question used a business term for a declared
  dimension/measure the YAML doesn't list (`subject` = the declared name,
  `content` = the new term).
- **`note`** — a short, user-independent fact about the model's vocabulary
  or data.

`validate_memory(model, kind, subject, content)` is the **one shared
rulebook** — used both to silently drop a bad LLM-proposed memory
(`nlq._validate_memories`, capped at `MAX_MEMORIES_PER_TURN = 3`) and to
400 a bad *admin* submission via `app/api/memories.py`. A synonym's subject
must resolve to a real declared dimension/measure, and mustn't already be a
declared name or synonym of it; content/subject length is capped
(`MAX_CONTENT_LEN=300`, `MAX_SUBJECT_LEN=100`); a model is capped at
`MAX_PER_MODEL = 200` stored memories, past which `MemoryStore.add()` is a
silent no-op — re-learning an already-known fact costs nothing and reports
nothing.

`MemoryStore` has **no per-user retrieval axis at all** — `created_by`
exists purely as audit attribution, never as a read filter — which is what
structurally prevents anything user-specific from ever being "remembered"
through this mechanism, regardless of what a prompt might otherwise coax a
model into proposing. `all_by_model()` is what feeds every future
conversation's catalog: a synonym merges into the matching dimension/
measure's `synonyms` list; a note becomes a `learned fact:` line — both
indistinguishable, by the time the LLM sees them, from ones declared
directly in the YAML.

### Conversations (`app/conversationstore.py`)

`ConversationStore` persists `conversations` (owner, title, `model_scope`,
per-conversation `llm_model`/`thinking` overrides — both `NULL` until
explicitly set, so an untouched conversation keeps following the server
default as it changes rather than freezing it at creation time) and
`conversation_messages` (role, question text, resolved query, result,
outcome, answer text). Every read/write is **strictly owner-scoped** —
`get(conversation_id, user_id)` returns `None` both for a missing
conversation and for one owned by someone else, so existence is never
leaked across accounts.

## The Composer (`app/composer.py`)

Same architecture as `llm.py`, applied to whole-page composition instead of
query translation: one forced tool call (`compose_page`, returning
`{name, html, summary}`), and everything the model returns is
**unvalidated** until `sanitize_notebook_html()` re-checks it.

**The notebook HTML vocabulary is a closed allowlist**
(`ALLOWED_TAGS`/`_TAG_ATTRS`/`_GLOBAL_ATTRS`) — no `<script>`, `<style>`,
`<img>`, `<a>`, inline `style=`, `id=`, or event-handler attributes
anywhere; a page styles itself purely through the app's own `nb-*` class
vocabulary (tabs, collapsibles, split rows, explainer callouts — see
[Frontend → Notebooks](frontend.md#notebooks)). `_Sanitizer` (an
`HTMLParser` subclass) rebuilds the HTML keeping only allowed tags/
attributes: a disallowed *container* (`<script>`, `<iframe>`, …) is dropped
**with its entire subtree** so nothing it wrapped can leak through as text;
other disallowed tags are merely unwrapped, letting harmless stray markup
degrade gracefully instead of failing the whole page.

**Grounding, not just structure, is enforced**: every `data-visual-id`/
`data-dashboard-id` the page embeds is collected during the sanitize pass
and checked against the *live* registry — a proposal referencing an id
that doesn't exist raises `HtmlValidationError` outright rather than
saving a page with a dead embed. `_check_tabs_structure()` similarly
requires every `nb-tabs` group's button/panel `data-tab` names to match
exactly.

**The prompt forbids invented numbers**: the live charts carry the actual
figures; the narrative only frames them — a page must never state a
specific number, percentage, or trend direction unless the user's own
narrative supplied it, precisely because the reader sees *live* data that
may have moved since the page was written.

**Templates** (`TEMPLATES`) are structural *hints*, not literal markup —
`freeform`, `executive` (headline stats up top, detail behind
collapsibles), `tabbed` (parallel threads, one `nb-tabs` group), `longform`
(continuous prose, evidence introduced inline), `brief` (one stat, one
chart, tight bullets). `build_catalog(store)` is what the model may embed —
every saved visual/dashboard's id, name, chart type, and declared query
fields, rebuilt live per request.

**Saving is deliberately not this module's job.** The client persists an
accepted draft through the ordinary author-gated notebooks CRUD (see
[API Layer](api-layer.md)), so there's exactly one write path for notebook
HTML, and it's the one `sanitize_notebook_html`'s contract documents.

## The measure writer (`app/measurewriter.py`)

The third seam, and the one with an **oracle**: a measure either compiles and
runs against the real data or it doesn't. So this one doesn't stop at
re-validating a proposal — it *executes* it, and when that fails it hands the
error back to the model and asks again.

```
build_context() -> write_measure | decline -> check() -> dry run -> Verdict
                        ^                                              |
                        |___________ Attempt(measure, error) __________|   (MAX_ATTEMPTS = 3)
```

**One forced tool call**, like the other two: `write_measure`
(`{name, label, format, description, expr, from, emits, synonyms, rationale}`)
or `decline` when the declared columns can't express the ask. `RawMeasure` is
unvalidated; only a proposal that survives the loop below is ever returned.

**`check()` re-applies the save paths' own rules** — snake_case name, unique
against every declared dimension/measure, a known `format`, `emits` needing a
`from`, and the parameter rules (`param()` is refused outright in the model
scope, exactly as `app/api/models.py` refuses to save one; in the visual scope
it must be declared, and int-typed in `LAG`'s offset position, as
`app/api/visuals.py` requires). Then the expression itself goes through the
identical `sqlgrammar.compile_expression` / `semantic.validate_from_block`
calls a hand-typed measure does — including the stricter of the two schemas
(the fact scan's source columns, not the query's dimension names), so a
measure that verifies here is one the author can also **save**.

**The dry run is what makes it trustworthy.** `verify()` runs the proposal as
an *inline measure* through `engine.run_query()` — the same code path
`POST /api/query` uses — and that catches everything static checking
structurally can't: a column the block selects that isn't in the source, a
type error inside an aggregate, an `emits` the block never outputs, and above
all a `from:` block that drops the query's dimensions. That last one is the
most common way a complex measure breaks and it is invisible until it runs,
so `dry_run_query()` deliberately shapes a query that would expose it: a
window measure gets a time dimension if the query has none, an emitted
dimension is grouped on, and a `from:` measure is always grouped by at least
one real dimension (with no grouping at all, `{dims}` collapses to the
engine's constant column and a block that forgot to carry the dimensions
through would pass). In the visual scope the shape starts from the query the
author is actually looking at, so a pass means "this works where you're
putting it".

An **unreachable source is not a rejection**: the measure compiled, so it
comes back with `ran=False` and a note saying it couldn't be executed — a
broken bucket must never read like a bad measure.

**The repair loop** (`run_streaming`) is the payoff: a rejected proposal
becomes an `Attempt(measure, error)` in the next prompt, under an explicit
instruction to fix the cause rather than resend or retreat to something
trivially different. Every attempt and its error travels back to the UI, so a
measure that took two tries says so instead of looking like magic. Three
attempts is the ceiling — past that the cause is usually data that isn't
there, and the author is better served by the error and the last attempt than
by more waiting.

**Context is built live per request** (`build_context()`), never taken from
the client — the caller names a model (or the modelling form's unsaved draft
`spec`), a fact table and a scope, and the server introspects: real columns
with dtypes (`engine.scan_schema`), every declared dimension with real sample
values for the categorical ones (bounded by `SAMPLE_VALUES_LIMIT`/
`SAMPLE_DIMENSIONS_LIMIT`, since each costs a `SELECT DISTINCT`), every
existing measure **with its formula** (the house style to match, and the
sibling names a window expression may read), and in the visual scope the
query and parameters the measure is being written into. The system prompt
teaches the three shapes structurally — plain, window (`OVER w`), and complex
(`from:`/`emits`, with the patterns that need one: an aggregate of an
aggregate, de-duplicating to a coarser entity, first/last event per entity,
semi-additive balances) — and its function list is read from
`sqlgrammar.aggregate_functions() & allowed_functions()`, so it can't
advertise something the validator would refuse.

**Saving is deliberately not this module's job**, same as the Composer: a
verified draft lands in the measure lab or the modelling form, and the author
saves it through the ordinary author-gated measure endpoints. One write path
for a measure, and it's the one a typed measure uses.

## Any provider: a URL and a key

Nothing above `llmclient.py` is Anthropic-specific. Point the deployment
anywhere by setting a base URL and a key:

| Variable | Meaning |
|---|---|
| `CI_LLM_BASE_URL` | The endpoint. Unset = Anthropic's own API. |
| `CI_LLM_API_KEY` | The key, and the on/off switch for every LLM feature. |
| `CI_LLM_MODEL` | A model id that endpoint actually serves (Azure: the deployment name). |
| `CI_LLM_PROVIDER` | `auto` (default) / `anthropic` / `openai` / `azure` / `bedrock`. |
| `CI_LLM_MODEL_CHOICES` | Comma-separated ids the Chat model picker offers. |
| `CI_LLM_THINKING_MODELS` | Which of those ids can be asked for extended thinking — `none` disables the toggle entirely. |
| `CI_LLM_API_VERSION` | Azure's dated deployment surface — setting it also *implies* `azure`, on any host. |
| `CI_LLM_AWS_REGION` | Region for native Bedrock (defaults to `AWS_REGION`). |

`GET /api/health` reports `llm_provider` (the resolved wire format) —
checking that first is the fastest way to diagnose a misconfigured
endpoint without reading server logs.

## What leaves the deployment

Only ever when `CI_LLM_API_KEY` is configured — no separate feature flag to
forget. Every chat question sends the question text and the model catalog
(names, descriptions, declared synonyms, a non-`from:` measure's literal
SQL formula — which can name a raw source column that's otherwise never
sent, since dimensions/filters/sort only ever use declared names) plus up
to 200 real distinct values of any categorical dimension, over HTTPS, to
the configured endpoint. Once a proposal is validated and run, the *result
rows* (capped at `MAX_ROWS`, the same cap the query builder uses) are also
sent, so the assistant can write the natural-language answer. The Composer
sends the instruction/narrative/history, the visual/dashboard catalog, and
the current draft HTML — never result rows. The sandbox coding agent sends
notebook *code*, output schemas, and bucket paths — never result rows
either; see [Sandbox](sandbox.md).

## API surface

See [API Layer](api-layer.md#chat) and
[API Layer](api-layer.md#composer) for the full route tables.
