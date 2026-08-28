"""Ask for a measure: the AI authoring route behind the measure lab's and the
modelling form's ASK AI.

Streams one authoring turn as Server-Sent Events, and — like the composer —
deliberately saves nothing. What comes back is a *verified draft*: it compiled
through app/sqlgrammar.py and (unless the source was unreachable) actually ran
against the live data, but it lands in the author's editor, and it becomes a
measure only when they save it through the ordinary author-gated endpoints in
app/api/models.py, or keep it on the visual. One write path for a measure, the
same one a human typing the expression uses.

The context the model is shown is built here from server state — the live
registry, or the draft spec the modelling form is editing — never from
client-supplied schema: the caller says which model, fact table and scope, and
app/measurewriter.py introspects the rest.

Author-gated end to end (writing measures is authoring), and 503 unless
CI_LLM_API_KEY is configured, exactly like chat and the composer.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config, measurewriter, semantic
from ..auth import User, require_role
from ..measurewriter import LLMMeasureWriter, WriteRequest, WriterError
from ..registry import registry
from .models import ModelSpec

router = APIRouter(tags=["measures"])

# One stateless writer, monkeypatched by tests — mirrors composer.py's
# _composer and chat.py's _translator.
_writer = LLMMeasureWriter()

# Prior asks in the same authoring session, re-sent by the client each turn
# (the drafting session is ephemeral, like the composer's) and trimmed here
# too so a long session can't grow an unbounded prompt.
_HISTORY_TURNS = 6


class WriteMeasureIn(BaseModel):
    """Where to write the measure, and what it should compute.

    The model is named one of two ways, because the two authoring surfaces
    have different things in hand: the measure lab is looking at a *saved*
    model (`model`), while the modelling form may be editing one that has
    never been saved, so it sends the same `spec` it already sends to
    /api/models/generate and the server renders and parses it exactly as that
    route does. `dataset` picks the fact table when a model holds several.
    """
    instruction: str
    model: str = ""
    spec: Optional[ModelSpec] = None
    dataset: str = ""
    scope: str = "model"
    # the visual's current query (dimensions/grain/filters/inline_measures) and
    # its declared parameters — context in the visual scope, and what the
    # verifying dry run runs against
    query: Optional[dict] = None
    parameters: list[dict] = []
    editing: Optional[dict] = None       # the measure being rewritten, if any
    history: list[dict] = []
    thinking: Optional[bool] = None


def _require_enabled() -> None:
    if not config.LLM_ENABLED:
        raise HTTPException(status_code=503, detail="measure writing is not configured (no LLM API key)")


def _resolve_model(body: WriteMeasureIn) -> semantic.Model:
    """The model to write against: the draft the form is editing, or a loaded
    one. A draft is rendered and parsed through the same spec_to_yaml ->
    parse_model_text -> resolve_model path /api/models/generate uses, so what
    the writer sees is what saving that form would produce."""
    if body.spec is not None:
        text = semantic.spec_to_yaml(body.spec.model_dump(by_alias=True))
        try:
            return semantic.resolve_model(semantic.parse_model_text(text),
                                          registry.dimension_bundles)
        except semantic.ModelError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"the model draft doesn't parse yet, so there's nothing to write "
                       f"a measure against: {exc}")
    if not body.model:
        raise HTTPException(status_code=400, detail="name a model (or send the draft spec)")
    model = registry.models.get(body.model)
    if model is None:
        raise HTTPException(status_code=404, detail=f"unknown model '{body.model}'")
    return model


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _attempts_out(attempts: list) -> list[dict]:
    return [{"expr": a.measure.expr, "from": a.measure.from_source, "error": a.error}
            for a in attempts]


def _response_payload(outcome: measurewriter.Outcome) -> dict[str, Any]:
    """The terminal `response` event. A measure only ever appears under
    outcome "written", and it is always one that passed verification."""
    attempts = _attempts_out(outcome.attempts)
    if outcome.status == "declined":
        return {"outcome": "declined", "reason": outcome.reason, "attempts": attempts}
    if outcome.status == "failed":
        return {
            "outcome": "failed",
            "message": outcome.error,
            # the last attempt rides along so the author can see what was tried
            # (and fix it by hand) instead of being told only that it failed
            "rejected": outcome.measure.to_dict() if outcome.measure else None,
            "attempts": attempts,
        }
    verdict = outcome.verdict
    return {
        "outcome": "written",
        "measure": outcome.measure.to_dict(),
        "rationale": outcome.measure.rationale,
        "verified": bool(verdict and verdict.ran),
        "note": (verdict.note if verdict else ""),
        "preview": (verdict.preview if verdict else None),
        "attempts": attempts,
    }


@router.post("/measures/write/stream")
def write_measure_stream(body: WriteMeasureIn, user: User = Depends(require_role("author"))):
    """One authoring turn as SSE.

    "thinking", "draft" (the tool call's arguments as they're written),
    "verifying" and "rejected" (a repair round starting) are display-only; the
    terminal "response" event carries the outcome. Role check before the
    enabled check, same as the composer: an unauthorized caller gets 403 even
    on a deployment with no LLM configured.
    """
    _require_enabled()
    if not body.instruction.strip():
        raise HTTPException(status_code=400, detail="say what the measure should compute")
    if body.scope not in measurewriter.SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown scope '{body.scope}' (choose one of {', '.join(measurewriter.SCOPES)})")

    model = _resolve_model(body)
    try:
        context = measurewriter.build_context(
            model, scope=body.scope, dataset=body.dataset, query=body.query,
            parameters=body.parameters, editing=body.editing)
    except ValueError as exc:       # composite model, no fact table named
        raise HTTPException(status_code=400, detail=str(exc))

    request = WriteRequest(
        instruction=body.instruction,
        context=context,
        history=[h for h in body.history if isinstance(h, dict)][-_HISTORY_TURNS:],
        thinking=body.thinking,
    )

    def _audit(outcome: str, detail: str = "") -> None:
        registry.auth_store.record_audit(
            "measure_write", user.username, actor_user_id=user.id,
            target=(f"outcome:{outcome} model:{context.model} table:{context.dataset} "
                    f"scope:{context.scope} ask:{body.instruction!r}"
                    + (f" {detail}" if detail else "")),
        )

    def gen():
        payload = None
        try:
            for event in measurewriter.run_streaming(_writer, model, request):
                if event.kind == "thinking":
                    yield _sse("thinking", {"text": event.text})
                elif event.kind == "draft":
                    yield _sse("draft", {"measure": event.draft, "attempt": event.attempt})
                elif event.kind == "verifying":
                    yield _sse("verifying", {"measure": event.draft, "attempt": event.attempt})
                elif event.kind == "rejected":
                    # the repair is the interesting part of this seam — say so
                    # out loud rather than letting a second attempt look like a stall
                    yield _sse("rejected", {"error": event.error, "attempt": event.attempt})
                elif event.kind == "outcome":
                    payload = _response_payload(event.outcome)
        except WriterError as exc:
            _audit("error")
            yield _sse("response", {
                "outcome": "error",
                "message": f"the measure writer is temporarily unavailable: {exc}",
            })
            return

        if payload is None:         # the loop always ends in an outcome
            _audit("error")
            yield _sse("response", {"outcome": "error", "message": "no measure came back — try again"})
            return

        name = (payload.get("measure") or {}).get("name", "")
        _audit(payload["outcome"], f"measure:{name}" if name else "")
        yield _sse("response", payload)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
