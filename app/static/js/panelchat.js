/* Ephemeral chat panel for the guided model form's right-hand side
   (modelform.js): the same translate -> re-validate -> execute pipeline as
   the standalone CHAT module (chat.js), scoped automatically to whichever
   model is currently being edited, and never persisted — POST
   /api/chat/panel/ask/stream writes no conversation or message row, so
   exploratory questions asked while authoring a model don't clutter chat
   history. Rendering is shared with chat.js (renderMessage et al. — a
   panel message is shaped exactly like a stored one, just with id: null)
   so a pinned-visual button, grounding tables/charts, and live "thinking"
   progress all look and behave identically in both places. */
"use strict";

import { fmtPartialQuery, isChatEnabled, parseSSE, renderLearnedNote, renderMessage, TOOL_LABELS } from "./chat.js";
import { $, el } from "./lib.js";

// Mirrors app/api/chat.py's _PRIOR_CONTEXT_TURNS — trimmed client-side too
// so a long-running panel session doesn't grow an unbounded request body.
const PRIOR_CONTEXT_TURNS = 5;

const panel = {
  open: false,
  modelName: null,      // the semantic model this panel is scoped to
  label: "",
  description: "",       // live (possibly unsaved) description text — sent as extra context
  messages: [],           // ephemeral thread, lost on close/model switch
  history: [],             // resolved turns kept for follow-up context (never sent to storage)
  busy: false,
};

export const isPanelChatAvailable = () => isChatEnabled();

// The modelling form calls this whenever it opens, or switches to, a saved
// model — a fresh model means a fresh (empty) thread, since a panel
// conversation only ever means something for the one model it was asked
// about. Re-affirming the same model is a no-op so a description edit
// (setPanelDescription) never wipes the thread mid-conversation.
export function setPanelModel(name, label) {
  const changed = panel.modelName !== name;
  panel.modelName = name;
  panel.label = label || name || "";
  if (changed) {
    panel.messages = [];
    panel.history = [];
    panel.busy = false;
  }
  renderPanelChrome();
  if (changed) renderPanelThread();
}

// Called on every edit to the Overview section's description field — the
// panel always asks with whatever's currently typed, saved or not.
export function setPanelDescription(text) {
  panel.description = text || "";
}

function renderPanelChrome() {
  const toggle = $("#mf-chat-toggle");
  if (toggle) toggle.hidden = !(panel.modelName && isPanelChatAvailable());
  const label = $("#mf-chat-model-label");
  if (label) label.textContent = panel.label || "";
  const panelEl = $("#mf-chat-panel");
  if (panelEl) panelEl.hidden = !panel.open || !panel.modelName;
  if (toggle) toggle.classList.toggle("on", panel.open);
}

export function togglePanelChat() {
  panel.open = !panel.open;
  renderPanelChrome();
  if (panel.open) $("#mf-chat-input")?.focus();
}

export function closePanelChat() {
  panel.open = false;
  renderPanelChrome();
}

function clearPanelThread() {
  panel.messages = [];
  panel.history = [];
  renderPanelThread();
}

function renderPanelThread() {
  const thread = $("#mf-chat-thread");
  if (!thread) return;
  thread.innerHTML = "";
  if (!panel.messages.length) {
    thread.append(el("div", { class: "empty-note" },
      `ask about ${panel.label || "this model"} — exploratory only, nothing here is saved`));
    return;
  }
  for (const msg of panel.messages) thread.append(renderMessage(msg));
  thread.scrollTop = thread.scrollHeight;
}

// Only a successfully-answered turn (matching app/api/chat.py's own
// _prior_turns rule) is reusable follow-up context — a decline or
// clarification carries no resolved query to hand back.
function pushHistoryTurn(questionText, responseMsg) {
  if (!responseMsg.resolved_query
      || (responseMsg.outcome !== "answered" && responseMsg.outcome !== "answered_empty")) return;
  const rq = responseMsg.resolved_query;
  panel.history.push({
    question_text: questionText, model: rq.model, dimensions: rq.dimensions,
    measures: rq.measures, filters: rq.filters, sort: rq.sort, limit: rq.limit,
    inline_measures: rq.inline_measures,
  });
  panel.history = panel.history.slice(-PRIOR_CONTEXT_TURNS);
}

async function askPanel(question) {
  if (!panel.modelName || panel.busy) return;
  panel.busy = true;
  const thread = $("#mf-chat-thread");
  let live = null;
  try {
    const res = await fetch("/api/chat/panel/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify({
        question, model_scope: [panel.modelName], description: panel.description, history: panel.history,
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || res.statusText);
    }

    let thinkingText = "";
    for await (const { event, data } of parseSSE(res)) {
      if (event === "question") {
        const note = thread.querySelector(".empty-note");
        if (note) note.remove();
        panel.messages.push(data.question);
        thread.append(renderMessage(data.question));
        live = el("div", { class: "chat-msg live" },
          el("span", { class: "tag" }, "THINKING…"),
          el("div", { class: "live-thinking" }),
          el("div", { class: "live-query" }));
        thread.append(live);
      } else if (event === "thinking") {
        thinkingText += data.text;
        live.querySelector(".live-thinking").textContent = thinkingText;
      } else if (event === "tool_name") {
        live.querySelector(".tag").textContent = TOOL_LABELS[data.tool_name] || data.tool_name;
      } else if (event === "tool_input") {
        live.querySelector(".live-query").textContent = fmtPartialQuery(data.tool_input);
      } else if (event === "response") {
        panel.messages.push(data.response);
        pushHistoryTurn(question, data.response);
        const rendered = renderMessage(data.response);
        live.replaceWith(rendered);
        live = null;
        if (data.learned && data.learned.length) rendered.append(renderLearnedNote(data.learned));
      }
      thread.scrollTop = thread.scrollHeight;
    }
  } catch (err) {
    if (live) live.remove();
    const errMsg = { role: "assistant", outcome: "error", answer_text: err.message };
    panel.messages.push(errMsg);
    thread.append(renderMessage(errMsg));
    thread.scrollTop = thread.scrollHeight;
  } finally {
    panel.busy = false;
  }
}

export function attachPanelChat() {
  $("#mf-chat-toggle").addEventListener("click", togglePanelChat);
  $("#mf-chat-close").addEventListener("click", closePanelChat);
  $("#mf-chat-clear").addEventListener("click", clearPanelThread);
  $("#mf-chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#mf-chat-input");
    const question = input.value.trim();
    if (!question || panel.busy) return;
    input.value = "";
    input.disabled = true;
    try {
      await askPanel(question);
    } finally {
      input.disabled = false;
      input.focus();
    }
  });
}
