/* Ask AI for a measure — the client half of POST /api/measures/write/stream,
   shared by the two places measures are authored: the measure lab (on a
   visual) and the modelling form (into the model).

   What comes back is a *verified draft*: the server compiled it and ran it
   against the live data before answering, so what lands in the editor is
   already known to work. It still lands in the editor rather than being
   saved — the author reads it, changes it if they want, and saves it through
   the same buttons they'd use for one they typed. Nothing here writes.

   The ask bar is deliberately one input: the model is given the whole
   catalog (columns, dimensions, existing formulas, the visual's own query)
   server-side, so the person only has to say what they want measured. */
"use strict";

import { canAuthor } from "./auth.js";
import { isChatEnabled, parseSSE, renderThinkingToggle, supportsThinking, thinkingDefault } from "./chat.js";
import { el, fmtMeasure } from "./lib.js";

/* Both surfaces hide the bar unless this deployment has an LLM configured and
   the person can author — the same two conditions the Composer's entry point
   checks (health.llm_enabled + canAuthor), so an unconfigured deployment
   never shows a button that can only 503. */
export const measureAiAvailable = () => isChatEnabled() && canAuthor();

/* One authoring turn. `handlers` are display-only — the returned payload is
   the only thing a caller should act on, exactly like the server treats the
   model's own output. */
export async function askForMeasure(body, handlers = {}) {
  const { onThinking, onStatus } = handlers;
  const res = await fetch("/api/measures/write/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || res.statusText);
  }
  let final = null;
  let thinking = "";
  for await (const { event, data } of parseSSE(res)) {
    if (event === "thinking") {
      thinking += data.text;
      onThinking?.(thinking);
    } else if (event === "draft") {
      onStatus?.(data.measure?.name ? `writing ${data.measure.name}…` : "writing…");
    } else if (event === "verifying") {
      onStatus?.("running it against your data…");
    } else if (event === "rejected") {
      // the repair round is the interesting part: say what broke rather than
      // letting a second attempt look like a stall
      onStatus?.(`✗ ${data.error} — trying again…`);
    } else if (event === "response") {
      final = data;
    }
  }
  if (!final) throw new Error("the measure writer sent no answer");
  return final;
}

/* The one-line summary shown after a turn: why this shape, and whether it was
   proved against real data or only compiled (an unreachable bucket is not the
   same as a verified measure, and must never read like one). */
export function outcomeNote(payload) {
  const bits = [];
  if (payload.verified) {
    // the check query groups by something whenever the measure needs it to, so
    // its first row is only a meaningful preview when there was exactly one:
    // quoting one group's number as "the" value would be a small lie
    const p = payload.preview || {};
    const single = p.rows === 1 && p.value !== null && p.value !== undefined;
    bits.push(single
      ? `✓ ran against your data (${fmtMeasure(p.value, payload.measure?.format, false)})`
      : "✓ ran against your data");
  } else if (payload.note) {
    bits.push(`⚠ compiled, but couldn't be run: ${payload.note}`);
  }
  if (payload.attempts?.length) {
    bits.push(`fixed itself after ${payload.attempts.length} rejected `
      + `attempt${payload.attempts.length > 1 ? "s" : ""}`);
  }
  if (payload.rationale) bits.push(payload.rationale);
  return bits.join(" · ");
}

/* The ask bar itself: an input, a THINKING toggle, a button and a status
   line, wired so a turn can't be started twice. `onAsk(text, ui)` does the
   actual call — the bar knows nothing about which surface it is on.

   The toggle is here rather than on each surface because both of them want
   it and it means the same thing on either: a measure that has to derive
   rows before it can aggregate them is a genuine reasoning problem and worth
   the wait, while "total units" is not. It stays null until someone touches
   it, so an untouched bar asks for the server's default rather than for a
   state the bar invented. */
export function askBar({ placeholder, onAsk, hint = "" }) {
  const input = el("input", { class: "ai-ask-input", placeholder, spellcheck: "false" });
  const btn = el("button", { class: "btn alt ai-ask-btn" }, "✨ ASK AI");
  const status = el("div", { class: "ai-ask-status" });
  const thinkInput = el("input", { type: "checkbox" });
  const thinkBox = el("label", { class: "flag-toggle" }, thinkInput, el("span", {}, "THINKING"));
  const bar = el("div", { class: "ai-ask" },
    el("div", { class: "ai-ask-row" }, input, thinkBox, btn),
    status);

  let thinking = null;      // null = whatever the server defaults to
  // The capability list arrives with /api/health, which can land after a bar
  // built at boot (the measure lab wires itself once) — so this is callable
  // again rather than only being read here.
  function syncThinking() {
    thinkBox.hidden = !supportsThinking();
    renderThinkingToggle(thinkBox, thinkInput, null,
      thinking == null ? thinkingDefault() : thinking);
  }
  syncThinking();
  thinkInput.addEventListener("change", () => { thinking = thinkInput.checked; });

  let busy = false;
  const ui = {
    // textContent, never innerHTML: this line relays engine and provider error
    // text, which quotes the proposed SQL back — none of it is ours to trust
    // as markup
    setStatus(text, kind = "") {
      status.className = "ai-ask-status" + (kind ? " " + kind : "");
      status.textContent = text;
    },
    clear() { input.value = ""; },
    // what the toggle is asking for right now — null means "server default",
    // which is what /api/measures/write/stream reads an absent field as
    thinking: () => thinking,
    syncThinking,
  };
  if (hint) ui.setStatus(hint);

  async function run() {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    btn.disabled = true;
    input.disabled = true;
    // the turn is already on the wire: flipping the toggle now would change
    // nothing about it, so it goes quiet until the answer lands
    thinkInput.disabled = true;
    ui.setStatus("thinking…");
    try {
      await onAsk(text, ui);
    } catch (err) {
      ui.setStatus(`✗ ${err.message}`, "err");
    } finally {
      busy = false;
      btn.disabled = false;
      input.disabled = false;
      syncThinking();          // back to whatever the model's capability allows
    }
  }

  btn.addEventListener("click", run);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); run(); }
  });
  return { bar, input, ...ui };
}

/* Turn a response into the note a surface shows, or throw the reason it
   didn't produce a measure — every caller wants exactly this split. */
export function measureOrThrow(payload) {
  if (payload.outcome === "written") return payload.measure;
  if (payload.outcome === "declined") throw new Error(payload.reason);
  if (payload.outcome === "failed") {
    throw new Error(`${payload.message} (gave up after ${payload.attempts.length} attempts)`);
  }
  throw new Error(payload.message || "no measure came back");
}
