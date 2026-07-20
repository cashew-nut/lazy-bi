/* COMPOSER: chat a notebook page into existence (Split Studio diptych —
   the script on the left: template / narrative / picks / instructions; the
   proof on the right: the draft page itself, live).

   Each turn POSTs /api/composer/compose/stream (ephemeral, like the
   modelling panel chat — no conversation rows) and the server streams the
   page typing itself into the preview; the terminal "response" event
   carries the sanitized page or an error. Drafts live entirely here until
   the user hits SAVE, which persists through the existing notebooks CRUD —
   the composer never writes a notebook behind the user's back. */
"use strict";

import { parseSSE } from "./chat.js";
import { $, api, el } from "./lib.js";
import { hydrate, stripScripts } from "./notebook.js";
import { navigate, paths } from "./router.js";
import { hooks, showView, state } from "./state.js";

// mirrors app/api/composer.py's _HISTORY_TURNS — trimmed client-side too
const HISTORY_TURNS = 8;
const STREAM_PAINT_MS = 250;   // throttle for the live typing preview

const cp = {
  notebookId: null,   // editing an existing notebook, else null (new draft)
  context: null,      // {templates, visuals, dashboards} from the server
  template: "freeform",
  visualIds: new Set(),
  dashIds: new Set(),
  name: "",
  html: "",           // current accepted draft (sanitized, server-returned)
  savedHtml: null,    // last html persisted to the notebooks store
  savedName: null,
  history: [],        // [{instruction, summary}] for follow-up context
  busy: false,
};

const dirty = () => (cp.html && cp.html !== cp.savedHtml) || (cp.name && cp.name !== cp.savedName);

hooks.confirmLeaveComposer = () => {
  if (state.view !== "composer" || cp.busy || !dirty()) return true;
  return confirm("Leave the composer? The unsaved draft will be discarded.");
};

export async function openComposer(notebookId) {
  showView("composer");
  cp.notebookId = notebookId;
  cp.template = "freeform";
  cp.visualIds = new Set();
  cp.dashIds = new Set();
  cp.name = "";
  cp.html = "";
  cp.savedHtml = null;
  cp.savedName = null;
  cp.history = [];
  cp.busy = false;

  const thread = $("#cp-thread");
  thread.innerHTML = "";
  $("#cp-narrative").value = "";
  $("#cp-name").value = "";
  $("#cp-open").disabled = notebookId == null;
  setStatus("");

  try {
    cp.context = await api("/api/composer/context");
  } catch (err) {
    // viewer deep-linked in, or LLM unconfigured — send them home gracefully
    thread.append(el("div", { class: "chat-msg error" },
      el("span", { class: "tag" }, "COMPOSER UNAVAILABLE"), err.message));
    renderPreview();
    return;
  }

  if (notebookId != null) {
    try {
      const nb = await api(`/api/notebooks/${notebookId}`);
      cp.name = nb.name;
      cp.html = nb.html;
      cp.savedHtml = nb.html;
      cp.savedName = nb.name;
      $("#cp-name").value = nb.name;
    } catch {
      cp.notebookId = null;   // gone — fall through to a fresh draft
    }
  }

  renderTemplates();
  renderPicks();
  renderPreview();
  threadNote(cp.html
    ? "this page is loaded — tell me what to change (\"make the funnel section tabs\", \"add an explainer to the trend chart\", \"tighten the intro\")"
    : "set the scene on the left, then tell me what to compose — I'll design the page, you tinker from there");
  $("#cp-input").focus();
}
hooks.openComposer = openComposer;

// ── left rail: setup ────────────────────────────────────────────────────────

function renderTemplates() {
  const box = $("#cp-templates");
  box.innerHTML = "";
  for (const t of cp.context.templates) {
    const card = el("button", { class: "cp-template" + (cp.template === t.id ? " on" : ""), type: "button" },
      el("span", { class: "nm" }, t.label),
      el("span", { class: "desc" }, t.description));
    card.addEventListener("click", () => { cp.template = t.id; renderTemplates(); });
    box.append(card);
  }
}

function pickChip(label, hint, on, toggle) {
  const chip = el("div", { class: "chip" + (on ? " on" : "") },
    el("span", { class: "tick" }, on ? "◈" : "◇"),
    el("span", { class: "lbl" }, label),
    el("span", { class: "hint" }, hint));
  chip.addEventListener("click", toggle);
  return chip;
}

function renderPicks() {
  const vbox = $("#cp-visual-list");
  vbox.innerHTML = "";
  if (!cp.context.visuals.length) vbox.append(el("div", { class: "empty-note" }, "no saved visuals yet — make some in the studio"));
  for (const v of cp.context.visuals) {
    vbox.append(pickChip(v.name, v.chart_type, cp.visualIds.has(v.id), () => {
      cp.visualIds.has(v.id) ? cp.visualIds.delete(v.id) : cp.visualIds.add(v.id);
      renderPicks();
    }));
  }
  const dbox = $("#cp-dash-list");
  dbox.innerHTML = "";
  if (!cp.context.dashboards.length) dbox.append(el("div", { class: "empty-note" }, "no dashboards yet"));
  for (const d of cp.context.dashboards) {
    dbox.append(pickChip(d.name, `${d.tiles} tiles`, cp.dashIds.has(d.id), () => {
      cp.dashIds.has(d.id) ? cp.dashIds.delete(d.id) : cp.dashIds.add(d.id);
      renderPicks();
    }));
  }
}

// ── right pane: the proof ───────────────────────────────────────────────────

function updateBadge() {
  const badge = $("#cp-draft-badge");
  badge.hidden = !cp.html;
  const unsaved = dirty();
  badge.textContent = unsaved ? "DRAFT · UNSAVED" : "DRAFT · SAVED";
  badge.classList.toggle("saved", !unsaved);
}

async function renderPreview() {
  const box = $("#cp-preview");
  const blank = $("#cp-blank");
  blank.hidden = !!cp.html;
  box.innerHTML = cp.html ? stripScripts(cp.html) : "";
  if (cp.html) await hydrate(box);
  updateBadge();
}

function setStatus(text, isErr = false) {
  const s = $("#cp-status");
  s.textContent = text;
  s.classList.toggle("err", isErr);
}

// ── the conversation ────────────────────────────────────────────────────────

function threadNote(text) {
  const thread = $("#cp-thread");
  thread.append(el("div", { class: "empty-note" }, text));
  thread.scrollTop = thread.scrollHeight;
}

function threadMsg(cls, tag, text) {
  const thread = $("#cp-thread");
  const msg = el("div", { class: "chat-msg " + cls }, el("span", { class: "tag" }, tag), text);
  thread.append(msg);
  thread.scrollTop = thread.scrollHeight;
  return msg;
}

async function composeTurn(instruction) {
  if (cp.busy || !cp.context) return;
  cp.busy = true;
  const thread = $("#cp-thread");
  thread.querySelectorAll(".empty-note").forEach((n) => n.remove());
  threadMsg("user", "YOU", instruction);
  const live = el("div", { class: "chat-msg live" },
    el("span", { class: "tag" }, "COMPOSING…"),
    el("div", { class: "live-thinking" }));
  thread.append(live);
  thread.scrollTop = thread.scrollHeight;

  const preview = $("#cp-preview");
  const firstTurn = !cp.html;
  let lastPaint = 0;
  let thinkingText = "";

  try {
    const res = await fetch("/api/composer/compose/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify({
        instruction,
        template: cp.template,
        narrative: $("#cp-narrative").value,
        name: $("#cp-name").value.trim(),
        visual_ids: [...cp.visualIds],
        dashboard_ids: [...cp.dashIds],
        current_html: cp.html,
        history: cp.history.slice(-HISTORY_TURNS),
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || res.statusText);
    }

    let done = null;
    for await (const { event, data } of parseSSE(res)) {
      if (event === "thinking") {
        thinkingText += data.text;
        live.querySelector(".live-thinking").textContent = thinkingText;
        thread.scrollTop = thread.scrollHeight;
      } else if (event === "html") {
        // the page types itself into the proof pane — unhydrated (charts
        // arrive when the sanitized final lands), throttled, scripts stripped
        const now = Date.now();
        if (now - lastPaint > STREAM_PAINT_MS) {
          lastPaint = now;
          $("#cp-blank").hidden = true;
          preview.innerHTML = stripScripts(data.html);
          preview.scrollTop = preview.scrollHeight;
        }
      } else if (event === "response") {
        done = data;
      }
    }
    if (!done) throw new Error("stream ended without a response");

    if (done.outcome === "composed") {
      cp.html = done.html;
      if (done.name && !$("#cp-name").value.trim()) $("#cp-name").value = done.name;
      cp.name = $("#cp-name").value.trim() || done.name;
      cp.history.push({ instruction, summary: done.summary });
      cp.history = cp.history.slice(-HISTORY_TURNS);
      let note = done.summary || "done.";
      if (done.stripped && done.stripped.length) {
        note += ` (dropped disallowed markup: ${done.stripped.join(", ")})`;
      }
      live.replaceWith(threadMsg("answered", firstTurn ? "COMPOSED" : "REVISED", note));
      await renderPreview();
      setStatus("");
    } else {
      live.replaceWith(threadMsg("error", "ERROR", done.message || "composition failed"));
      await renderPreview();   // restore the last good draft in the proof pane
    }
  } catch (err) {
    live.remove();
    threadMsg("error", "ERROR", err.message);
    await renderPreview();
  } finally {
    cp.busy = false;
    thread.scrollTop = thread.scrollHeight;
  }
}

// ── saving (through the ordinary notebooks CRUD) ────────────────────────────

async function saveDraft(asNew) {
  if (!cp.html) { setStatus("nothing to save yet", true); return; }
  const name = $("#cp-name").value.trim() || cp.name || "untitled page";
  try {
    let nb;
    if (!asNew && cp.notebookId != null) {
      nb = await api(`/api/notebooks/${cp.notebookId}`, { method: "PUT", body: { name, html: cp.html } });
    } else {
      nb = await api("/api/notebooks", { method: "POST", body: { name, html: cp.html } });
      cp.notebookId = nb.id;
    }
    cp.savedHtml = cp.html;
    cp.savedName = name;
    cp.name = name;
    $("#cp-open").disabled = false;
    setStatus(`saved · ${name}`);
    updateBadge();
    hooks.refreshNotebookList && hooks.refreshNotebookList();
  } catch (err) {
    setStatus("save failed: " + err.message, true);
  }
}

export function attachComposer() {
  $("#cp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#cp-input");
    const instruction = input.value.trim();
    if (!instruction || cp.busy) return;
    input.value = "";
    input.disabled = true;
    try {
      await composeTurn(instruction);
    } finally {
      input.disabled = false;
      input.focus();
    }
  });
  $("#cp-save").addEventListener("click", () => saveDraft(false));
  $("#cp-save-as").addEventListener("click", () => saveDraft(true));
  // navigate() runs the leave guard, so an unsaved draft still prompts here
  $("#cp-open").addEventListener("click", () => {
    if (cp.notebookId != null) navigate(paths.notebook(cp.notebookId));
  });
  $("#cp-back").addEventListener("click", () => navigate(paths.home()));
  $("#cp-name").addEventListener("input", () => {
    cp.name = $("#cp-name").value.trim();
    updateBadge();
  });
}
