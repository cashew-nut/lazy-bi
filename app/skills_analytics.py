"""Concrete Skill handlers for the analytics agent
(agents/analytics.yaml) — registered into app/skills.py's registry as an
import side effect (imported once from app/main.py, before either
Registry.init() or mcpserver.build_mcp() need the registry populated).

ask_question wraps the exact same question -> resolve -> execute -> persist
-> audit orchestration the browser's conversational-analytics chat already
uses (app/nlq.py, promoted from app/api/chat.py — research.md R6), so an
MCP call and a browser chat turn against the same conversation produce
identical persisted history. It deliberately does not support a
conversation's per-model llm_model override the browser chat does — always
using the single default translator below is an intentionally narrower MVP
scope (spec.md), not an oversight.
"""
from __future__ import annotations

from dataclasses import asdict

from . import config
from .auth import User
from .llm import AnthropicTranslator, TranslatorError
from .nlq import build_catalog, handle_decision, handle_translator_error, resolve, start_ask
from .skills import Skill, register_skill

# A dedicated instance (not app.api.chat's `_translator`) so tests can
# monkeypatch this module's translator without reaching into chat.py's.
_translator = AnthropicTranslator()

_NOT_CONFIGURED_TEXT = "conversational analytics is not configured"


def _empty_result(conversation_id, outcome: str, answer_text: str) -> dict:
    return {
        "conversation_id": conversation_id,
        "question": None,
        "response": {"outcome": outcome, "answer_text": answer_text},
        "learned": [],
    }


def _with_conversation_id(result: dict, conversation_id: int) -> dict:
    return {"conversation_id": conversation_id, **result}


def _ask_question_blocked(reason: str, args: dict) -> dict:
    """invoke_skill()'s rate-limit path never reaches ask_question's own
    handler, so it never resolves/creates a conversation — the caller's own
    conversation_id (if any) is just echoed back, same as any other
    "nothing happened yet" outcome here."""
    return _empty_result(args.get("conversation_id"), "rate_limited", f"rate limited: {reason}")


def _ask_question(user: User, args: dict) -> dict:
    if not config.LLM_ENABLED:
        # Mirrors app/api/chat.py's route-level Depends(_require_enabled)
        # gate — reimplemented as a plain check here since that gate raises
        # an HTTPException and isn't reusable outside a FastAPI route
        # (research.md R6). No conversation is touched, exactly like the
        # HTTP route 503ing before _start_ask ever runs.
        return _empty_result(args.get("conversation_id"), "error", _NOT_CONFIGURED_TEXT)

    from .registry import registry

    conversation_id = args.get("conversation_id")
    if conversation_id is None:
        conv = registry.conversation_store.create(user.id, model_scope=[], llm_model=None)
    else:
        conv = registry.conversation_store.get(conversation_id, user.id)
        if conv is None:
            return _empty_result(conversation_id, "error", "conversation not found")

    question = args["question"]
    question_msg, catalog, prior_context = start_ask(conv, question)

    try:
        decision = resolve(
            question, catalog, prior_context, registry.models, _translator,
            scope=conv["model_scope"],
        )
    except TranslatorError as exc:
        result = handle_translator_error(conv["id"], user, question_msg, question, exc)
        return _with_conversation_id(result, conv["id"])

    result = handle_decision(conv["id"], user, question_msg, question, decision)
    return _with_conversation_id(result, conv["id"])


ASK_QUESTION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "A business question in plain language.",
        },
        "conversation_id": {
            "type": ["integer", "null"],
            "description": (
                "Reuse a prior ask_question conversation for follow-up "
                "context (must belong to the caller). Omit to start a new one."
            ),
        },
    },
    "required": ["question"],
}

ASK_QUESTION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "conversation_id": {
            "type": ["integer", "null"],
            "description": "null only when the call was blocked before any conversation was touched (not configured, rate limited).",
        },
        "question": {"type": ["object", "null"]},
        "response": {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["answered", "answered_empty", "clarification", "query_shown",
                             "declined", "error", "rate_limited"],
                },
                "answer_text": {"type": "string"},
                "resolved_query": {"type": ["object", "null"]},
                "result": {"type": ["object", "null"]},
            },
        },
        "learned": {"type": "array"},
    },
}

register_skill(Skill(
    name="ask_question",
    description=(
        "Ask a business question in plain language against the platform's "
        "declared semantic models. Returns a grounded, structured answer, "
        "or a clarification/decline when the question can't be answered "
        "directly — never raw data outside the semantic layer."
    ),
    min_role="viewer",
    input_schema=ASK_QUESTION_INPUT_SCHEMA,
    output_schema=ASK_QUESTION_OUTPUT_SCHEMA,
    handler=_ask_question,
    rate_limited=True,
    on_blocked=_ask_question_blocked,
))


def _list_models(user: User, args: dict) -> dict:
    """The same catalog nlq.build_catalog() already produces for the LLM's
    own prompt inside ask_question — uniform across every role since model
    access isn't itself role-scoped today (only write actions are)."""
    from .registry import registry
    catalog = build_catalog(registry.models, [], memories=registry.memory_store.all_by_model())
    return {"models": [asdict(entry) for entry in catalog]}


LIST_MODELS_INPUT_SCHEMA = {"type": "object", "properties": {}}

LIST_MODELS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "models": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "dimensions": {"type": "array"},
                    "measures": {"type": "array"},
                    "learned_notes": {"type": "array"},
                },
            },
        },
    },
}

register_skill(Skill(
    name="list_models",
    description=(
        "List the models, dimensions, and measures queryable via "
        "ask_question — the same catalog ask_question itself is grounded "
        "on, including declared synonyms and sample values."
    ),
    min_role="viewer",
    input_schema=LIST_MODELS_INPUT_SCHEMA,
    output_schema=LIST_MODELS_OUTPUT_SCHEMA,
    handler=_list_models,
    rate_limited=False,
))
