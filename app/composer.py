"""The Composer's LLM seam: chat a notebook page into existence.

Same architecture as app/llm.py (the conversational-analytics seam), applied
to page composition instead of query translation: the third-party LLM is
forced through a single typed tool call (`compose_page`), and everything the
model returns is treated as *unvalidated* until sanitize_notebook_html() has
re-checked it against this module's hard rules and the live registry —
allowed tags/attributes only, no scripts/handlers/external resources, and
every embedded visual/dashboard id must exist right now. A proposal that
references a phantom visual fails outright, exactly like nlq.resolve()
declining a phantom measure; nothing unchecked can ever reach the notebooks
store.

Swappable by design: tests use a FakeComposer implementing the same Composer
protocol, so the contract is exercised with zero network calls.

Design rules in the system prompt are distilled from the project's hallmark
skill (.claude/skills/hallmark): structural variety between pages, honest
copy (the live charts carry the numbers — the narrative never invents any),
and the app's own layout vocabulary instead of freestyle CSS.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from typing import Iterator, Literal, Protocol

from . import config

logger = logging.getLogger(__name__)


# ── the notebook html vocabulary ────────────────────────────────────────────
# One shared source of truth for what a notebook page may contain. The
# client-side hydrator (static/js/notebook.js) brings the marker classes to
# life; everything else is plain content markup styled by style.css.

ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "b", "i", "em", "strong", "u", "s", "small", "sup", "sub",
    "span", "div", "section", "aside", "blockquote", "ul", "ol", "li", "br", "hr",
    "table", "thead", "tbody", "tr", "th", "td", "details", "summary", "button", "code",
}

# void elements among the allowed set (no closing tag expected)
_VOID_TAGS = {"br", "hr"}

# Attributes allowed anywhere. `open` so a collapsible can start expanded,
# `hidden` so non-initial tab panels don't flash before hydration.
_GLOBAL_ATTRS = {"class", "open", "hidden"}

# Tag-specific attributes on top of the global set — the notebook.js
# hydration contract, nothing more. No style/id/src/href/on* anywhere: pages
# style themselves purely through the nb-* class vocabulary.
_TAG_ATTRS = {
    "button": {"data-tab"},
    "div": {"data-tab", "data-visual-id", "data-dashboard-id", "data-view"},
    "aside": {"data-title", "data-tone"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

_EXPLAINER_TONES = {"info", "method", "warn"}


class HtmlValidationError(Exception):
    """The composed html violates a hard rule that stripping can't fix
    (an embedded visual/dashboard that doesn't exist, malformed tabs)."""


class _Sanitizer(HTMLParser):
    """Rebuilds the html keeping only allowed tags/attributes. Disallowed
    *containers* are dropped with their entire subtree (a <script> body must
    never leak through as text); other disallowed tags are unwrapped so
    harmless markup (a stray <figure>) degrades gracefully. Collects every
    embedded visual/dashboard reference for registry re-validation."""

    # dropped with their whole subtree, not unwrapped
    _DROP_SUBTREE = {"script", "style", "iframe", "object", "svg", "template", "head"}
    # void in html — no subtree to drop, no end tag will ever arrive
    _DROP_VOID = {"link", "meta", "embed", "source", "track", "img", "input"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.visual_ids: list[int] = []
        self.dashboard_ids: list[int] = []
        self.stripped: set[str] = set()      # tag/attr names removed, for the warning note
        self._drop_depth = 0                 # >0 while inside a dropped subtree
        self._open_stack: list[str] = []     # allowed tags currently open, for auto-closing

    def _clean_attrs(self, tag: str, attrs) -> str:
        allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(tag, set())
        keep = []
        for name, value in attrs:
            name = name.lower()
            if name not in allowed:
                self.stripped.add(f"{tag}[{name}]")
                continue
            if name == "data-visual-id":
                if value and value.isdigit():
                    self.visual_ids.append(int(value))
                else:
                    raise HtmlValidationError(f"data-visual-id must be a number, got {value!r}")
            if name == "data-dashboard-id":
                if value and value.isdigit():
                    self.dashboard_ids.append(int(value))
                else:
                    raise HtmlValidationError(f"data-dashboard-id must be a number, got {value!r}")
            if name == "data-tone" and value not in _EXPLAINER_TONES:
                self.stripped.add(f"{tag}[data-tone={value}]")
                continue
            if value is None:
                keep.append(name)
            else:
                keep.append(f'{name}="{escape(value, quote=True)}"')
        return (" " + " ".join(keep)) if keep else ""

    def handle_starttag(self, tag, attrs):
        if tag in self._DROP_VOID:
            self.stripped.add(tag)
            return
        if self._drop_depth:
            if tag in self._DROP_SUBTREE or (tag in ALLOWED_TAGS and tag not in _VOID_TAGS):
                self._drop_depth += 1
            return
        if tag in self._DROP_SUBTREE:
            self.stripped.add(tag)
            self._drop_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            self.stripped.add(tag)   # unwrap: children still processed
            return
        if tag in _VOID_TAGS:
            self.out.append(f"<{tag}>")
            return
        self.out.append(f"<{tag}{self._clean_attrs(tag, attrs)}>")
        self._open_stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if self._drop_depth:
            return
        if tag in _VOID_TAGS:
            self.out.append(f"<{tag}>")
        elif tag in self._DROP_SUBTREE or tag not in ALLOWED_TAGS:
            self.stripped.add(tag)
        else:
            self.out.append(f"<{tag}{self._clean_attrs(tag, attrs)}></{tag}>")

    def handle_endtag(self, tag):
        if tag in self._DROP_VOID:
            return
        if self._drop_depth:
            if tag in self._DROP_SUBTREE or (tag in ALLOWED_TAGS and tag not in _VOID_TAGS):
                self._drop_depth -= 1
            return
        if tag not in ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        if tag in self._open_stack:
            # close any tags the model left dangling inside this one
            while self._open_stack:
                top = self._open_stack.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data):
        if not self._drop_depth:
            self.out.append(escape(data, quote=False))

    def close(self):
        super().close()
        while self._open_stack:
            self.out.append(f"</{self._open_stack.pop()}>")


def _tab_names(tags: list[str], marker: str) -> set[str]:
    names = set()
    for tag in tags:
        if marker in tag:
            m = re.search(r'data-tab="([^"]+)"', tag)
            if m:
                names.add(m.group(1))
    return names


def _check_tabs_structure(html: str) -> None:
    """Every nb-tabs group must have matching button/panel data-tab names —
    a mismatch renders as an empty tab group, which is worse than failing
    the proposal so the model can retry. Groups don't nest (the prompt says
    so), so splitting on the group opener isolates each group's tags."""
    for seg in re.split(r'<div[^>]*class="[^"]*nb-tabs[^"]*"[^>]*>', html)[1:]:
        buttons = _tab_names(re.findall(r"<button[^>]*>", seg), "nb-tab-btn")
        panels = _tab_names(re.findall(r"<div[^>]*>", seg), "nb-tab-panel")
        if buttons != panels:
            raise HtmlValidationError(
                f"nb-tabs group has mismatched tab names: buttons={sorted(buttons)} panels={sorted(panels)}")


@dataclass(frozen=True)
class SanitizedPage:
    html: str
    visual_ids: list[int]
    dashboard_ids: list[int]
    stripped: list[str]       # names of tags/attrs removed (empty = clean pass)


def sanitize_notebook_html(html: str, known_visual_ids: set[int],
                           known_dashboard_ids: set[int]) -> SanitizedPage:
    """The composer's re-validation gate. Structural violations are healed
    by stripping (and reported); grounding violations — ids that don't exist
    in the registry right now — raise, because a page of dead embeds is not
    a page. Also used by tests as the single contract for what may be saved."""
    parser = _Sanitizer()
    parser.feed(html)
    parser.close()
    clean = "".join(parser.out).strip()
    if not clean:
        raise HtmlValidationError("composed page is empty after sanitization")
    unknown_v = [v for v in parser.visual_ids if v not in known_visual_ids]
    if unknown_v:
        raise HtmlValidationError(
            f"page embeds visual id(s) {sorted(set(unknown_v))} that don't exist — "
            "only ids from the provided catalog may be used")
    unknown_d = [d for d in parser.dashboard_ids if d not in known_dashboard_ids]
    if unknown_d:
        raise HtmlValidationError(
            f"page embeds dashboard id(s) {sorted(set(unknown_d))} that don't exist — "
            "only ids from the provided catalog may be used")
    _check_tabs_structure(clean)
    return SanitizedPage(
        html=clean,
        visual_ids=parser.visual_ids,
        dashboard_ids=parser.dashboard_ids,
        stripped=sorted(parser.stripped),
    )


# ── templates ───────────────────────────────────────────────────────────────
# A template is a structural *hint*, not literal markup — the LLM writes the
# page within the vocabulary; the template steers the macrostructure. Ids are
# stable API values; hint text goes verbatim into the prompt.

TEMPLATES = [
    {
        "id": "freeform",
        "label": "Freeform",
        "description": "No prescribed shape — the story decides the structure.",
        "hint": "No prescribed structure. Choose the shape that best serves this "
                "particular story; do not default to the same layout every time.",
    },
    {
        "id": "executive",
        "label": "Executive report",
        "description": "Headline stats up top, then sections that earn the detail.",
        "hint": "Executive report: open with a one-paragraph framing, then a compact "
                "row of headline stat visuals (nb-split or a plain sequence of "
                "compact visuals), then one section per theme. Detail and caveats "
                "live in nb-collapsible blocks so the top stays scannable. Close "
                "with a short takeaways list.",
    },
    {
        "id": "tabbed",
        "label": "Tabbed explorer",
        "description": "Parallel threads of one story, one tab each.",
        "hint": "Tabbed explorer: a short intro, then ONE nb-tabs group carrying the "
                "parallel threads of the story (one tab per thread, each mixing prose "
                "and visuals). Use nb-explainer inside tabs for reading guidance. "
                "Avoid nesting tab groups.",
    },
    {
        "id": "longform",
        "label": "Long-form narrative",
        "description": "Continuous prose; visuals appear as evidence where the text needs them.",
        "hint": "Long-form narrative: continuous prose with h2 section heads, each "
                "visual introduced by the sentence before it (claim, then proof). Use "
                "nb-split rows to pair a paragraph with its chart side by side; keep "
                "methodology in one nb-collapsible near the end.",
    },
    {
        "id": "brief",
        "label": "One-page brief",
        "description": "One headline number, one chart, tight bullets. Nothing else.",
        "hint": "One-page brief: a single h1, one compact headline stat visual, one "
                "main chart, and a tight bullet list of takeaways drawn from the "
                "user's narrative. Ruthlessly short — no tabs, at most one "
                "collapsible for method notes.",
    },
]

TEMPLATE_IDS = {t["id"] for t in TEMPLATES}


# ── catalog: what the model may embed ───────────────────────────────────────

@dataclass(frozen=True)
class ComposerCatalog:
    """Everything the LLM may reference, in the exact shape shown to it.
    Built from the live registry per request — ids here are the only ids a
    proposal may embed (sanitize_notebook_html re-checks against the same
    source afterwards)."""
    visuals: list[dict] = field(default_factory=list)      # {id,name,model,chart_type,dimensions,measures}
    dashboards: list[dict] = field(default_factory=list)   # {id,name,views:[{index,name}],tiles}


def build_catalog(store) -> ComposerCatalog:
    visuals = []
    for v in store.list():
        q = (v.get("spec") or {}).get("query") or {}
        dims = [d.get("name") if isinstance(d, dict) else d for d in q.get("dimensions") or []]
        visuals.append({
            "id": v["id"], "name": v["name"], "model": v["model"],
            "chart_type": (v.get("spec") or {}).get("chartType", "auto"),
            "dimensions": dims, "measures": q.get("measures") or [],
        })
    dashboards = []
    for d in store.list_dashboards():
        dashboards.append({
            "id": d["id"], "name": d["name"], "tiles": len(d.get("items") or []),
            "views": [{"index": i, "name": v["name"]} for i, v in enumerate(d.get("views") or [])],
        })
    return ComposerCatalog(visuals=visuals, dashboards=dashboards)


def _catalog_text(catalog: ComposerCatalog) -> str:
    lines = ["Saved visuals (embed with <div class=\"nb-visual\" data-visual-id=\"ID\"></div>):"]
    if not catalog.visuals:
        lines.append("  (none saved yet)")
    for v in catalog.visuals:
        lines.append(
            f"  id={v['id']} · {v['name']!r} · {v['chart_type']} chart of "
            f"{', '.join(v['measures']) or '(no measures)'}"
            + (f" by {', '.join(str(d) for d in v['dimensions'])}" if v['dimensions'] else " (single value)")
            + f" · model {v['model']}")
    lines.append("Saved dashboards (embed with <div class=\"nb-dashboard\" data-dashboard-id=\"ID\" data-view=\"N\"></div>):")
    if not catalog.dashboards:
        lines.append("  (none saved yet)")
    for d in catalog.dashboards:
        views = ", ".join(f"{v['index']}={v['name']!r}" for v in d["views"])
        lines.append(f"  id={d['id']} · {d['name']!r} · {d['tiles']} tile(s) · views: {views}")
    return "\n".join(lines)


# ── the tool + prompt ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class RawComposition:
    """The LLM's unvalidated proposal — page name, body-fragment html, and a
    short conversational summary of what it did/changed."""
    name: str
    html: str
    summary: str


@dataclass(frozen=True)
class ComposeStreamEvent:
    """One incremental update from Composer.compose_streaming(). Every kind
    but "done" is display-only (live preview of the page typing itself into
    existence); only the final RawComposition ever reaches sanitization and
    the caller — identical trust model to llm.StreamEvent."""
    kind: Literal["thinking", "html", "done"]
    text: str = ""                              # kind="thinking": thinking delta
    html: str | None = None                     # kind="html": accumulated partial html so far
    final: RawComposition | None = None         # kind="done"


class ComposerError(Exception):
    """The LLM call itself failed (network/timeout/API error) — distinct
    from a bad *proposal*, which sanitize_notebook_html handles."""


class Composer(Protocol):
    def compose_streaming(self, request: "ComposeRequest") -> Iterator[ComposeStreamEvent]: ...


@dataclass(frozen=True)
class ComposeRequest:
    """One composition turn. First turn: current_html is empty and the model
    designs from the narrative + template + selections. Refinement turns:
    current_html carries the accepted draft and instruction says what to
    change — the model returns the *whole revised page* each time."""
    instruction: str
    catalog: ComposerCatalog
    template: str = "freeform"
    narrative: str = ""
    name: str = ""
    selected_visual_ids: list[int] = field(default_factory=list)
    selected_dashboard_ids: list[int] = field(default_factory=list)
    current_html: str = ""
    history: list[dict] = field(default_factory=list)   # [{instruction, summary}]


_COMPOSE_TOOL = {
    "name": "compose_page",
    "eager_input_streaming": True,
    "description": (
        "Return the complete notebook page. Always the WHOLE page — on a "
        "refinement turn, return the full revised html, never a fragment or "
        "a diff. html is a body fragment using only the allowed vocabulary."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "short page title, e.g. 'Q2 Recruitment Story'."},
            "html": {"type": "string", "description": "the full page body fragment."},
            "summary": {
                "type": "string",
                "description": "1-3 conversational sentences: what the page does, or what changed this turn.",
            },
        },
        "required": ["name", "html", "summary"],
    },
}

_SYSTEM_PROMPT = """You are the Composer for a BI platform: you write narrative notebook pages \
as HTML body fragments that embed the user's saved, live-rendering visuals. You always answer \
with exactly one compose_page tool call carrying the complete page.

VOCABULARY — the page may only use these elements (anything else is stripped):
- Prose: <h1> (one per page, optional) <h2> <h3> <p> <b> <i> <em> <strong> <ul> <ol> <li> \
<blockquote> <table> <thead> <tbody> <tr> <th> <td> <hr> <br> <code> <span> <small>
- Live chart: <div class="nb-visual" data-visual-id="ID"></div> — ID must come from the catalog. \
Add class "compact" for stat/single-value visuals (short tile): <div class="nb-visual compact" ...>.
- Live dashboard: <div class="nb-dashboard" data-dashboard-id="ID" data-view="N"></div> — \
renders that dashboard at saved view N (omit data-view for its default view).
- Collapsible: <details class="nb-collapsible"><summary><span class="tree-caret">▸</span>Title</summary>\
<div class="nb-collapsible-body">…</div></details> — depth on demand: methodology, caveats, appendix. \
Add the open attribute to start expanded.
- Tabs: <div class="nb-tabs"><div class="nb-tab-list"><button class="nb-tab-btn on" data-tab="a">A</button>\
<button class="nb-tab-btn" data-tab="b">B</button></div><div class="nb-tab-panel" data-tab="a">…</div>\
<div class="nb-tab-panel" data-tab="b" hidden>…</div></div> — parallel threads of one story. Exactly one \
button has class "on"; every non-initial panel carries hidden; button and panel data-tab names must match.
- Explainer window: <aside class="nb-explainer" data-title="How to read this" data-tone="info">\
<p>…</p></aside> — a callout that teaches the reader how to read a chart, defines a term, or flags a \
caveat. data-tone is info (default), method, or warn. Place it next to the visual it explains.
- Split row: <div class="nb-split"><div class="nb-side">…prose…</div><div class="nb-side">…visual…</div></div> \
— claim on one side, proof on the other. Alternate the prose side between consecutive splits.

HARD RULES:
- No <script>, <style>, <img>, <a>, <iframe>, style= attributes, id= attributes, or event handlers. \
Styling comes entirely from the classes above — the app themes the page.
- Embed only visual/dashboard ids from the catalog below. NEVER invent an id. If nothing in the \
catalog fits a section, write the prose without a chart and say so in your summary.
- HONEST COPY: the live charts carry the numbers. Never state a specific figure, percentage, or \
trend direction unless the user's own narrative stated it. Frame neutrally instead: "how X splits \
across Y", "the trend since launch" — the reader sees the live data, which may have changed since \
you wrote the page.
- Use the user's narrative as the voice of the page — tighten it, structure it, but do not \
invent claims, quotes, or context they didn't give you.

DESIGN RULES:
- Structure serves the story. Tabs for parallel threads; collapsibles for depth-on-demand; \
nb-split for claim-and-proof pairs; explainers beside the chart they decode. Don't use every \
device on every page — a page that is all chrome reads worse than prose.
- Vary macrostructure between pages: if the conversation shows the last page was tab-led, don't \
default to tabs again unless asked.
- Headings are sentence case, specific, and scannable ("Screening funnel, month by month" — not \
"Data Analysis Section"). At most one h1.
- Every visual earns an introduction: the sentence before it says what to look for. Never stack \
three charts with no prose between them.
- Respect explicit layout requests exactly (tabs vs collapsibles, order, what sits side by side).
- On refinement turns, change what was asked and preserve everything else — the user is tinkering, \
not restarting. Return the complete revised page.
"""


def _template_hint(template_id: str) -> str:
    for t in TEMPLATES:
        if t["id"] == template_id:
            return t["hint"]
    return TEMPLATES[0]["hint"]


def _selection_text(req: ComposeRequest) -> str:
    if not req.selected_visual_ids and not req.selected_dashboard_ids:
        return ("The user picked no specific visuals — choose from the catalog "
                "whatever genuinely supports the narrative (or none).")
    parts = []
    if req.selected_visual_ids:
        parts.append(f"visuals {req.selected_visual_ids}")
    if req.selected_dashboard_ids:
        parts.append(f"dashboards {req.selected_dashboard_ids}")
    return ("The user picked " + " and ".join(parts) + " for this page. Build around these; "
            "add others from the catalog only when they clearly serve the story.")


def build_user_prompt(req: ComposeRequest) -> str:
    lines = [
        f"Catalog of live embeds:\n{_catalog_text(req.catalog)}",
        f"\nTemplate: {req.template} — {_template_hint(req.template)}",
        f"\n{_selection_text(req)}",
    ]
    if req.name:
        lines.append(f"\nWorking page title: {req.name!r} (keep unless asked to rename).")
    if req.narrative.strip():
        lines.append(f"\nThe user's narrative (the raw material for the page's prose):\n{req.narrative.strip()}")
    if req.history:
        lines.append("\nEarlier turns this session:")
        for h in req.history:
            lines.append(f"- asked: {h.get('instruction', '')!r} -> {h.get('summary', '')}")
    if req.current_html.strip():
        lines.append(
            "\nCURRENT PAGE (revise this — apply the instruction below, preserve everything "
            f"not asked about, and return the complete revised page):\n{req.current_html.strip()}")
    lines.append(f"\nInstruction: {req.instruction.strip()}")
    return "\n".join(lines)


class AnthropicComposer:
    """Talks to the Anthropic Messages API with forced tool-use so the result
    is always one typed compose_page proposal."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or config.LLM_API_KEY
        self.model = model or config.LLM_MODEL

    def _request_kwargs(self, request: ComposeRequest) -> dict:
        return dict(
            model=self.model,
            max_tokens=8192,
            system=_SYSTEM_PROMPT,
            tools=[_COMPOSE_TOOL],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": build_user_prompt(request)}],
        )

    def compose_streaming(self, request: ComposeRequest) -> Iterator[ComposeStreamEvent]:
        import anthropic

        from .llm import _anthropic_client, _thinking_kwargs  # shared client/adaptive-thinking gate

        client = _anthropic_client(self.api_key)
        try:
            with client.messages.stream(
                **self._request_kwargs(request),
                **_thinking_kwargs(self.model),
            ) as stream:
                for event in stream:
                    if event.type == "thinking":
                        yield ComposeStreamEvent(kind="thinking", text=event.thinking)
                    elif event.type == "input_json":
                        snapshot = event.snapshot if isinstance(event.snapshot, dict) else {}
                        if isinstance(snapshot.get("html"), str):
                            yield ComposeStreamEvent(kind="html", html=snapshot["html"])
                message = stream.get_final_message()
        except anthropic.APIError as exc:
            logger.warning("Anthropic composer call failed: %r (cause: %r)", exc, exc.__cause__)
            raise ComposerError(str(exc)) from exc

        for block in message.content:
            if block.type == "tool_use" and block.name == "compose_page":
                args = block.input or {}
                yield ComposeStreamEvent(kind="done", final=RawComposition(
                    name=str(args.get("name") or "untitled page"),
                    html=str(args.get("html") or ""),
                    summary=str(args.get("summary") or ""),
                ))
                return
        raise ComposerError("model did not call compose_page")
