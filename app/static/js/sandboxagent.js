/* Sandbox coding agent panel: ask for polars, get cells you can apply into
   the notebook with one click (app/sandbox_agent.py is the server seam).

   Deliberately not a chat about data — it writes code for the notebook that
   is open, and everything it sees comes from that notebook's live (possibly
   unsaved) state: every cell's source, and whatever the last run left on
   each cell (stdout/error tails and the result *schema* — never result
   rows). Nothing is persisted: the thread lives until you close the
   notebook, mirroring panelchat.js's ephemeral posture.

   A proposal is never applied, never run, and never saved on its own. You
   click APPLY (or APPLY + RUN, which is the whole feedback loop: apply,
   run, and the resulting error is context for the next request — cheaper
   and faster than having the agent write tests it would have to run). */
"use strict";

import { isAdmin } from "./auth.js";
import { parseSSE } from "./chat.js";
import { $, el } from "./lib.js";
import { highlightPython } from "./pyhighlight.js";
import { applyAgentCell, cellIndexById, notebookContext, runThrough } from "./sandbox.js";
import { hooks } from "./state.js";

// Mirrors app/config.py's SANDBOX_AGENT_HISTORY_TURNS — trimmed client-side
// too so a long session doesn't grow an unbounded request body.
const HISTORY_TURNS = 6;

const agent = {
  enabled: false,
  models: [],          // selectable model ids from GET /api/health
  model: "",           // server default (config.SANDBOX_AGENT_MODEL)
  open: false,
  messages: [],        // ephemeral thread — {role, ...}
  history: [],         // [{request, reply}] follow-up context, resent each ask
  busy: false,
};

// GET /api/health already says whether the feature is configured — same
// single-probe pattern as chat.js's probeChatAvailability.
export function probeSandboxAgent(health) {
  agent.enabled = !!health.sandbox_agent_enabled;
  agent.models = health.llm_models || [];
  agent.model = health.sandbox_agent_model || "";
}

export const isAgentAvailable = () => agent.enabled && isAdmin();

// ── panel chrome ─────────────────────────────────────────────────────────

export function renderAgentChrome() {
  const toggle = $("#sbx-agent-toggle");
  if (toggle) {
    toggle.hidden = !isAgentAvailable();
    toggle.classList.toggle("on", agent.open);
  }
  const panelEl = $("#sbx-agent-panel");
  if (panelEl) panelEl.hidden = !agent.open || !isAgentAvailable();
  const select = $("#sbx-agent-model");
  if (select && !select.options.length && agent.models.length) {
    for (const m of agent.models) select.append(el("option", { value: m }, m));
    select.value = agent.models.includes(agent.model) ? agent.model : agent.models[0];
  }
}

export function toggleAgentPanel() {
  agent.open = !agent.open;
  renderAgentChrome();
  if (agent.open) $("#sbx-agent-input")?.focus();
}

// Called by sandbox.js whenever a different notebook is opened: a thread is
// only ever about the notebook it was asked against.
export function resetAgentThread() {
  agent.messages = [];
  agent.history = [];
  agent.busy = false;
  renderThread();
  renderAgentChrome();
}

// ── thread rendering ─────────────────────────────────────────────────────

function codeBlock(source) {
  const pre = el("pre", { class: "py-highlight sbx-agent-code" });
  const code = el("code", {});
  code.innerHTML = highlightPython(source);
  pre.append(code);
  return pre;
}

function targetLabel(cell) {
  const idx = cell.target_id ? cellIndexById(cell.target_id) : -1;
  return idx >= 0 ? `replaces cell [${idx + 1}]` : "new cell";
}

function renderProposedCell(cell) {
  const head = el("div", { class: "sbx-agent-cell-head" },
    el("span", { class: "field-label" }, targetLabel(cell)));
  const applyBtn = el("button", { class: "btn plain" }, "APPLY");
  applyBtn.addEventListener("click", () => { applyAgentCell(cell); flash(applyBtn); });
  const runBtn = el("button", { class: "btn plain", title: "apply, then run the notebook through this cell" },
    "APPLY + RUN");
  runBtn.addEventListener("click", async () => {
    const idx = applyAgentCell(cell);
    flash(runBtn);
    await runThrough(idx);
  });
  head.append(applyBtn, runBtn);
  const box = el("div", { class: "sbx-agent-cell" }, head, codeBlock(cell.source));
  if (cell.syntax_error) box.append(el("div", { class: "sbx-agent-warn" }, cell.syntax_error));
  return box;
}

function flash(btn) {
  const original = btn.textContent;
  btn.textContent = "APPLIED";
  setTimeout(() => { btn.textContent = original; }, 900);
}

function renderAgentMessage(msg) {
  if (msg.role === "user") return el("div", { class: "chat-msg user" }, msg.text);
  const bubble = el("div", { class: "chat-msg " + (msg.kind === "error" ? "error" : "assistant") },
    el("span", { class: "tag" }, (msg.kind || "answer").toUpperCase()));
  if (msg.text) bubble.append(el("div", {}, msg.text));
  if (msg.notes) bubble.append(el("div", { class: "sbx-agent-notes" }, msg.notes));
  for (const cell of msg.cells || []) bubble.append(renderProposedCell(cell));
  if ((msg.cells || []).length > 1) {
    const allBtn = el("button", { class: "btn alt sbx-agent-all" }, "APPLY ALL");
    allBtn.addEventListener("click", () => {
      let last = -1;
      for (const cell of msg.cells) last = applyAgentCell(cell);
      flash(allBtn);
      return last;
    });
    bubble.append(allBtn);
  }
  for (const w of msg.warnings || []) bubble.append(el("div", { class: "sbx-agent-warn" }, w));
  return bubble;
}

function renderThread() {
  const thread = $("#sbx-agent-thread");
  if (!thread) return;
  thread.innerHTML = "";
  if (!agent.messages.length) {
    thread.append(el("div", { class: "empty-note" },
      "ask for polars — it sees this notebook's cells, their last run's errors and result schemas, "
      + "and the bucket's paths. Nothing here is saved."));
    return;
  }
  for (const msg of agent.messages) thread.append(renderAgentMessage(msg));
  thread.scrollTop = thread.scrollHeight;
}

// A partial tool input, rendered while it streams in. Sources arrive
// incrementally, so this is a live preview of code being written — the same
// display-only role chat.js's fmtPartialQuery plays for a query.
function renderLive(box, input) {
  box.innerHTML = "";
  if (input.notes) box.append(el("div", { class: "sbx-agent-notes" }, input.notes));
  if (input.text) box.append(el("div", {}, input.text));
  for (const cell of input.cells || []) {
    if (typeof cell?.source === "string") box.append(codeBlock(cell.source));
  }
}

// ── asking ───────────────────────────────────────────────────────────────

function pushHistory(request, msg) {
  const reply = msg.kind === "answer" ? msg.text : (msg.notes || "proposed code");
  agent.history.push({ request, reply: (reply || "").slice(0, 400) });
  agent.history = agent.history.slice(-HISTORY_TURNS);
}

// `displayText` keeps a bulky request (a pasted traceback) out of the
// thread without shrinking what the agent actually receives.
async function ask(request, displayText) {
  if (agent.busy || !isAgentAvailable()) return;
  agent.busy = true;
  const thread = $("#sbx-agent-thread");
  const userMsg = { role: "user", text: displayText || request };
  agent.messages.push(userMsg);
  thread.querySelector(".empty-note")?.remove();
  thread.append(renderAgentMessage(userMsg));
  const live = el("div", { class: "chat-msg live" },
    el("span", { class: "tag" }, "WRITING…"), el("div", { class: "live-body" }));
  thread.append(live);
  thread.scrollTop = thread.scrollHeight;

  try {
    const res = await fetch("/api/sandbox/agent/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify({
        request, ...notebookContext(), history: agent.history,
        llm_model: $("#sbx-agent-model")?.value || null,
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || res.statusText);
    }
    for await (const { event, data } of parseSSE(res)) {
      if (event === "tool_name") {
        live.querySelector(".tag").textContent = data.tool_name === "answer" ? "ANSWERING…" : "WRITING…";
      } else if (event === "tool_input") {
        renderLive(live.querySelector(".live-body"), data.tool_input || {});
      } else if (event === "response") {
        const msg = { role: "assistant", ...data };
        agent.messages.push(msg);
        pushHistory(request, msg);
        live.replaceWith(renderAgentMessage(msg));
      }
      thread.scrollTop = thread.scrollHeight;
    }
  } catch (err) {
    live.remove();
    const errMsg = { role: "assistant", kind: "error", text: err.message };
    agent.messages.push(errMsg);
    thread.append(renderAgentMessage(errMsg));
    thread.scrollTop = thread.scrollHeight;
  } finally {
    agent.busy = false;
  }
}

// Sends the failing cell's error straight back — the loop this agent is
// built around, one click instead of a retyped description.
export function askAboutError(cellId, error) {
  if (!isAgentAvailable()) return;
  if (!agent.open) toggleAgentPanel();
  const label = `cell [${cellIndexById(cellId) + 1}]`;
  ask(`Cell ${cellId} (${label}) failed. Fix it:\n${(error || "").slice(-800)}`,
      `fix the error in ${label}`);
}

// sandbox.js drives the notebook and calls back into the panel (open a
// notebook -> fresh thread; a failed cell -> "FIX WITH AGENT"). Registered
// as hooks rather than imported so the dependency stays one-way: this module
// imports sandbox.js, never the reverse (state.js's stated pattern).
hooks.renderAgentChrome = renderAgentChrome;
hooks.resetAgentThread = resetAgentThread;
hooks.askAboutError = askAboutError;
hooks.isAgentAvailable = isAgentAvailable;

// ── wiring (called once from main.js) ────────────────────────────────────

export function attachSandboxAgent() {
  $("#sbx-agent-toggle").addEventListener("click", toggleAgentPanel);
  $("#sbx-agent-close").addEventListener("click", () => { agent.open = false; renderAgentChrome(); });
  $("#sbx-agent-clear").addEventListener("click", resetAgentThread);
  $("#sbx-agent-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#sbx-agent-input");
    const request = input.value.trim();
    if (!request || agent.busy) return;
    input.value = "";
    ask(request);
  });
  // ⌘/Ctrl+Enter submits from the textarea; plain Enter keeps newlines,
  // since a request often quotes code
  $("#sbx-agent-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      $("#sbx-agent-form").requestSubmit();
    }
  });
}
