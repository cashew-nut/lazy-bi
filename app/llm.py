"""The one seam that talks to a third-party LLM (specs/012-conversational-
analytics/). Everything above this module (app/nlq.py) only ever sees the
typed `RawToolCall` result — never raw model output — and treats it as
*unvalidated*: nlq.resolve() re-checks it against the live semantic model
before it can become an executable query (research.md R2).

Swappable by design: tests use a FakeTranslator implementing the same
Translator protocol, so the translator contract is exercised with zero
network calls (plan.md's Testing section).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator, Literal, Protocol

from . import config, engine
from .semantic import TIME_GRAINS

logger = logging.getLogger(__name__)

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
            "The user is asking to see, return, or repeat the actual query "
            "(model/dimensions/measures/filters) behind a previous answer in "
            "this conversation — not a new business question. Use this for "
            "things like 'show me the query', 'what did you just run', or "
            "'return the query you used'. Takes no arguments."
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
    "You must call exactly one tool: propose_query when the question maps "
    "unambiguously to the catalog, ask_clarification when it could "
    "reasonably map to more than one model/dimension/measure, "
    "show_last_query when the user is asking to see/return the query used "
    "for a previous answer rather than asking a new business question, or "
    "decline when it cannot be answered from the catalog at all."
)


def _catalog_text(catalog: list[ModelCatalogEntry]) -> str:
    lines = []
    for m in catalog:
        lines.append(f"## model: {m.name} ({m.label}) — {m.description}")
        for d in m.dimensions:
            line = f"  dimension: {d['name']} ({d['type']}) — {d.get('description', '')}"
            if d.get("synonyms"):
                line += f" | also called: {', '.join(d['synonyms'])}"
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
    if not prior_context:
        return "(no prior turns in this conversation)"
    lines = []
    for t in prior_context:
        lines.append(
            f"- Q: {t.question_text!r} -> model={t.model}, dimensions={t.dimensions}, "
            f"measures={t.measures}, filters={t.filters}"
        )
    return "\n".join(lines)


def _build_prompt(question: str, catalog: list[ModelCatalogEntry], prior_context: list[PriorTurn]) -> str:
    return (
        f"Catalog:\n{_catalog_text(catalog)}\n\n"
        f"Prior turns in this conversation:\n{_prior_context_text(prior_context)}\n\n"
        f"Question: {question}"
    )


# Subset of config.LLM_MODEL_CHOICES that supports Anthropic's "adaptive"
# extended-thinking mode. Haiku doesn't, and requesting it there 400s with
# "adaptive thinking is not supported on this model" — the bug this fixes.
# Keep in sync with LLM_MODEL_CHOICES the same way that list's own comment
# asks: add an entry here whenever a newly-added choice supports adaptive
# thinking.
_ADAPTIVE_THINKING_MODELS = {"claude-opus-4-8", "claude-sonnet-5"}


def _thinking_kwargs(model: str) -> dict:
    """The `thinking` kwarg for messages.stream(), omitted entirely for a
    model that doesn't support adaptive thinking rather than sent
    unconditionally and left to 400."""
    if model in _ADAPTIVE_THINKING_MODELS:
        return {"thinking": {"type": "adaptive", "display": "summarized"}}
    return {}


class AnthropicTranslator:
    """Talks to the Anthropic Messages API with forced tool-use so the
    result is always one of the four typed decisions (research.md R1)."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or config.LLM_API_KEY
        self.model = model or config.LLM_MODEL

    def translate(
        self,
        question: str,
        catalog: list[ModelCatalogEntry],
        prior_context: list[PriorTurn],
    ) -> RawToolCall:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        prompt = _build_prompt(question, catalog, prior_context)
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=_tools_for_catalog(catalog),
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            # The user only ever sees a generic "temporarily unavailable"
            # message (chat.py) — log the real cause server-side so a
            # deployer can actually diagnose a bad key / network / proxy
            # issue instead of staring at "Connection error." with nothing
            # in the terminal.
            logger.warning("Anthropic API call failed: %r (cause: %r)", exc, exc.__cause__)
            raise TranslatorError(str(exc)) from exc

        for block in response.content:
            if block.type == "tool_use":
                return RawToolCall(kind=block.name, args=block.input)
        raise TranslatorError("model did not call any tool")

    def translate_streaming(
        self,
        question: str,
        catalog: list[ModelCatalogEntry],
        prior_context: list[PriorTurn],
    ) -> Iterator[StreamEvent]:
        """Same call as translate(), but yields StreamEvents for live display
        (adaptive thinking on models that support it — _thinking_kwargs — and
        the tool call's args as they're built — eager_input_streaming on
        every _TOOLS entry means `event.snapshot` below is already a parsed
        partial dict, not just a raw JSON fragment) as it goes, ending with a
        "done" event carrying exactly what translate() would have returned
        outright. A caller that only wants the final decision can skip every
        event but "done" — nothing here is trusted any more than
        translate()'s return value is; the re-validation in nlq.py is
        unchanged."""
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        prompt = _build_prompt(question, catalog, prior_context)
        try:
            with client.messages.stream(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=_tools_for_catalog(catalog),
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": prompt}],
                **_thinking_kwargs(self.model),
            ) as stream:
                for event in stream:
                    if event.type == "thinking":
                        yield StreamEvent(kind="thinking", text=event.thinking)
                    elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield StreamEvent(kind="tool_name", tool_name=event.content_block.name)
                    elif event.type == "input_json":
                        snapshot = event.snapshot if isinstance(event.snapshot, dict) else {}
                        yield StreamEvent(kind="tool_input", tool_input=snapshot)
                message = stream.get_final_message()
        except anthropic.APIError as exc:
            logger.warning("Anthropic API call failed: %r (cause: %r)", exc, exc.__cause__)
            raise TranslatorError(str(exc)) from exc

        for block in message.content:
            if block.type == "tool_use":
                yield StreamEvent(kind="done", final=RawToolCall(kind=block.name, args=block.input))
                return
        raise TranslatorError("model did not call any tool")
