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
from typing import Literal, Protocol

from . import config

logger = logging.getLogger(__name__)

ToolKind = Literal["propose_query", "ask_clarification", "decline"]


@dataclass(frozen=True)
class ModelCatalogEntry:
    """One model's queryable shape, as shown to the LLM — never raw source
    columns/paths/credentials, only what semantic.model_to_spec already
    exposes to the existing /api/models endpoint (research.md R4)."""
    name: str
    label: str
    description: str
    dimensions: list[dict] = field(default_factory=list)  # [{name, label, type, description}]
    measures: list[dict] = field(default_factory=list)     # [{name, label, description}]


@dataclass(frozen=True)
class PriorTurn:
    """A prior turn's resolved structure, offered as follow-up context
    (research.md R5) — never raw result rows."""
    question_text: str
    model: str | None
    dimensions: list
    measures: list[str]
    filters: list[dict]


@dataclass(frozen=True)
class RawToolCall:
    """The LLM's unvalidated tool call. `kind` says which of the three tools
    it invoked; `args` is that tool's raw input dict."""
    kind: ToolKind
    args: dict


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


_TOOLS = [
    {
        "name": "propose_query",
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
                                    "grain": {"type": "string", "description": "e.g. 1mo, 1qtr, 1y — only for time dimensions"},
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
                            "op": {"type": "string"},
                            "value": {},
                            "values": {"type": "array"},
                        },
                        "required": ["field", "op"],
                    },
                },
                "sort": {
                    "type": ["object", "null"],
                    "properties": {"by": {"type": "string"}, "desc": {"type": "boolean"}},
                },
                "limit": {"type": ["integer", "null"]},
            },
            "required": ["model", "dimensions", "measures"],
        },
    },
    {
        "name": "ask_clarification",
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
        "name": "decline",
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

_SYSTEM_PROMPT = (
    "You are a BI assistant answering questions strictly from a declared "
    "semantic layer. You may only reference models/dimensions/measures "
    "given in the catalog below — never a raw column, another data source, "
    "code, or SQL. You must call exactly one tool: propose_query when the "
    "question maps unambiguously to the catalog, ask_clarification when it "
    "could reasonably map to more than one model/dimension/measure, or "
    "decline when it cannot be answered from the catalog at all."
)


def _catalog_text(catalog: list[ModelCatalogEntry]) -> str:
    lines = []
    for m in catalog:
        lines.append(f"## model: {m.name} ({m.label}) — {m.description}")
        for d in m.dimensions:
            lines.append(f"  dimension: {d['name']} ({d['type']}) — {d.get('description', '')}")
        for meas in m.measures:
            lines.append(f"  measure: {meas['name']} — {meas.get('description', '')}")
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


class AnthropicTranslator:
    """Talks to the Anthropic Messages API with forced tool-use so the
    result is always one of the three typed decisions (research.md R1)."""

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
        prompt = (
            f"Catalog:\n{_catalog_text(catalog)}\n\n"
            f"Prior turns in this conversation:\n{_prior_context_text(prior_context)}\n\n"
            f"Question: {question}"
        )
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=_TOOLS,
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
