"""NL-to-semantic-query translation core (specs/012-conversational-
analytics/). Model-agnostic entry point future features (3.2 prompt-to-
dashboard, 3.3 dashboard-analyst) can call directly — see contracts/
chat-api.md's internal contract.

The one rule this module exists to enforce (Constitution Principle I): an
LLM's output is never trusted as an already-safe query. `resolve()` always
re-validates a `propose_query` tool call against the *live* semantic model
before it can become a `ProposeQuery` decision — anything that doesn't
resolve cleanly downgrades to `Decline`, it never raises past this module
into an executed query.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from . import engine, semantic
from .auth import User
from .llm import ModelCatalogEntry, PriorTurn, Translator, TranslatorError

__all__ = [
    "ProposeQuery", "AskClarification", "Decline", "ShowQuery", "Decision",
    "build_catalog", "resolve",
]


@dataclass(frozen=True)
class ProposeQuery:
    model: str
    dimensions: list
    measures: list[str]
    filters: list[dict] = field(default_factory=list)
    sort: dict | None = None
    limit: int | None = None


@dataclass(frozen=True)
class AskClarification:
    question_text: str
    candidates: list[str]


@dataclass(frozen=True)
class Decline:
    reason_text: str


@dataclass(frozen=True)
class ShowQuery:
    """The previous turn's already-validated resolved query, surfaced
    verbatim on request (e.g. "can you show me the query you ran?") instead
    of being re-guessed as a fresh semantic query."""
    question_text: str
    model: str | None
    dimensions: list
    measures: list[str]
    filters: list[dict] = field(default_factory=list)
    sort: dict | None = None
    limit: int | None = None


Decision = Union[ProposeQuery, AskClarification, Decline, ShowQuery]


def build_catalog(models: dict[str, semantic.Model], scope: list[str]) -> list[ModelCatalogEntry]:
    """The catalog shown to the LLM, built live from the loaded models
    (research.md R4) — never cached, so a model rename/reload can never make
    the assistant propose against a stale schema. `scope` (research.md R6),
    when non-empty, restricts the catalog to just those model names."""
    names = scope if scope else list(models.keys())
    entries = []
    for name in names:
        model = models.get(name)
        if model is None:  # a pinned scope named a model that no longer exists
            continue
        entries.append(ModelCatalogEntry(
            name=model.name,
            label=model.label,
            description=model.description,
            dimensions=[
                {"name": d.name, "label": d.label, "type": d.type, "description": d.description}
                for d in model.dimensions.values()
            ],
            measures=[_measure_catalog_entry(m) for m in model.measures.values()],
        ))
    return entries


def _measure_catalog_entry(m: semantic.Measure) -> dict:
    """A name/description alone often isn't enough to pick the right measure
    (e.g. 'avg_unit_price' = mean(unit_price) vs. 'aov' = revenue per order —
    indistinguishable by name, and roughly half of this project's demo
    measures carry no description at all) — so the measure's actual DSL
    formula is included as ground truth the LLM can read directly, not just
    infer from a label. Framed measures (m.frame_source is set) are the one
    exception: their expr_source is a fragment over an intermediary frame
    (see semantic.Measure) and is meaningless without that frame's context,
    so it's omitted rather than shown dangling — the description is relied
    on for those instead. Note this puts the measure's raw source-column
    references (e.g. `unit_price`, never a declared dimension) in front of
    the LLM, which is new relative to the rest of the catalog — see README's
    "Conversational analytics" section (FR-015)."""
    entry = {"name": m.name, "label": m.label, "description": m.description}
    if m.frame_source is None:
        # YAML's `>` folded block style (used by some multi-line measures)
        # keeps a trailing newline in expr_source; strip it so it doesn't
        # leak a stray blank line into the middle of the LLM's prompt text
        entry["expr"] = m.expr_source.strip()
    return entry


def _dim_name(entry) -> str:
    return entry if isinstance(entry, str) else entry.get("name", "")


def _validate_propose_query(args: dict, models: dict[str, semantic.Model],
                             scope: list[str]) -> ProposeQuery | Decline:
    model_name = args.get("model")
    model = models.get(model_name)
    if model is None:
        return Decline(f"'{model_name}' is not a model this conversation can query.")
    if scope and model_name not in scope:
        return Decline(f"'{model_name}' is outside this conversation's selected model scope.")

    dimensions = args.get("dimensions") or []
    measures = args.get("measures") or []
    filters = args.get("filters") or []

    def bad(reason: str) -> Decline:
        return Decline(f"can't answer that from the declared '{model_name}' model: {reason}")

    try:
        for entry in dimensions:
            model.dimension(_dim_name(entry))
            grain = entry.get("grain") if isinstance(entry, dict) else None
            if grain and grain not in semantic.TIME_GRAINS:
                return bad(f"'{grain}' isn't a supported time grain "
                           f"(use one of {', '.join(semantic.TIME_GRAINS)})")
        for m in measures:
            model.measure(m)
        for f in filters:
            model.dimension(f.get("field", ""))
            op = f.get("op")
            # defense in depth alongside llm.py's schema/prompt: a proposal
            # that still names an op outside engine.FILTER_OPS (e.g. '='
            # instead of 'eq') declines cleanly here rather than reaching
            # engine.run_query and surfacing as a raw, unexplained QueryError
            if op not in engine.FILTER_OPS:
                return bad(f"filter op '{op}' isn't supported "
                           f"(use one of {', '.join(sorted(engine.FILTER_OPS))})")
    except semantic.ModelError as exc:
        return bad(str(exc))

    if not measures:
        return Decline(f"the question doesn't map to any declared measure in '{model_name}'.")

    sort = args.get("sort")
    if sort and sort.get("by") not in measures:
        try:
            model.dimension(sort.get("by", ""))
        except semantic.ModelError:
            return Decline(f"can't sort by an undeclared field in '{model_name}'.")

    return ProposeQuery(
        model=model_name, dimensions=dimensions, measures=measures,
        filters=filters, sort=sort, limit=args.get("limit"),
    )


def _validate_ask_clarification(args: dict, catalog: list[ModelCatalogEntry]) -> AskClarification | Decline:
    known = {m.name for m in catalog}
    for m in catalog:
        known.update(d["name"] for d in m.dimensions)
        known.update(meas["name"] for meas in m.measures)
    candidates = [c for c in (args.get("candidates") or []) if c in known]
    if not candidates:
        return Decline("that's ambiguous and no valid candidates could be identified from the declared models.")
    return AskClarification(question_text=args.get("question_text", ""), candidates=candidates)


def _validate_show_last_query(prior_context: list[PriorTurn]) -> ShowQuery | Decline:
    """The most recent prior turn was already re-validated when it was first
    proposed (it only ever entered prior_context via a successful
    ProposeQuery) — showing it verbatim needs no re-check against the live
    model, only that one actually exists to show."""
    if not prior_context:
        return Decline("there's no prior query yet in this conversation to show.")
    last = prior_context[-1]
    return ShowQuery(
        question_text=last.question_text, model=last.model, dimensions=last.dimensions,
        measures=last.measures, filters=last.filters, sort=last.sort, limit=last.limit,
    )


def resolve(
    question: str,
    catalog: list[ModelCatalogEntry],
    prior_context: list[PriorTurn],
    user: User,
    models: dict[str, semantic.Model],
    translator: Translator,
    scope: list[str] | None = None,
) -> Decision:
    """Translate `question` into a Decision. Raises TranslatorError only for
    the LLM call itself failing (network/timeout) — every other failure mode
    (bad/unsafe proposal) resolves to a Decline, never an exception, so a
    caller only needs to catch TranslatorError for the "assistant is
    unreachable" case (contracts/chat-api.md's 503)."""
    raw = translator.translate(question, catalog, prior_context)
    scope = scope or []

    if raw.kind == "decline":
        return Decline(reason_text=raw.args.get("reason_text", "I can't answer that from the declared models."))
    if raw.kind == "ask_clarification":
        return _validate_ask_clarification(raw.args, catalog)
    if raw.kind == "show_last_query":
        return _validate_show_last_query(prior_context)
    if raw.kind == "propose_query":
        return _validate_propose_query(raw.args, models, scope)
    return Decline(f"unrecognized response type '{raw.kind}'.")
