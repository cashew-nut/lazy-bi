"""Conversational analytics: conversations CRUD + the core "ask" action
(specs/012-conversational-analytics/). Every route requires at least the
viewer role — asking a question is read-only, the same tier as POST
/api/query (FR-005) — and every conversation route is strictly scoped to
its owner (FR-013). The whole feature is off (503) unless CI_LLM_API_KEY is
configured, so an unconfigured deployment never calls a third party for
this (research.md R7).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import config, engine, nlq, semantic
from ..auth import User, require_role
from ..llm import AnthropicTranslator, PriorTurn, TranslatorError
from ..registry import registry

router = APIRouter(tags=["chat"])

# One translator instance is enough — it's a thin, stateless API client.
_translator = AnthropicTranslator()

# How many prior turns feed into follow-up context (research.md R5).
_PRIOR_CONTEXT_TURNS = 5


class ConversationIn(BaseModel):
    model_scope: list[str] = []


class ConversationPatch(BaseModel):
    title: Optional[str] = None
    model_scope: Optional[list[str]] = None


class AskIn(BaseModel):
    question: str


def _require_enabled() -> None:
    if not config.LLM_ENABLED:
        raise HTTPException(status_code=503, detail="conversational analytics is not configured")


def _validate_scope(model_scope: list[str]) -> None:
    unknown = [m for m in model_scope if m not in registry.models]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown model(s) in model_scope: {unknown}")


def _get_owned(conversation_id: int, user: User) -> dict:
    conv = registry.conversation_store.get(conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@router.get("/conversations", dependencies=[Depends(_require_enabled)])
def list_conversations(user: User = Depends(require_role("viewer"))):
    return registry.conversation_store.list_for_user(user.id)


@router.post("/conversations", status_code=201, dependencies=[Depends(_require_enabled)])
def create_conversation(body: ConversationIn, user: User = Depends(require_role("viewer"))):
    _validate_scope(body.model_scope)
    return registry.conversation_store.create(user.id, body.model_scope)


@router.get("/conversations/{conversation_id}", dependencies=[Depends(_require_enabled)])
def get_conversation(conversation_id: int, user: User = Depends(require_role("viewer"))):
    return _get_owned(conversation_id, user)


@router.patch("/conversations/{conversation_id}", dependencies=[Depends(_require_enabled)])
def update_conversation(conversation_id: int, body: ConversationPatch,
                         user: User = Depends(require_role("viewer"))):
    _get_owned(conversation_id, user)
    if body.model_scope is not None:
        _validate_scope(body.model_scope)
    updated = registry.conversation_store.update(
        conversation_id, user.id, title=body.title, model_scope=body.model_scope)
    if not updated:
        raise HTTPException(status_code=404, detail="conversation not found")
    return updated


@router.delete("/conversations/{conversation_id}", status_code=204, dependencies=[Depends(_require_enabled)])
def delete_conversation(conversation_id: int, user: User = Depends(require_role("viewer"))):
    if not registry.conversation_store.delete(conversation_id, user.id):
        raise HTTPException(status_code=404, detail="conversation not found")


def _prior_turns(conv: dict) -> list[PriorTurn]:
    """Recent successfully-answered turns, as follow-up context (research.md
    R5) — resolved structure only, never raw result rows. Declines and
    clarifying questions aren't reusable context, so only outcome in
    (answered, answered_empty) contributes a turn."""
    turns = []
    last_question = ""
    for msg in conv["messages"]:
        if msg["role"] == "user":
            last_question = msg["question_text"] or ""
        elif msg["outcome"] in ("answered", "answered_empty") and msg["resolved_query"]:
            rq = msg["resolved_query"]
            turns.append(PriorTurn(
                question_text=last_question,
                model=rq.get("model"), dimensions=rq.get("dimensions", []),
                measures=rq.get("measures", []), filters=rq.get("filters", []),
            ))
    return turns[-_PRIOR_CONTEXT_TURNS:]


def _summarize(resolved: dict, result: dict) -> str:
    """Templated (non-LLM) grounding summary — cheaper and faster than a
    second model round trip, and trivially guaranteed to only ever describe
    what `result` actually contains (T016)."""
    if result["row_count"] == 0:
        return "That query ran successfully but returned no matching data."
    dim_cols = [c["name"] for c in result["columns"] if c["kind"] == "dimension"]
    measure_cols = [c for c in result["columns"] if c["kind"] == "measure"]
    if not dim_cols and result["row_count"] == 1:
        row = result["rows"][0]
        parts = [f"{c['label']}: {row.get(c['name'])}" for c in measure_cols]
        return "; ".join(parts)
    top = result["rows"][0]
    headline = ", ".join(f"{c['label']}: {top.get(c['name'])}" for c in measure_cols)
    return (f"Found {result['row_count']} row(s) broken down by "
            f"{', '.join(dim_cols)}. Top row — {headline}.")


@router.post("/conversations/{conversation_id}/ask", dependencies=[Depends(_require_enabled)])
def ask(conversation_id: int, body: AskIn, user: User = Depends(require_role("viewer"))):
    conv = _get_owned(conversation_id, user)
    store = registry.conversation_store
    question_msg = store.add_message(conversation_id, "user", question_text=body.question)

    catalog = nlq.build_catalog(registry.models, conv["model_scope"])
    prior_context = _prior_turns(conv)

    try:
        decision = nlq.resolve(
            body.question, catalog, prior_context, user, registry.models, _translator,
            scope=conv["model_scope"],
        )
    except TranslatorError as exc:
        response_msg = store.add_message(
            conversation_id, "assistant", outcome="error",
            answer_text=f"the assistant is temporarily unavailable: {exc}",
        )
        registry.auth_store.record_audit(
            "chat_ask", user.username, actor_user_id=user.id,
            target=f"conversation:{conversation_id} outcome:error question:{body.question!r}",
        )
        return {"question": question_msg, "response": response_msg}

    if isinstance(decision, nlq.Decline):
        response_msg = store.add_message(
            conversation_id, "assistant", outcome="declined", answer_text=decision.reason_text)
        audit_target = f"conversation:{conversation_id} outcome:declined question:{body.question!r}"
    elif isinstance(decision, nlq.AskClarification):
        answer_text = decision.question_text
        if decision.candidates:
            answer_text += f" (options: {', '.join(decision.candidates)})"
        response_msg = store.add_message(
            conversation_id, "clarification", outcome="clarification", answer_text=answer_text)
        audit_target = (f"conversation:{conversation_id} outcome:clarification "
                         f"question:{body.question!r} candidates:{decision.candidates}")
    else:
        model = registry.models[decision.model]
        resolved_query = {
            "model": decision.model, "dimensions": decision.dimensions,
            "measures": decision.measures, "filters": decision.filters,
            "sort": decision.sort, "limit": decision.limit,
        }
        try:
            result = engine.run_query(model, resolved_query)
        except (semantic.ModelError, engine.QueryError) as exc:
            response_msg = store.add_message(
                conversation_id, "assistant", outcome="error", answer_text=f"query failed: {exc}")
            registry.auth_store.record_audit(
                "chat_ask", user.username, actor_user_id=user.id,
                target=f"conversation:{conversation_id} outcome:error question:{body.question!r}",
            )
            return {"question": question_msg, "response": response_msg}
        outcome = "answered_empty" if result["row_count"] == 0 else "answered"
        response_msg = store.add_message(
            conversation_id, "assistant", outcome=outcome,
            resolved_query=resolved_query, result=result,
            answer_text=_summarize(resolved_query, result),
        )
        audit_target = (f"conversation:{conversation_id} outcome:{outcome} question:{body.question!r} "
                         f"model:{decision.model} dimensions:{decision.dimensions} measures:{decision.measures}")

    registry.auth_store.record_audit(
        "chat_ask", user.username, actor_user_id=user.id, target=audit_target,
    )
    return {"question": question_msg, "response": response_msg}
