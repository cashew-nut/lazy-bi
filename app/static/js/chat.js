/* Conversational analytics: chat UI over the semantic layer's declared
   models (specs/012-conversational-analytics/). Ask a question, get a
   grounded answer — the exact result the answer is based on is always
   shown alongside it (FR-004), and conversations persist per-user
   (FR-013). Hidden entirely (nav + view) when the server hasn't
   configured CI_LLM_API_KEY (research.md R7) — probeChatAvailability()
   decides that once, at boot. */
"use strict";

import { $, el, api, fmtMeasure } from "./lib.js";
import { state } from "./state.js";

const chat = {
  enabled: false,
  conversations: [],
  current: null,   // full conversation {id, title, model_scope, messages}
};

export async function probeChatAvailability() {
  try {
    chat.conversations = await api("/api/conversations");
    chat.enabled = true;
    $("#chat-nav-btn").hidden = false;
  } catch {
    chat.enabled = false;   // 503 (not configured) or any other failure: stay hidden
  }
}

export async function loadChat() {
  if (!chat.enabled) {
    $("#chat-thread").innerHTML = "";
    $("#chat-thread").append(el("div", { class: "empty-note" },
      "conversational analytics isn't configured on this server."));
    return;
  }
  renderScopeSelect();
  await refreshConvList();
  if (!chat.current && chat.conversations.length) await openConversation(chat.conversations[0].id);
  else renderThread();
}

function renderScopeSelect() {
  const sel = $("#chat-scope");
  const selected = chat.current ? new Set(chat.current.model_scope || []) : new Set();
  sel.innerHTML = "";
  for (const m of state.models) {
    const opt = el("option", { value: m.name }, m.label || m.name);
    opt.selected = selected.has(m.name);
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
    const item = el("div", { class: "chat-conv" + (chat.current && chat.current.id === c.id ? " on" : "") },
      el("div", { class: "nm" }, c.title || "untitled conversation"),
      el("div", { class: "sub" }, (c.model_scope || []).join(", ") || "auto"));
    item.addEventListener("click", () => openConversation(c.id));
    box.append(item);
  }
  if (!chat.conversations.length) {
    box.append(el("div", { class: "empty-note" }, "no conversations yet"));
  }
}

export async function openConversation(id) {
  chat.current = await api(`/api/conversations/${id}`);
  renderScopeSelect();
  renderConvList();
  renderThread();
}

export async function newConversation() {
  const scope = [...$("#chat-scope").selectedOptions].map((o) => o.value);
  const created = await api("/api/conversations", { method: "POST", body: { model_scope: scope } });
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

function renderMessage(msg) {
  if (msg.role === "user") {
    return el("div", { class: "chat-msg user" }, msg.question_text);
  }
  const bubble = el("div", { class: "chat-msg " + (msg.outcome || msg.role) },
    el("span", { class: "tag" }, (msg.outcome || msg.role).replace("_", " ").toUpperCase()),
    el("div", {}, msg.answer_text || ""));
  if (msg.resolved_query) {
    const q = msg.resolved_query;
    bubble.append(el("div", { class: "meta" },
      `model: ${q.model} · dimensions: ${(q.dimensions || []).map(fieldName).join(", ") || "—"} `
      + `· measures: ${(q.measures || []).join(", ") || "—"}`));
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
  $("#chat-scope").addEventListener("change", async () => {
    if (!chat.current) return;
    const scope = [...$("#chat-scope").selectedOptions].map((o) => o.value);
    chat.current = await api(`/api/conversations/${chat.current.id}`, { method: "PATCH", body: { model_scope: scope } });
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
