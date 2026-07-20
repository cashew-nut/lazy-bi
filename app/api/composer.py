"""The Composer: chat a notebook page into existence (SSE streaming).

Ephemeral like the modelling panel chat — no conversation rows, the draft
lives client-side and the caller re-sends current_html/history each turn.
Saving is deliberately NOT done here: the client persists an accepted draft
through the existing author-gated notebooks CRUD, so there is exactly one
write path for notebook html and it's the one the sanitizer's contract
documents. Author-gated end to end (composing exists to author pages), and
503 unless CI_LLM_API_KEY is configured, same as chat.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config
from ..auth import User, require_role
from ..composer import (
    TEMPLATE_IDS, TEMPLATES, AnthropicComposer, ComposeRequest, ComposerError,
    HtmlValidationError, build_catalog, sanitize_notebook_html,
)
from ..registry import registry

router = APIRouter(tags=["composer"])

# One stateless client, monkeypatched by tests — mirrors chat.py's _translator.
_composer = AnthropicComposer()

# History turns re-sent by the client each call; trimmed server-side too so a
# long tinkering session can't grow an unbounded prompt.
_HISTORY_TURNS = 8


class ComposeIn(BaseModel):
    instruction: str
    template: str = "freeform"
    narrative: str = ""
    name: str = ""
    visual_ids: list[int] = []
    dashboard_ids: list[int] = []
    current_html: str = ""
    history: list[dict] = []


def _require_enabled() -> None:
    if not config.LLM_ENABLED:
        raise HTTPException(status_code=503, detail="the composer is not configured (no LLM API key)")


@router.get("/composer/context")
def composer_context(user: User = Depends(require_role("author"))):
    """Everything the composer UI needs to render its setup rail: the
    template choices and the embeddable catalog (same catalog the LLM is
    shown, so the picker and the prompt can never disagree). The
    enabled-check runs *after* the role dependency (same reasoning as
    chat.py's pin_message): an unauthorized caller gets 403, not 503, even
    on an unconfigured deployment."""
    _require_enabled()
    catalog = build_catalog(registry.store)
    return {
        "templates": [{k: t[k] for k in ("id", "label", "description")} for t in TEMPLATES],
        "visuals": catalog.visuals,
        "dashboards": catalog.dashboards,
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/composer/compose/stream")
def compose_stream(body: ComposeIn, user: User = Depends(require_role("author"))):
    """One composition turn as Server-Sent Events: "thinking" and "html"
    (accumulating partial page, for the live typing-itself preview) are
    display-only; the terminal "response" event carries either the
    sanitized page ({outcome:"composed", name, html, summary, stripped})
    or {outcome:"error", message}. Only html that passed
    sanitize_notebook_html ever appears in a composed response — the raw
    model output is never surfaced as a result. Role before enabled-check,
    like composer_context above."""
    _require_enabled()
    if not body.instruction.strip():
        raise HTTPException(status_code=400, detail="instruction must not be empty")
    if body.template not in TEMPLATE_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown template '{body.template}' (choose one of {sorted(TEMPLATE_IDS)})")

    catalog = build_catalog(registry.store)
    known_visuals = {v["id"] for v in catalog.visuals}
    known_dashboards = {d["id"] for d in catalog.dashboards}
    bad_v = [v for v in body.visual_ids if v not in known_visuals]
    bad_d = [d for d in body.dashboard_ids if d not in known_dashboards]
    if bad_v or bad_d:
        raise HTTPException(status_code=400, detail=f"unknown selection: visuals {bad_v}, dashboards {bad_d}")

    request = ComposeRequest(
        instruction=body.instruction,
        catalog=catalog,
        template=body.template,
        narrative=body.narrative,
        name=body.name,
        selected_visual_ids=body.visual_ids,
        selected_dashboard_ids=body.dashboard_ids,
        current_html=body.current_html,
        history=[h for h in body.history if isinstance(h, dict)][-_HISTORY_TURNS:],
    )

    def _audit(outcome: str) -> None:
        registry.auth_store.record_audit(
            "composer_compose", user.username, actor_user_id=user.id,
            target=f"outcome:{outcome} template:{body.template} instruction:{body.instruction!r}",
        )

    def gen():
        final = None
        try:
            for event in _composer.compose_streaming(request):
                if event.kind == "thinking":
                    yield _sse("thinking", {"text": event.text})
                elif event.kind == "html":
                    yield _sse("html", {"html": event.html})
                elif event.kind == "done":
                    final = event.final
        except ComposerError as exc:
            _audit("error")
            yield _sse("response", {
                "outcome": "error",
                "message": f"the composer is temporarily unavailable: {exc}",
            })
            return

        if final is None:
            _audit("error")
            yield _sse("response", {
                "outcome": "error",
                "message": "the composer returned no page — try again",
            })
            return

        try:
            page = sanitize_notebook_html(final.html, known_visuals, known_dashboards)
        except HtmlValidationError as exc:
            _audit("rejected")
            yield _sse("response", {
                "outcome": "error",
                "message": f"the composed page failed validation: {exc} — ask again and I'll retry",
            })
            return

        _audit("composed")
        yield _sse("response", {
            "outcome": "composed",
            "name": final.name,
            "html": page.html,
            "summary": final.summary,
            "stripped": page.stripped,
        })

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
