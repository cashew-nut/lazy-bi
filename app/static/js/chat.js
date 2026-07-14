/* Conversational analytics: chat UI over the semantic layer's declared
   models (specs/012-conversational-analytics/). Ask a question, get a
   grounded answer — the exact result the answer is based on is always
   shown alongside it (FR-004), and conversations persist per-user
   (FR-013). Hidden entirely (nav + view) when the server hasn't
   configured CI_LLM_API_KEY (research.md R7) — probeChatAvailability()
   decides that once, at boot. */
"use strict";

import { $, el, api, fmtMeasure } from "./lib.js";
import { state, modelByName } from "./state.js";

const chat = {
  enabled: false,
  llmModels: [],       // selectable model ids, from GET /api/health (config.LLM_MODEL_CHOICES)
  defaultModel: "",
  conversations: [],
  current: null,   // full conversation {id, title, model_scope, llm_model, messages}
  scopeSelection: new Set(),   // model names ticked in the scope picker — mirrors
                                // chat.current.model_scope when a conversation is
                                // open, or the pending scope for a new one
};

// GET /api/health already tells us whether the feature is configured server-
// side and which models are selectable — no need for a second round trip
// (and no error-shaped 503 to catch) just to decide whether to show the nav.
export function probeChatAvailability(health) {
  chat.enabled = !!health.llm_enabled;
  chat.llmModels = health.llm_models || [];
  chat.defaultModel = health.llm_default_model || "";
  $("#chat-nav-btn").hidden = !chat.enabled;
}

export async function loadChat() {
  if (!chat.enabled) {
    $("#chat-thread").innerHTML = "";
    $("#chat-thread").append(el("div", { class: "empty-note" },
      "conversational analytics isn't configured on this server."));
    return;
  }
  renderScopeChips();
  renderModelSelect();
  await refreshConvList();
  if (!chat.current && chat.conversations.length) await openConversation(chat.conversations[0].id);
  else renderThread();
}

// Clear chip-toggle picker (not a cramped <select multiple>) so it's obvious
// at a glance which models a conversation is pinned to — clicking a chip
// toggles it in chat.scopeSelection and, if a conversation is open, PATCHes
// its model_scope immediately; an empty selection means "auto-infer across
// every model" (research.md R6), which the hint below the chips spells out.
function renderScopeChips() {
  const box = $("#chat-scope-chips");
  box.innerHTML = "";
  for (const m of state.models) {
    const on = chat.scopeSelection.has(m.name);
    const tooltip = m.name + (m.description ? ` — ${m.description}` : "");
    const chip = el("div", { class: "chip" + (on ? " on" : ""), title: tooltip },
      el("span", { class: "tick" }, on ? "◈" : "◇"),
      el("span", { class: "lbl" }, m.label || m.name));
    chip.addEventListener("click", () => toggleScope(m.name));
    box.append(chip);
  }
  $("#chat-scope-hint").textContent = chat.scopeSelection.size
    ? `only ${[...chat.scopeSelection].length} pinned model(s) will be considered — click a chip to unpin.`
    : "no models pinned — the assistant infers which one to use from everything you can access.";
}

async function toggleScope(name) {
  if (chat.scopeSelection.has(name)) chat.scopeSelection.delete(name);
  else chat.scopeSelection.add(name);
  renderScopeChips();
  if (chat.current) {
    chat.current = await api(`/api/conversations/${chat.current.id}`,
      { method: "PATCH", body: { model_scope: [...chat.scopeSelection] } });
    renderConvList();
  }
}

function renderModelSelect() {
  const sel = $("#chat-model");
  const current = (chat.current && chat.current.llm_model) || chat.defaultModel;
  sel.innerHTML = "";
  for (const id of chat.llmModels) {
    const opt = el("option", { value: id }, id === chat.defaultModel ? `${id} (default)` : id);
    opt.selected = id === current;
    sel.append(opt);
  }
}

async function refreshConvList() {
  chat.conversations = await api("/api/conversations");
  renderConvList();
}

function renderConvList() {
  const box = $("#chat-conv-list");
  box.innerHTML = "";
  for (const c of chat.conversations) {
    const scopeLabel = (c.model_scope || []).map((n) => (modelByName(n) || { label: n }).label).join(", ") || "all models";
    const item = el("div", { class: "chat-conv" + (chat.current && chat.current.id === c.id ? " on" : "") },
      el("div", { class: "nm" }, c.title || "untitled conversation"),
      el("div", { class: "sub" }, `${scopeLabel} · ${c.llm_model || chat.defaultModel}`));
    item.addEventListener("click", () => openConversation(c.id));
    box.append(item);
  }
  if (!chat.conversations.length) {
    box.append(el("div", { class: "empty-note" }, "no conversations yet"));
  }
}

export async function openConversation(id) {
  chat.current = await api(`/api/conversations/${id}`);
  chat.scopeSelection = new Set(chat.current.model_scope || []);
  renderScopeChips();
  renderModelSelect();
  renderConvList();
  renderThread();
}

export async function newConversation() {
  const scope = [...chat.scopeSelection];
  const llmModel = $("#chat-model").value || undefined;
  const created = await api("/api/conversations", {
    method: "POST", body: { model_scope: scope, llm_model: llmModel },
  });
  chat.conversations.unshift(created);
  await openConversation(created.id);
}

function renderThread() {
  const thread = $("#chat-thread");
  thread.innerHTML = "";
  if (!chat.current) {
    thread.append(el("div", { class: "empty-note" }, "start a new conversation to ask a question"));
    return;
  }
  if (!chat.current.messages.length) {
    thread.append(el("div", { class: "empty-note" }, "ask a question about your data, in plain language"));
  }
  for (const msg of chat.current.messages) thread.append(renderMessage(msg));
  thread.scrollTop = thread.scrollHeight;
}

function fieldName(entry) {
  return typeof entry === "string" ? entry : entry.name;
}

function fmtFilter(f) {
  if (f.op === "in" || f.op === "not_in") return `${f.field} ${f.op} [${(f.values || []).join(", ")}]`;
  return `${f.field} ${f.op} ${f.value}`;
}

// The full resolved query (model/dimensions/measures/filters/sort/limit),
// not just model/dimensions/measures — every answered turn is independently
// verifiable this way, and it's what a "query_shown" message (the assistant
// answering "show me the query") actually shows.
function renderMessage(msg) {
  if (msg.role === "user") {
    return el("div", { class: "chat-msg user" }, msg.question_text);
  }
  const bubble = el("div", { class: "chat-msg " + (msg.outcome || msg.role) },
    el("span", { class: "tag" }, (msg.outcome || msg.role).replace("_", " ").toUpperCase()),
    el("div", {}, msg.answer_text || ""));
  if (msg.resolved_query) {
    const q = msg.resolved_query;
    const filterText = (q.filters || []).map(fmtFilter).join("; ") || "—";
    const sortText = q.sort && q.sort.by ? `${q.sort.by} ${q.sort.desc === false ? "asc" : "desc"}` : "—";
    bubble.append(el("div", { class: "meta" },
      `model: ${q.model} · dimensions: ${(q.dimensions || []).map(fieldName).join(", ") || "—"} `
      + `· measures: ${(q.measures || []).join(", ") || "—"} · filters: ${filterText} `
      + `· sort: ${sortText} · limit: ${q.limit ?? "—"}`));
  }
  if (msg.result && msg.result.rows && msg.result.rows.length) {
    bubble.append(renderGroundingTable(msg.result));
  }
  return bubble;
}

function renderGroundingTable(result) {
  const cols = result.columns;
  const table = el("table", { class: "grounding" });
  table.append(el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c.label || c.name)))));
  const body = el("tbody");
  for (const row of result.rows.slice(0, 20)) {
    body.append(el("tr", {}, ...cols.map((c) => el("td", {},
      c.kind === "measure" ? fmtMeasure(row[c.name], c.format) : String(row[c.name] ?? "")))));
  }
  table.append(body);
  if (result.row_count > 20) {
    table.append(el("caption", { style: "caption-side: bottom; text-align: left; color: var(--ink-3); font-size: 10px; padding-top: 4px" },
      `showing 20 of ${result.row_count} rows`));
  }
  return table;
}

export function attachChat() {
  $("#chat-new").addEventListener("click", () => newConversation());
  $("#chat-model").addEventListener("change", async (e) => {
    if (!chat.current) return;
    chat.current = await api(`/api/conversations/${chat.current.id}`,
      { method: "PATCH", body: { llm_model: e.target.value } });
    renderConvList();
  });
  $("#chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!chat.enabled) return;
    const input = $("#chat-input");
    const question = input.value.trim();
    if (!question) return;
    if (!chat.current) await newConversation();
    input.value = "";
    input.disabled = true;
    try {
      await api(`/api/conversations/${chat.current.id}/ask`, { method: "POST", body: { question } });
      chat.current = await api(`/api/conversations/${chat.current.id}`);
      renderThread();
      await refreshConvList();
    } catch (err) {
      chat.current.messages.push({ role: "assistant", outcome: "error", answer_text: err.message });
      renderThread();
    } finally {
      input.disabled = false;
      input.focus();
    }
  });
}
