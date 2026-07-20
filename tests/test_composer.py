"""The Composer: sanitize_notebook_html's contract (the single gate between
raw LLM output and anything a caller may save) + the /api/composer surface.
The real AnthropicComposer is swapped for a FakeComposer on
app.api.composer._composer, mirroring test_chat_api's FakeTranslator — no
network calls, deterministic scripted pages."""
from __future__ import annotations

import pytest

from app.api import composer as composer_api
from app.composer import (
    TEMPLATES, ComposeRequest, ComposerCatalog, ComposeStreamEvent, ComposerError,
    HtmlValidationError, RawComposition, build_user_prompt, sanitize_notebook_html,
)

from .test_chat_api import _parse_sse


class FakeComposer:
    """Scripted Composer: yields optional display events then a done event
    (or raises)."""

    def __init__(self, final: RawComposition | None = None, error: str | None = None,
                 partials: list[str] | None = None):
        self.final = final
        self.error = error
        self.partials = partials or []
        self.requests: list[ComposeRequest] = []

    def compose_streaming(self, request):
        self.requests.append(request)
        if self.error:
            raise ComposerError(self.error)
        for p in self.partials:
            yield ComposeStreamEvent(kind="html", html=p)
        yield ComposeStreamEvent(kind="done", final=self.final)


@pytest.fixture(autouse=True)
def llm_enabled(monkeypatch):
    monkeypatch.setattr(composer_api.config, "LLM_ENABLED", True)


def _fake(monkeypatch, **kwargs) -> FakeComposer:
    fake = FakeComposer(**kwargs)
    monkeypatch.setattr(composer_api, "_composer", fake)
    return fake


# ── sanitize_notebook_html: the save-gate contract ──────────────────────────

V = {1, 2, 3}
D = {7}


def test_sanitize_keeps_the_notebook_vocabulary():
    html = ('<h2>Story</h2><p>Intro <b>bold</b></p>'
            '<div class="nb-visual compact" data-visual-id="1"></div>'
            '<aside class="nb-explainer" data-title="How to read" data-tone="warn"><p>x</p></aside>'
            '<div class="nb-split"><div class="nb-side"><p>a</p></div><div class="nb-side"><p>b</p></div></div>'
            '<details class="nb-collapsible" open><summary><span class="tree-caret">▸</span>More</summary>'
            '<div class="nb-collapsible-body"><p>y</p></div></details>')
    page = sanitize_notebook_html(html, V, D)
    assert page.stripped == []
    assert page.visual_ids == [1]
    assert 'data-title="How to read"' in page.html
    assert 'data-tone="warn"' in page.html
    assert "<details" in page.html and "open" in page.html


def test_sanitize_drops_script_subtree_without_leaking_text():
    page = sanitize_notebook_html(
        "<p>keep</p><script>alert('leak me')</script><p>also keep</p>", V, D)
    assert "leak" not in page.html
    assert "keep" in page.html and "also keep" in page.html
    assert "script" in page.stripped


def test_sanitize_void_embeds_do_not_swallow_the_rest_of_the_page():
    # img/link/meta are void: no closing tag ever arrives, so dropping a
    # "subtree" for them would discard everything after — regression guard
    page = sanitize_notebook_html(
        '<p>a</p><img src="http://evil/x.png"><link rel="x"><meta charset="y"><p>b</p>', V, D)
    assert "<p>a</p>" in page.html and "<p>b</p>" in page.html
    assert {"img", "link", "meta"} <= set(page.stripped)
    assert "evil" not in page.html


def test_sanitize_unwraps_unknown_tags_but_keeps_children():
    page = sanitize_notebook_html("<figure><p>caption text</p></figure>", V, D)
    assert "caption text" in page.html
    assert "<figure" not in page.html
    assert "figure" in page.stripped


def test_sanitize_strips_event_handlers_style_and_ids():
    page = sanitize_notebook_html(
        '<p onclick="alert(1)" style="color:red" id="x" class="ok">hi</p>', V, D)
    assert "onclick" not in page.html and "style" not in page.html and 'id="x"' not in page.html
    assert 'class="ok"' in page.html
    assert "p[onclick]" in page.stripped


def test_sanitize_closes_dangling_tags():
    page = sanitize_notebook_html("<div><p>open ended", V, D)
    assert page.html.endswith("</p></div>")


def test_sanitize_rejects_unknown_visual_id():
    with pytest.raises(HtmlValidationError, match="visual id"):
        sanitize_notebook_html('<div class="nb-visual" data-visual-id="999"></div>', V, D)


def test_sanitize_rejects_unknown_dashboard_id():
    with pytest.raises(HtmlValidationError, match="dashboard id"):
        sanitize_notebook_html('<div class="nb-dashboard" data-dashboard-id="999"></div>', V, D)


def test_sanitize_rejects_non_numeric_visual_id():
    with pytest.raises(HtmlValidationError, match="must be a number"):
        sanitize_notebook_html('<div class="nb-visual" data-visual-id="javascript:x"></div>', V, D)


def test_sanitize_rejects_mismatched_tab_names():
    html = ('<div class="nb-tabs"><div class="nb-tab-list">'
            '<button class="nb-tab-btn on" data-tab="a">A</button></div>'
            '<div class="nb-tab-panel" data-tab="b"><p>x</p></div></div>')
    with pytest.raises(HtmlValidationError, match="mismatched"):
        sanitize_notebook_html(html, V, D)


def test_sanitize_accepts_tabs_regardless_of_attribute_order():
    html = ('<div class="nb-tabs"><div class="nb-tab-list">'
            '<button data-tab="a" class="nb-tab-btn on">A</button></div>'
            '<div data-tab="a" class="nb-tab-panel"><p>x</p></div></div>')
    page = sanitize_notebook_html(html, V, D)
    assert 'data-tab="a"' in page.html


def test_sanitize_rejects_empty_page():
    with pytest.raises(HtmlValidationError, match="empty"):
        sanitize_notebook_html("<script>only this</script>", V, D)


def test_sanitize_strips_unknown_explainer_tone():
    page = sanitize_notebook_html(
        '<aside class="nb-explainer" data-tone="sparkly"><p>x</p></aside>', V, D)
    assert "data-tone" not in page.html
    assert "aside[data-tone=sparkly]" in page.stripped


def test_seeded_demo_notebook_passes_the_sanitizer_clean(client):
    """The hand-authored sample page and the LLM gate share one vocabulary —
    if this fails, seed.py and composer.py have drifted apart."""
    demo = next(n for n in client.get("/api/notebooks").json() if n["name"] == "Recruitment Overview")
    html = client.get(f"/api/notebooks/{demo['id']}").json()["html"]
    visuals = {v["id"] for v in client.get("/api/visuals").json()}
    dashboards = {d["id"] for d in client.get("/api/dashboards").json()}
    page = sanitize_notebook_html(html, visuals, dashboards)
    assert page.stripped == []
    assert set(page.visual_ids) <= visuals


# ── prompt assembly ─────────────────────────────────────────────────────────

def test_prompt_carries_catalog_selection_draft_and_history():
    catalog = ComposerCatalog(
        visuals=[{"id": 4, "name": "Rev by cat", "model": "sales", "chart_type": "bar",
                  "dimensions": ["category"], "measures": ["revenue"]}],
        dashboards=[{"id": 7, "name": "Ops", "tiles": 2, "views": [{"index": 0, "name": "default"}]}],
    )
    req = ComposeRequest(
        instruction="make it tabbed", catalog=catalog, template="tabbed",
        narrative="q2 was strong", name="Q2 Story", selected_visual_ids=[4],
        current_html="<p>old page</p>", history=[{"instruction": "first", "summary": "made a page"}],
    )
    prompt = build_user_prompt(req)
    assert "id=4" in prompt and "'Rev by cat'" in prompt
    assert "id=7" in prompt and "views: 0='default'" in prompt
    assert "visuals [4]" in prompt
    assert "q2 was strong" in prompt
    assert "old page" in prompt and "CURRENT PAGE" in prompt
    assert "'first'" in prompt and "made a page" in prompt
    assert prompt.rstrip().endswith("Instruction: make it tabbed")


# ── /api/composer surface ───────────────────────────────────────────────────

def test_context_disabled_without_llm_key(author_client, monkeypatch):
    monkeypatch.setattr(composer_api.config, "LLM_ENABLED", False)
    assert author_client.get("/api/composer/context").status_code == 503


def test_context_requires_author_role(viewer_client):
    assert viewer_client.get("/api/composer/context").status_code == 403


def test_context_lists_templates_and_catalog(author_client):
    ctx = author_client.get("/api/composer/context").json()
    assert {t["id"] for t in ctx["templates"]} == {t["id"] for t in TEMPLATES}
    assert all({"id", "label", "description"} <= set(t) for t in ctx["templates"])
    assert ctx["visuals"], "seeded visuals should appear in the catalog"
    assert {"id", "name", "model", "chart_type"} <= set(ctx["visuals"][0])
    assert ctx["dashboards"] and ctx["dashboards"][0]["views"]


def test_compose_requires_author_role(viewer_client):
    res = viewer_client.post("/api/composer/compose/stream", json={"instruction": "x"})
    assert res.status_code == 403


def test_compose_stream_sanitizes_and_returns_the_page(author_client, monkeypatch):
    vid = author_client.get("/api/visuals").json()[0]["id"]
    raw = (f'<h2>Story</h2><div class="nb-visual" data-visual-id="{vid}"></div>'
           '<script>evil()</script><p onclick="x()">tap</p>')
    fake = _fake(monkeypatch,
                 final=RawComposition(name="My Page", html=raw, summary="built it"),
                 partials=["<h2>St", "<h2>Story</h2>"])

    res = author_client.post("/api/composer/compose/stream", json={
        "instruction": "compose", "template": "executive", "narrative": "n",
        "visual_ids": [vid],
    })
    assert res.status_code == 200
    events = _parse_sse(res.text)
    assert [e for e, _ in events][:2] == ["html", "html"]
    outcome = dict(events)["response"]
    assert outcome["outcome"] == "composed"
    assert outcome["name"] == "My Page"
    assert outcome["summary"] == "built it"
    assert "script" not in outcome["html"] and "onclick" not in outcome["html"]
    assert f'data-visual-id="{vid}"' in outcome["html"]
    assert "script" in outcome["stripped"] and "p[onclick]" in outcome["stripped"]
    # the selections made it into the ComposeRequest
    assert fake.requests[0].selected_visual_ids == [vid]
    assert fake.requests[0].template == "executive"


def test_compose_rejects_page_with_phantom_visual(author_client, monkeypatch):
    _fake(monkeypatch, final=RawComposition(
        name="Bad", html='<div class="nb-visual" data-visual-id="424242"></div>', summary="s"))
    before = len(author_client.get("/api/notebooks").json())

    res = author_client.post("/api/composer/compose/stream", json={"instruction": "compose"})
    outcome = dict(_parse_sse(res.text))["response"]
    assert outcome["outcome"] == "error"
    assert "424242" in outcome["message"]
    assert len(author_client.get("/api/notebooks").json()) == before


def test_compose_reports_llm_failure_as_error_event(author_client, monkeypatch):
    _fake(monkeypatch, error="socket burned out")
    res = author_client.post("/api/composer/compose/stream", json={"instruction": "compose"})
    outcome = dict(_parse_sse(res.text))["response"]
    assert outcome["outcome"] == "error"
    assert "temporarily unavailable" in outcome["message"]


def test_compose_validates_request_shape(author_client, monkeypatch):
    _fake(monkeypatch, final=RawComposition(name="x", html="<p>x</p>", summary="s"))
    assert author_client.post("/api/composer/compose/stream",
                              json={"instruction": "   "}).status_code == 400
    assert author_client.post("/api/composer/compose/stream",
                              json={"instruction": "x", "template": "nope"}).status_code == 400
    assert author_client.post("/api/composer/compose/stream",
                              json={"instruction": "x", "visual_ids": [999999]}).status_code == 400


def test_composed_page_saves_through_ordinary_notebooks_crud(author_client, monkeypatch):
    """End-to-end shape of the real flow: compose → sanitized html → the
    client persists it via POST /api/notebooks — one write path for pages."""
    vid = author_client.get("/api/visuals").json()[0]["id"]
    _fake(monkeypatch, final=RawComposition(
        name="Composed Page",
        html=f'<p>intro</p><div class="nb-visual" data-visual-id="{vid}"></div>',
        summary="s"))
    res = author_client.post("/api/composer/compose/stream", json={"instruction": "go"})
    outcome = dict(_parse_sse(res.text))["response"]

    created = author_client.post("/api/notebooks", json={
        "name": outcome["name"], "html": outcome["html"]}).json()
    fetched = author_client.get(f"/api/notebooks/{created['id']}").json()
    assert fetched["html"] == outcome["html"]
    author_client.delete(f"/api/notebooks/{created['id']}")
