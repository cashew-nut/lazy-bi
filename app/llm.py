"""The one seam that turns a question into a typed decision (specs/012-
conversational-analytics/). Everything above this module (app/nlq.py) only
ever sees the typed `RawToolCall` result — never raw model output — and treats
it as *unvalidated*: nlq.resolve() re-checks it against the live semantic
model before it can become an executable query (research.md R2).

Swappable by design at two levels: tests use a FakeTranslator implementing the
same Translator protocol, so the translator contract is exercised with zero
network calls (plan.md's Testing section) — and the *provider* underneath is
swappable too, since everything here is expressed as a provider-neutral
llmclient.ChatRequest. The prompt and tool schemas below are the whole of this
module's contribution; which API they're sent to is app/llmclient.py's
business (Anthropic, OpenAI, Azure, Bedrock, or any compatible gateway).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Literal, Protocol

from . import config, engine, llmclient
from .semantic import TIME_GRAINS

ToolKind = Literal["propose_query", "ask_clarification", "decline", "show_last_query"]

# Reused (not copied) from the engine/semantic modules that actually enforce
# these, so the tool schema/prompt can never drift from what a proposal is
# re-validated against (nlq._validate_propose_query) and executed against
# (engine.run_query) — see the bug this fixes: filters[].op previously had
# no declared vocabulary at all (the model guessed '=' instead of 'eq'), and
# grain's only guidance was a wrong example ("1qtr" isn't a real grain).
_FILTER_OPS = sorted(engine.FILTER_OPS)
_GRAINS = list(TIME_GRAINS)
_RELATIVE_DATE_KEYWORDS = list(engine.RELATIVE_DATE_KEYWORDS)


@dataclass(frozen=True)
class ModelCatalogEntry:
    """One model's queryable shape, as shown to the LLM — only what semantic.
    model_to_spec already exposes to the existing, authenticated /api/models
    endpoint (research.md R4). Every dimension/measure also carries any
    declared `synonyms` (alternate business vocabulary, e.g. 'sales' for a
    measure named 'revenue') so a question's own wording can be matched even
    when it doesn't echo the declared name/label/description. Non-framed
    measures also carry their DSL `expr` (nlq._measure_catalog_entry) so the
    LLM can read a measure's actual formula instead of guessing from its
    name/description alone — a name isn't always enough to tell e.g. an
    unweighted average from a weighted one. A formula may reference raw
    source columns that never appear anywhere else in this catalog
    (dimensions/filters/sort only ever use declared names); that's a
    deliberate, documented data-egress addition (README's "Conversational
    analytics" section, FR-015), not a new *query* capability — a raw column
    named in a formula still can't be used anywhere in a proposal
    (app/nlq.py's re-validation rejects it)."""
    name: str
    label: str
    description: str
    dimensions: list[dict] = field(default_factory=list)  # [{name, label, type, description, synonyms}]
    measures: list[dict] = field(default_factory=list)     # [{name, label, description, synonyms, expr?}]
    # chat-learned free-text facts about this model (memorystore kind:"note"),
    # shown as "learned fact" lines in the prompt catalog. Learned *synonyms*
    # don't appear here — nlq.build_catalog merges them straight into the
    # dimension/measure `synonyms` lists above, indistinguishable from
    # yaml-declared ones by the time the LLM sees them.
    learned_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PriorTurn:
    """A prior turn's resolved structure, offered as follow-up context
    (research.md R5) — never raw result rows."""
    question_text: str
    model: str | None
    dimensions: list
    measures: list[str]
    filters: list[dict]
    sort: dict | None = None
    limit: int | None = None
    inline_measures: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class RawToolCall:
    """The LLM's unvalidated tool call. `kind` says which of the four tools
    it invoked; `args` is that tool's raw input dict."""
    kind: ToolKind
    args: dict


@dataclass(frozen=True)
class StreamEvent:
    """One incremental update from Translator.translate_streaming(), for a
    caller that wants to show live progress before the final decision is
    ready. Every kind but "done" is display-only — nlq.resolve_streaming
    still re-validates only the final RawToolCall (`final`), identical to
    the non-streaming path, so streaming can never change what's trusted."""
    kind: Literal["thinking", "tool_name", "tool_input", "done"]
    text: str = ""                      # kind="thinking": the thinking delta
    tool_name: str | None = None        # kind="tool_name": which of the four tools was called
    tool_input: dict | None = None      # kind="tool_input": accumulated partial args so far
    final: RawToolCall | None = None    # kind="done": what translate() would have returned outright


class TranslatorError(Exception):
    """The LLM call itself failed (network/timeout/API error) — distinct
    from the model producing a bad *proposal*, which is nlq.resolve()'s
    concern, not this module's."""


class Translator(Protocol):
    def translate(
        self,
        question: str,
        catalog: list[ModelCatalogEntry],
        prior_context: list[PriorTurn],
    ) -> RawToolCall: ...

    def translate_streaming(
        self,
        question: str,
        catalog: list[ModelCatalogEntry],
        prior_context: list[PriorTurn],
    ) -> Iterator[StreamEvent]: ...


_TOOLS = [
    {
        "name": "propose_query",
        "eager_input_streaming": True,
        "description": (
            "Answer the question with a semantic query against exactly one "
            "declared model. Every dimension/measure named MUST be one of "
            "the catalog's declared names for that model — never a raw "
            "column, invented field, or field from a different model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "dimensions": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "grain": {
                                        "type": "string",
                                        "enum": _GRAINS,
                                        "description": "only for time-typed dimensions.",
                                    },
                                },
                                "required": ["name"],
                            },
                        ]
                    },
                },
                "measures": {"type": "array", "items": {"type": "string"}},
                "inline_measures": {
                    "type": "array",
                    "description": (
                        "Ad-hoc measures computed only for this query, for a calculation "
                        "the catalog has no declared measure for (a running total, a "
                        "period-over-period change/growth, etc.). Each must be a window "
                        "expression — running_total(measure) or lag(measure[, periods]) — "
                        "over one of the catalog's own declared measure names, never a raw "
                        "column. Include the chosen name(s) in `measures` above to have "
                        "them appear in the result."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "a new name, not already a declared measure/dimension."},
                            "expr": {
                                "type": "string",
                                "description": (
                                    "e.g. running_total(revenue), lag(revenue), lag(revenue, 4), "
                                    "or (revenue - lag(revenue)) / lag(revenue) for a % change. "
                                    "Bare names must be declared measures of this query's model."
                                ),
                            },
                            "label": {"type": "string"},
                            "format": {"type": "string", "enum": ["number", "currency", "percent"]},
                        },
                        "required": ["name", "expr"],
                    },
                },
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "op": {
                                "type": "string",
                                "enum": _FILTER_OPS,
                                "description": (
                                    "eq/ne/gt/gte/lt/lte/contains compare against `value`; "
                                    "in/not_in compare against `values` (a list). contains is "
                                    "a case-insensitive substring match. Never a symbol like "
                                    "'=' or '>', and never a SQL keyword like 'LIKE'."
                                ),
                            },
                            "value": {
                                "description": (
                                    "for eq/ne/gt/gte/lt/lte/contains. A date/time field also "
                                    f"accepts a relative keyword ({', '.join(_RELATIVE_DATE_KEYWORDS)}) "
                                    "or an offset like 'today-90d' / 'today+2mo', besides an ISO date."
                                ),
                            },
                            "values": {"type": "array", "description": "for in/not_in only."},
                        },
                        "required": ["field", "op"],
                    },
                },
                "sort": {
                    "type": ["object", "null"],
                    "properties": {
                        "by": {"type": "string", "description": "one of this query's own dimension or measure names."},
                        "desc": {"type": "boolean", "description": "defaults to true (descending) when omitted."},
                    },
                },
                "limit": {"type": ["integer", "null"]},
            },
            "required": ["model", "dimensions", "measures"],
        },
    },
    {
        "name": "ask_clarification",
        "eager_input_streaming": True,
        "description": (
            "The question is ambiguous between more than one real model, "
            "dimension, or measure. Ask the user which they meant, naming "
            "the actual candidate names from the catalog — never invent one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_text": {"type": "string"},
                "candidates": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question_text", "candidates"],
        },
    },
    {
        "name": "show_last_query",
        "eager_input_streaming": True,
        "description": (
            "The user wants to SEE the query definition "
            "(model/dimensions/measures/filters) behind a previous answer in "
            "this conversation, as text — they are not asking for any data "
            "they don't already have. Use this only for things like 'show me "
            "the query', 'what did you just run', or 'return the query you "
            "used'. Do NOT use this for a follow-up that wants the previous "
            "answer changed, extended, or recomputed — 'break this down by "
            "quarter', 'now just the top 5', 'and last year?', 'same but "
            "monthly' all ask for new numbers, so they are propose_query "
            "calls built on the prior turn, not this tool. Referring to a "
            "previous answer is not the same as asking to see its query. "
            "Takes no arguments."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "decline",
        "eager_input_streaming": True,
        "description": (
            "The question cannot be answered from the declared catalog "
            "(needs a raw column, an undeclared cross-model join, "
            "arbitrary code/SQL, or is not a business question at all). "
            "Explain briefly and plainly why."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason_text": {"type": "string"}},
            "required": ["reason_text"],
        },
    },
]

# Optional on every one of the four tools (learning can accompany any
# decision — an answer, a clarification, even a decline): durable facts
# about a *model* worth remembering for future conversations, by any user.
# Anything the LLM proposes here is re-validated (nlq._validate_memories)
# before it can reach the store, exactly like a proposed query.
_MEMORIES_PROPERTY = {
    "type": "array",
    "maxItems": 3,
    "description": (
        "Optional: durable facts learned from THIS exchange about a catalog "
        "model itself, worth remembering for every future user of that model. "
        "kind 'synonym': the question used a business term for a declared "
        "dimension/measure that its catalog entry doesn't list — subject is "
        "the declared name, content is the new term. kind 'note': a short, "
        "user-independent fact about the model's vocabulary or data (e.g. "
        "'category values are title-cased English names'). NEVER "
        "record anything about the current user: no preferences, no names, "
        "no habits, no favorite charts or formats, nothing session-specific "
        "— only facts about the model that hold for everyone."
    ),
    "items": {
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "the catalog model this fact belongs to."},
            "kind": {"type": "string", "enum": ["synonym", "note"]},
            "subject": {
                "type": "string",
                "description": "synonym only: the declared dimension/measure name the term maps to.",
            },
            "content": {
                "type": "string",
                "description": "the synonym term itself, or the note text (short, one fact).",
            },
        },
        "required": ["model", "kind", "content"],
    },
}

for _tool in _TOOLS:
    _tool["input_schema"]["properties"]["memories"] = _MEMORIES_PROPERTY


def _tools_for_catalog(catalog: list[ModelCatalogEntry]) -> list[dict]:
    """_TOOLS, with propose_query's `model` constrained to this request's
    actual catalog — the same defense-in-depth reasoning as the filters[].op
    and dimensions[].grain enums above (both sourced from the engine/semantic
    modules rather than hand-copied): `model` previously had no declared
    vocabulary at all, so the LLM could omit it (or invent one) with nothing
    in the schema to ground it — most visible with a single-model scope,
    where there's no real ambiguity to resolve. That surfaced as nlq.py's
    _validate_propose_query declining with the confusing "'None' is not a
    model this conversation can query." An empty catalog (nothing this
    conversation can query at all) leaves `model` unconstrained since an
    empty enum would be meaningless; nlq.py's re-validation is unchanged
    either way — this only narrows what the LLM is likely to produce."""
    if not catalog:
        return _TOOLS
    names = [m.name for m in catalog]
    tools = [dict(t) for t in _TOOLS]
    for t in tools:
        if t["name"] == "propose_query":
            t["input_schema"] = {
                **t["input_schema"],
                "properties": {
                    **t["input_schema"]["properties"],
                    "model": {"type": "string", "enum": names},
                },
            }
    return tools


_SYSTEM_PROMPT = (
    "You are a BI assistant answering questions strictly from a declared "
    "semantic layer. You may only reference models/dimensions/measures "
    "given in the catalog below — never a raw column, another data source, "
    "code, or SQL.\n\n"
    "A dimension or measure may list 'also called' terms — alternate "
    "business vocabulary a question might use instead of the declared name "
    "(e.g. 'sales' or 'turnover' for a measure named 'revenue'). Recognize "
    "these when matching the question's wording. Some measures also include "
    "a 'computed as' formula — the measure's actual definition, given "
    "because its name/description alone can be ambiguous (e.g. an "
    "unweighted average vs. a weighted one) or it may have no description "
    "at all — use it only to judge which declared measure best answers the "
    "question. Either way, a synonym or a formula is never itself a valid "
    "value anywhere in a tool call: always use the dimension's/measure's "
    "own declared `name` in propose_query — never a synonym string, never a "
    "formula you write or adapt yourself, and never a column referenced "
    "inside a formula (as a dimension, filter field, or otherwise).\n\n"
    "A categorical dimension may list 'sample values' — real values stored "
    "in that column. When an eq/ne/in/not_in filter targets it, always use "
    "one of these real values, converted from the question's own wording to "
    "match exactly (case included) — e.g. if the question says 'cardiology "
    "trials' and the sample values show 'Cardiology', filter on 'Cardiology'; "
    "if it says a country's ISO-2 code but the sample values are ISO-3 (or "
    "vice versa, or full country names), convert to whichever form actually "
    "appears. Never filter on the question's literal wording when it "
    "doesn't match a real value. If nothing in the sample values plausibly "
    "corresponds, prefer a case-insensitive `contains` filter over guessing "
    "an exact value, or ask_clarification.\n\n"
    "Rules for a propose_query call (violating these makes the query fail):\n"
    f"- filters[].op must be exactly one of: {', '.join(_FILTER_OPS)} — never "
    "a symbol like '=' or '>', and never a SQL keyword.\n"
    "- eq/ne/gt/gte/lt/lte/contains compare against `value`; in/not_in "
    "compare against `values` (a list). contains is a case-insensitive "
    "substring match.\n"
    "- A date/time filter's `value` may be an ISO date ('2025-01-31') or a "
    f"relative keyword ({', '.join(_RELATIVE_DATE_KEYWORDS)}), or an offset "
    "like 'today-90d' / 'today+2mo'.\n"
    f"- A time dimension's `grain` (when given) must be one of: {', '.join(_GRAINS)}.\n"
    "- sort.by must name one of the query's own dimensions or measures; "
    "sort.desc defaults to true (descending) when omitted.\n\n"
    "If the question needs a calculation the catalog has no declared measure "
    "for — a running total, a period-over-period change or growth rate, "
    "etc. — define it yourself with propose_query's `inline_measures`: give "
    "it a new name and an expr built from running_total(measure) and/or "
    "lag(measure[, periods]) over one of the catalog's own declared measure "
    "names (never a raw column, and never a synonym or formula string). "
    "Plain arithmetic (+ - * /) is allowed around those, so e.g. a "
    "quarter-over-quarter change is lag(revenue) and a % change is "
    "(revenue - lag(revenue)) / lag(revenue). Then include the inline "
    "measure's own name in `measures` so it appears in the result — the "
    "sibling measure it references doesn't need to be listed separately, "
    "it's pulled in automatically. Never invent a running total/lag over "
    "something that isn't a declared measure.\n\n"
    "Follow-ups: the prompt lists this conversation's prior turns, oldest "
    "first — each one's question and the exact query that answered it. Most "
    "questions after the first are follow-ups that adjust the most recent "
    "turn rather than starting over ('break this down by quarter', 'now "
    "just the top 5', 'and last year?', 'same for the north region', 'what "
    "about orders instead'). Answer those with propose_query: start from "
    "the most recent prior turn's model, dimensions, measures, filters, "
    "sort and limit, and change only what the follow-up actually asks to "
    "change. Repeat the parts it doesn't mention rather than dropping them "
    "— every propose_query call must be complete on its own, there is no "
    "way to send only the difference. 'Break down / split / group by X' "
    "means ADDING X to that turn's dimensions while keeping the ones "
    "already there, and for a time dimension it means setting the grain the "
    "question asks for (by quarter -> grain '1q', by month -> '1mo'). Treat "
    "a question as a fresh start only when it genuinely changes subject.\n\n"
    "You must call exactly one tool: propose_query when the question maps "
    "unambiguously to the catalog — including any follow-up that adjusts a "
    "prior turn, since anything asking for numbers (new or recomputed) is a "
    "propose_query — ask_clarification when it could reasonably map to more "
    "than one model/dimension/measure, show_last_query ONLY when the user "
    "wants to see the query definition behind a previous answer and is not "
    "asking for data at all, or decline when it cannot be answered from the "
    "catalog at all.\n\n"
    "Self-learning: whatever tool you call may also carry `memories` — "
    "durable facts about a catalog model learned from this exchange, stored "
    "against that model and shown to every future conversation about it. "
    "Record a memory only when this exchange actually revealed one: a "
    "business term the question used for a declared dimension/measure that "
    "its catalog entry doesn't already list (kind 'synonym', subject = the "
    "declared name, content = the term), or a short user-independent fact "
    "about the model's vocabulary or data (kind 'note'). A model may also "
    "already show 'learned fact' lines — treat those as catalog truth, and "
    "don't re-record them. STRICT PRIVACY RULE: memories describe the data "
    "model, never the person asking. Do not store the user's preferences, "
    "identity, role, habits, or anything else about them (no 'the user "
    "prefers charts', no 'Alice usually asks about EMEA') — if a fact is "
    "only true for this user or this session, it is not a memory. Most "
    "turns should record none."
)


def _catalog_text(catalog: list[ModelCatalogEntry]) -> str:
    lines = []
    for m in catalog:
        lines.append(f"## model: {m.name} ({m.label}) — {m.description}")
        for note in m.learned_notes:
            # chat-learned, admin-curated facts (memorystore kind:"note") —
            # rendered before the declared entries so they read as context
            # for everything below, same as the model description
            lines.append(f"  learned fact: {note}")
        for d in m.dimensions:
            line = f"  dimension: {d['name']} ({d['type']}) — {d.get('description', '')}"
            if d.get("synonyms"):
                line += f" | also called: {', '.join(d['synonyms'])}"
            if d.get("sample_values"):
                line += f" | sample values: {', '.join(str(v) for v in d['sample_values'])}"
            lines.append(line)
        for meas in m.measures:
            line = f"  measure: {meas['name']} ({meas.get('label', '')}) — {meas.get('description', '')}"
            if meas.get("synonyms"):
                line += f" | also called: {', '.join(meas['synonyms'])}"
            if meas.get("expr"):
                # ground truth for what this measure actually computes — see
                # nlq._measure_catalog_entry; use it to tell similarly-named
                # or undescribed measures apart, never to invent a new one
                line += f" | computed as: {meas['expr']}"
            lines.append(line)
    return "\n".join(lines) if lines else "(no models available to this user)"


def _prior_context_text(prior_context: list[PriorTurn]) -> str:
    """Prior turns oldest-first, with the last one explicitly marked as the
    one a follow-up adjusts — "break this down by quarter" is resolved
    against a specific turn, so which turn that is can't be left implicit.
    Every field a propose_query call would have to repeat is rendered,
    `sort` and `limit` included: PriorTurn has always carried them but they
    were never shown, so a follow-up to "top 5 products by revenue" had no
    way to know the limit it was supposed to keep."""
    if not prior_context:
        return "(no prior turns in this conversation)"
    lines = []
    last = len(prior_context) - 1
    for i, t in enumerate(prior_context):
        marker = " [most recent — a follow-up adjusts THIS turn]" if i == last else ""
        line = (
            f"- Q: {t.question_text!r}{marker} -> model={t.model}, dimensions={t.dimensions}, "
            f"measures={t.measures}, filters={t.filters}, sort={t.sort}, limit={t.limit}"
        )
        if t.inline_measures:
            line += f", inline_measures={t.inline_measures}"
        lines.append(line)
    return "\n".join(lines)


def _build_prompt(question: str, catalog: list[ModelCatalogEntry], prior_context: list[PriorTurn]) -> str:
    return (
        f"Catalog:\n{_catalog_text(catalog)}\n\n"
        f"Prior turns in this conversation (oldest first):\n{_prior_context_text(prior_context)}\n\n"
        f"Question: {question}"
    )


class LLMTranslator:
    """Forced tool-use against the configured provider, so the result is
    always one of the four typed decisions (research.md R1). Which provider
    that is — and therefore which wire format, auth scheme and base URL — is
    resolved once by app/llmclient.py from CI_LLM_BASE_URL / CI_LLM_PROVIDER;
    nothing in this class is Anthropic-specific."""

    def __init__(self, api_key: str | None = None, model: str | None = None, client=None):
        self.api_key = api_key or config.LLM_API_KEY
        self.model = model or config.LLM_MODEL
        self.client = client or llmclient.build_client(api_key=self.api_key)

    def _request(
        self,
        question: str,
        catalog: list[ModelCatalogEntry],
        prior_context: list[PriorTurn],
        *,
        thinking: bool,
    ) -> llmclient.ChatRequest:
        """The request shared by translate() and translate_streaming() — the
        two differ only in streaming itself and in asking for extended
        thinking, which is worth its latency only when there is a live view
        to show it in."""
        return llmclient.ChatRequest(
            model=self.model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            tools=_tools_for_catalog(catalog),
            prompt=_build_prompt(question, catalog, prior_context),
            thinking=thinking,
        )

    def translate(
        self,
        question: str,
        catalog: list[ModelCatalogEntry],
        prior_context: list[PriorTurn],
    ) -> RawToolCall:
        try:
            call = self.client.call(self._request(question, catalog, prior_context, thinking=False))
        except llmclient.LLMError as exc:
            raise TranslatorError(str(exc)) from exc
        return RawToolCall(kind=call.name, args=call.args)

    def translate_streaming(
        self,
        question: str,
        catalog: list[ModelCatalogEntry],
        prior_context: list[PriorTurn],
    ) -> Iterator[StreamEvent]:
        """Same call as translate(), but yields StreamEvents for live display
        (extended thinking on models that support it, and the tool call's
        args as they're built) as it goes, ending with a "done" event
        carrying exactly what translate() would have returned outright. A
        caller that only wants the final decision can skip every event but
        "done" — nothing here is trusted any more than translate()'s return
        value is; the re-validation in nlq.py is unchanged."""
        request = self._request(question, catalog, prior_context, thinking=True)
        try:
            for event in self.client.stream(request):
                if event.kind == "done":
                    final = event.final
                    yield StreamEvent(kind="done", final=RawToolCall(kind=final.name, args=final.args))
                    return
                yield StreamEvent(
                    kind=event.kind, text=event.text,
                    tool_name=event.tool_name, tool_input=event.tool_input,
                )
        except llmclient.LLMError as exc:
            raise TranslatorError(str(exc)) from exc
        raise TranslatorError("model did not call any tool")


# The pre-multi-provider name, kept so existing call sites and any deployer's
# patches keep importing. There is nothing Anthropic-specific left behind it.
AnthropicTranslator = LLMTranslator
