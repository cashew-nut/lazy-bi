"""Conversational analytics: conversations CRUD + the core "ask" action
(specs/012-conversational-analytics/). Every route requires at least the
viewer role — asking a question is read-only, the same tier as POST
/api/query (FR-005) — and every conversation route is strictly scoped to
its owner (FR-013). The whole feature is off (503) unless CI_LLM_API_KEY is
configured, so an unconfigured deployment never calls a third party for
this (research.md R7).
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config, engine, nlq, semantic
from ..auth import User, require_role
from ..llm import AnthropicTranslator, PriorTurn, StreamEvent, TranslatorError
from ..registry import registry
from .visuals import _validate_visual_spec

router = APIRouter(tags=["chat"])

# The default translator (server-configured model) — a thin, stateless API
# client, so one instance is enough. A conversation that picks a non-default
# model gets its own instance from _translator_for() below.
_translator = AnthropicTranslator()

# How many prior turns feed into follow-up context (research.md R5).
_PRIOR_CONTEXT_TURNS = 5


class ConversationIn(BaseModel):
    model_scope: list[str] = []
    llm_model: Optional[str] = None


class ConversationPatch(BaseModel):
    title: Optional[str] = None
    model_scope: Optional[list[str]] = None
    llm_model: Optional[str] = None


class AskIn(BaseModel):
    question: str


class PinIn(BaseModel):
    name: str = ""
    dashboard_id: Optional[int] = None
    new_dashboard_name: Optional[str] = None


def _require_enabled() -> None:
    if not config.LLM_ENABLED:
        raise HTTPException(status_code=503, detail="conversational analytics is not configured")


def _validate_scope(model_scope: list[str]) -> None:
    unknown = [m for m in model_scope if m not in registry.models]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown model(s) in model_scope: {unknown}")


def _validate_llm_model(llm_model: Optional[str]) -> None:
    if llm_model is not None and llm_model not in config.LLM_MODEL_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown llm_model '{llm_model}' (choose one of {config.LLM_MODEL_CHOICES})",
        )


def _translator_for(llm_model: Optional[str]):
    """The default `_translator` already targets config.LLM_MODEL — only
    spin up a dedicated client when a conversation picked something else,
    so the common case (no per-conversation override) stays a single
    reused instance, and tests that monkeypatch `_translator` keep working
    unmodified."""
    if not llm_model or llm_model == config.LLM_MODEL:
        return _translator
    return AnthropicTranslator(model=llm_model)


def _get_owned(conversation_id: int, user: User) -> dict:
    conv = registry.conversation_store.get(conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


def _start_ask(conversation_id: int, user: User, question: str):
    """Shared setup for ask() and ask_stream(): persist the user's turn and
    assemble everything a Translator needs to answer it. The catalog carries
    every stored model memory (learned synonyms merged into the declared
    ones, notes as learned-fact lines) — the self-learning loop's read half."""
    conv = _get_owned(conversation_id, user)
    question_msg = registry.conversation_store.add_message(
        conversation_id, "user", question_text=question)
    catalog = nlq.build_catalog(registry.models, conv["model_scope"],
                                memories=registry.memory_store.all_by_model())
    prior_context = _prior_turns(conv)
    translator = _translator_for(conv.get("llm_model"))
    return conv, question_msg, catalog, prior_context, translator


@router.get("/conversations", dependencies=[Depends(_require_enabled)])
def list_conversations(user: User = Depends(require_role("viewer"))):
    return registry.conversation_store.list_for_user(user.id)


@router.post("/conversations", status_code=201, dependencies=[Depends(_require_enabled)])
def create_conversation(body: ConversationIn, user: User = Depends(require_role("viewer"))):
    _validate_scope(body.model_scope)
    _validate_llm_model(body.llm_model)
    return registry.conversation_store.create(user.id, body.model_scope, body.llm_model)


@router.get("/conversations/{conversation_id}", dependencies=[Depends(_require_enabled)])
def get_conversation(conversation_id: int, user: User = Depends(require_role("viewer"))):
    return _get_owned(conversation_id, user)


@router.patch("/conversations/{conversation_id}", dependencies=[Depends(_require_enabled)])
def update_conversation(conversation_id: int, body: ConversationPatch,
                         user: User = Depends(require_role("viewer"))):
    _get_owned(conversation_id, user)
    if body.model_scope is not None:
        _validate_scope(body.model_scope)
    if body.llm_model is not None:
        _validate_llm_model(body.llm_model)
    updated = registry.conversation_store.update(
        conversation_id, user.id, title=body.title, model_scope=body.model_scope,
        llm_model=body.llm_model)
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
                sort=rq.get("sort"), limit=rq.get("limit"),
                inline_measures=rq.get("inline_measures", []),
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


def _handle_translator_error(conversation_id: int, user: User, question_msg: dict,
                              question: str, exc: TranslatorError) -> dict:
    store = registry.conversation_store
    response_msg = store.add_message(
        conversation_id, "assistant", outcome="error",
        answer_text=f"the assistant is temporarily unavailable: {exc}",
    )
    registry.auth_store.record_audit(
        "chat_ask", user.username, actor_user_id=user.id,
        target=f"conversation:{conversation_id} outcome:error question:{question!r}",
    )
    return {"question": question_msg, "response": response_msg, "learned": []}


def _persist_learned(conversation_id: int, user: User, decision: nlq.Decision) -> list[dict]:
    """The self-learning loop's write half: store the decision's already
    re-validated memories against their semantic models (never against the
    user — created_by is audit attribution only) and audit-log each write.
    MemoryStore.add returning None (duplicate / at cap) is a silent no-op,
    so re-learning a known fact costs nothing and reports nothing."""
    saved = []
    for mem in decision.learned:
        stored = registry.memory_store.add(
            mem["model"], mem["kind"], mem["subject"], mem["content"],
            source="chat", created_by=user.username, conversation_id=conversation_id,
        )
        if stored:
            saved.append(stored)
            registry.auth_store.record_audit(
                "chat_memory", user.username, actor_user_id=user.id,
                target=(f"conversation:{conversation_id} memory:{stored['id']} "
                        f"model:{stored['model']} kind:{stored['kind']} "
                        f"subject:{stored['subject']!r} content:{stored['content']!r}"),
            )
    return saved


def _resolved_query_dict(decision) -> dict:
    return {
        "model": decision.model, "dimensions": decision.dimensions,
        "measures": decision.measures, "filters": decision.filters,
        "sort": decision.sort, "limit": decision.limit,
        "inline_measures": decision.inline_measures,
    }


def _handle_decision(conversation_id: int, user: User, question_msg: dict,
                      question: str, decision: nlq.Decision) -> dict:
    """Persist `decision` as the assistant's turn (executing a ProposeQuery
    against the live engine first) and audit-log the outcome. Shared by
    ask() and ask_stream() — a decision is handled identically regardless of
    whether it was reached via a streamed or a plain translate() call."""
    store = registry.conversation_store
    # persisted before the outcome branches: what this exchange taught about
    # a model is independent of whether the query it accompanied succeeded
    learned = _persist_learned(conversation_id, user, decision)
    if isinstance(decision, nlq.Decline):
        response_msg = store.add_message(
            conversation_id, "assistant", outcome="declined", answer_text=decision.reason_text)
        audit_target = f"conversation:{conversation_id} outcome:declined question:{question!r}"
    elif isinstance(decision, nlq.AskClarification):
        answer_text = decision.question_text
        if decision.candidates:
            answer_text += f" (options: {', '.join(decision.candidates)})"
        response_msg = store.add_message(
            conversation_id, "clarification", outcome="clarification", answer_text=answer_text)
        audit_target = (f"conversation:{conversation_id} outcome:clarification "
                         f"question:{question!r} candidates:{decision.candidates}")
    elif isinstance(decision, nlq.ShowQuery):
        resolved_query = _resolved_query_dict(decision)
        response_msg = store.add_message(
            conversation_id, "assistant", outcome="query_shown", resolved_query=resolved_query,
            answer_text=f"Here's the query behind “{decision.question_text}”.",
        )
        audit_target = f"conversation:{conversation_id} outcome:query_shown question:{question!r}"
    else:
        model = registry.models[decision.model]
        resolved_query = _resolved_query_dict(decision)
        try:
            result = engine.run_query(model, resolved_query)
        except (semantic.ModelError, engine.QueryError) as exc:
            response_msg = store.add_message(
                conversation_id, "assistant", outcome="error", answer_text=f"query failed: {exc}")
            registry.auth_store.record_audit(
                "chat_ask", user.username, actor_user_id=user.id,
                target=f"conversation:{conversation_id} outcome:error question:{question!r}",
            )
            return {"question": question_msg, "response": response_msg, "learned": learned}
        outcome = "answered_empty" if result["row_count"] == 0 else "answered"
        response_msg = store.add_message(
            conversation_id, "assistant", outcome=outcome,
            resolved_query=resolved_query, result=result,
            answer_text=_summarize(resolved_query, result),
        )
        audit_target = (f"conversation:{conversation_id} outcome:{outcome} question:{question!r} "
                         f"model:{decision.model} dimensions:{decision.dimensions} measures:{decision.measures}")

    registry.auth_store.record_audit(
        "chat_ask", user.username, actor_user_id=user.id, target=audit_target,
    )
    return {"question": question_msg, "response": response_msg, "learned": learned}


@router.post("/conversations/{conversation_id}/ask", dependencies=[Depends(_require_enabled)])
def ask(conversation_id: int, body: AskIn, user: User = Depends(require_role("viewer"))):
    conv, question_msg, catalog, prior_context, translator = _start_ask(
        conversation_id, user, body.question)

    try:
        decision = nlq.resolve(
            body.question, catalog, prior_context, registry.models, translator,
            scope=conv["model_scope"],
        )
    except TranslatorError as exc:
        return _handle_translator_error(conversation_id, user, question_msg, body.question, exc)

    return _handle_decision(conversation_id, user, question_msg, body.question, decision)


def _default_pin_name(conv: dict, message_id: int) -> str:
    """The question that produced the pinned answer, as the visual's name —
    the closest thing a chat turn has to a human-written title."""
    question = ""
    for m in conv["messages"]:
        if m["id"] >= message_id:
            break
        if m["role"] == "user" and m["question_text"]:
            question = m["question_text"]
    return question.strip()[:60] or conv["title"] or "chat visual"


@router.post("/conversations/{conversation_id}/messages/{message_id}/pin", status_code=201)
def pin_message(conversation_id: int, message_id: int, body: PinIn,
                user: User = Depends(require_role("author"))):
    """Persist an answered turn as a saved visual — the message's stored
    resolved_query becomes the visual's query verbatim, so Studio and
    dashboards re-execute exactly what grounded the answer (never a client-
    side reconstruction of it). Optionally lands the visual on a dashboard
    in the same call: an existing one by id, or a brand-new one by name.
    Author-gated like every other visual/dashboard mutation, even though
    asking is viewer-tier — and unlike the other conversation routes the
    enabled-check runs *after* the role dependency, so an unauthorized
    caller gets 403 (not 503) even on an unconfigured deployment."""
    _require_enabled()
    if body.dashboard_id is not None and body.new_dashboard_name:
        raise HTTPException(status_code=400, detail="pass dashboard_id or new_dashboard_name, not both")
    conv = _get_owned(conversation_id, user)
    msg = next((m for m in conv["messages"] if m["id"] == message_id), None)
    if not msg:
        raise HTTPException(status_code=404, detail="message not found")
    if msg["outcome"] not in ("answered", "answered_empty") or not msg["resolved_query"]:
        raise HTTPException(status_code=400, detail="only answered messages can be pinned")
    rq = msg["resolved_query"]
    if rq["model"] not in registry.models:
        raise HTTPException(status_code=400, detail=f"model '{rq['model']}' is no longer defined")
    # resolve the target dashboard before creating anything, so a bad id
    # can't leave an orphaned visual behind
    target = None
    if body.dashboard_id is not None:
        target = registry.store.get_dashboard(body.dashboard_id)
        if not target:
            raise HTTPException(status_code=404, detail="dashboard not found")
    name = body.name.strip() or _default_pin_name(conv, message_id)
    spec = {
        "query": {
            "model": rq["model"],
            "dimensions": rq.get("dimensions") or [],
            "measures": rq.get("measures") or [],
            "inline_measures": rq.get("inline_measures") or [],
            "filters": rq.get("filters") or [],
            "sort": rq.get("sort"),
            "limit": rq.get("limit"),
            "parameters": [],
            "parameter_values": {},
        },
        "chartType": "auto",
    }
    _validate_visual_spec(spec)
    visual = registry.store.create(name, rq["model"], spec)
    dashboard = None
    if target:
        dashboard = registry.store.update_dashboard(
            target["id"], target["name"], target["items"] + [{"visual_id": visual["id"]}],
            target["views"], target["active_view"])
    elif body.new_dashboard_name:
        dashboard = registry.store.create_dashboard(
            body.new_dashboard_name.strip() or name, [{"visual_id": visual["id"]}], [], 0)
    registry.auth_store.record_audit(
        "chat_pin", user.username, actor_user_id=user.id,
        target=f"conversation:{conversation_id} message:{message_id} visual:{visual['id']}"
               + (f" dashboard:{dashboard['id']}" if dashboard else ""),
    )
    return {"visual": visual, "dashboard": dashboard}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/conversations/{conversation_id}/ask/stream", dependencies=[Depends(_require_enabled)])
def ask_stream(conversation_id: int, body: AskIn, user: User = Depends(require_role("viewer"))):
    """Same behavior as POST .../ask (identical persisted messages, audit
    log, and re-validation), but as Server-Sent Events: "thinking"/
    "tool_name"/"tool_input" events let a caller show progress live before
    the final "response" event — which carries exactly the same
    {question, response} body ask() returns outright, so a client can
    render it with the same code path either way. Not EventSource-based
    (its GET-only, no-custom-headers API can't carry the CSRF header this
    app requires for cookie-authed mutations) — a caller reads this with
    fetch() + a ReadableStream reader instead."""
    conv, question_msg, catalog, prior_context, translator = _start_ask(
        conversation_id, user, body.question)

    def gen():
        yield _sse("question", {"question": question_msg})
        decision = None
        try:
            for item in nlq.resolve_streaming(
                body.question, catalog, prior_context, registry.models, translator,
                scope=conv["model_scope"],
            ):
                if not isinstance(item, StreamEvent):
                    decision = item
                    continue
                if item.kind == "thinking":
                    yield _sse("thinking", {"text": item.text})
                elif item.kind == "tool_name":
                    yield _sse("tool_name", {"tool_name": item.tool_name})
                elif item.kind == "tool_input":
                    yield _sse("tool_input", {"tool_input": item.tool_input})
        except TranslatorError as exc:
            yield _sse("response", _handle_translator_error(conversation_id, user, question_msg, body.question, exc))
            return
        yield _sse("response", _handle_decision(conversation_id, user, question_msg, body.question, decision))

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
