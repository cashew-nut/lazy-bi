/* Filter vocabulary shared by the builder, dashboard views, and focus mode. */
"use strict";

import { el } from "./lib.js";
import { fetchDimValues } from "./charts/common.js";
import { valueCache } from "./state.js";

export const FILTER_OPS = [
  ["eq", "="], ["ne", "≠"], ["gt", ">"], ["gte", "≥"], ["lt", "<"], ["lte", "≤"],
  ["in", "in"], ["not_in", "not in"], ["contains", "contains"],
];

export const filterReady = (f) =>
  f.op === "in" || f.op === "not_in" ? f.values.length : f.value !== "" && f.value != null;

export const toApiFilter = (f) =>
  f.op === "in" || f.op === "not_in"
    ? { field: f.field, op: f.op, values: f.values }
    : { field: f.field, op: f.op, value: f.value };

// categorical dims always use the multi-value ops — "=" / "≠" are folded
// into "in" / "not in" with one value, so there's a single picklist UX
// instead of two (see migrateEqNe / normalizeCategoricalOp below)
export const opsForDim = (dim) =>
  dim && dim.type === "categorical"
    ? FILTER_OPS.filter(([o]) => o === "in" || o === "not_in" || o === "contains")
    : FILTER_OPS;

function migrateEqNe(flt) {
  if (flt.op === "eq" || flt.op === "ne") {
    flt.op = flt.op === "eq" ? "in" : "not_in";
    flt.values = flt.value !== "" && flt.value != null ? [String(flt.value)] : flt.values || [];
    flt.value = "";
  }
}

// call once per render, before building a filter's op selector: migrates a
// legacy scalar eq/ne on a categorical field to in/not_in, carrying the
// existing value across so already-saved filters keep matching
export function normalizeCategoricalOp(flt, dim) {
  if (dim && dim.type === "categorical") migrateEqNe(flt);
}

// call when a filter's field just changed: pick an op that's valid for the
// new dimension and clear the now-meaningless value
export function resetFilterForField(flt, dim) {
  flt.op = dim && dim.type === "categorical" ? "in" : "eq";
  flt.value = "";
  flt.values = [];
}

// ── dynamic ("relative") date values for time filters ────────────
// A keyword like "today" or "start_of_month" instead of a fixed calendar
// date. Resolved server-side (app/engine.py, same vocabulary) against that
// day's date at query time, so a saved "today" keeps meaning today on
// every future run rather than freezing at save time.
export const RELATIVE_DATE_OPTIONS = [
  ["today", "Today"],
  ["yesterday", "Yesterday"],
  ["tomorrow", "Tomorrow"],
  ["start_of_week", "Start of this week"],
  ["end_of_week", "End of this week"],
  ["start_of_month", "Start of this month"],
  ["end_of_month", "End of this month"],
  ["start_of_quarter", "Start of this quarter"],
  ["end_of_quarter", "End of this quarter"],
  ["start_of_year", "Start of this year"],
  ["end_of_year", "End of this year"],
];

const RELATIVE_OFFSET_RE = /^today[+-]\d+(d|w|mo|y)$/;

export const isRelativeDate = (v) =>
  RELATIVE_DATE_OPTIONS.some(([k]) => k === v) || RELATIVE_OFFSET_RE.test(String(v ?? ""));

// value control for a single-value time filter: a fixed calendar date, or
// a dynamic keyword picked from RELATIVE_DATE_OPTIONS
export function timeValueControl(flt, onChange) {
  const wrap = el("span", { class: "time-val" });
  const rebuild = () => {
    wrap.innerHTML = "";
    const relative = isRelativeDate(flt.value);
    const mode = el("select", {
      class: "time-mode",
      onchange: (e) => {
        flt.value = e.target.value === "relative" ? RELATIVE_DATE_OPTIONS[0][0] : "";
        onChange();
        rebuild();
      },
    }, el("option", { value: "fixed" }, "fixed date"), el("option", { value: "relative" }, "dynamic"));
    mode.value = relative ? "relative" : "fixed";
    wrap.append(mode);
    if (relative) {
      const sel = el("select", { onchange: (e) => { flt.value = e.target.value; onChange(); } });
      for (const [v, label] of RELATIVE_DATE_OPTIONS) sel.append(el("option", { value: v }, label));
      sel.value = flt.value;
      wrap.append(sel);
    } else {
      wrap.append(el("input", {
        type: "date", value: flt.value || "", placeholder: "value…",
        onchange: (e) => { flt.value = e.target.value; onChange(); },
      }));
    }
  };
  rebuild();
  return wrap;
}

// ── searchable multi-value picklist (categorical filters, "in"/"not_in") ──
// type to narrow `options`, click/Enter to toggle a value into `flt.values`,
// selections shown as removable chips
export function multiSelectPicker(flt, options, onChange) {
  const wrap = el("div", { class: "ms-pick" });
  const chips = el("div", { class: "ms-pick-chips" });
  const input = el("input", { type: "text", class: "ms-pick-input", placeholder: "type to filter…" });
  const list = el("div", { class: "ms-pick-list" });
  list.hidden = true;

  function toggle(v) {
    const s = String(v);
    flt.values = flt.values.includes(s) ? flt.values.filter((x) => x !== s) : [...flt.values, s];
    onChange();
    renderChips();
    renderList();
  }

  function renderChips() {
    chips.innerHTML = "";
    for (const v of flt.values) {
      const rm = el("b", {}, "✕");
      rm.addEventListener("mousedown", (e) => { e.preventDefault(); toggle(v); });
      chips.append(el("span", { class: "ms-pick-chip" }, v, rm));
    }
  }

  function renderList() {
    const q = input.value.trim().toLowerCase();
    const matches = options.filter((v) => String(v).toLowerCase().includes(q));
    list.innerHTML = "";
    if (!matches.length) {
      list.append(el("div", { class: "ms-pick-empty" }, options.length ? "no matches" : "no values"));
      return;
    }
    for (const v of matches.slice(0, 200)) {
      const s = String(v);
      const on = flt.values.includes(s);
      const row = el("div", { class: "ms-pick-opt" + (on ? " on" : "") },
        el("span", { class: "tick" }, on ? "◆" : "◇"), s);
      row.addEventListener("mousedown", (e) => { e.preventDefault(); toggle(v); });
      list.append(row);
    }
  }

  input.addEventListener("focus", () => { list.hidden = false; renderList(); });
  input.addEventListener("input", () => { list.hidden = false; renderList(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { list.hidden = true; input.blur(); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const q = input.value.trim().toLowerCase();
      const first = options.find((v) => String(v).toLowerCase().includes(q));
      if (first !== undefined) { toggle(first); input.value = ""; renderList(); }
    } else if (e.key === "Backspace" && !input.value && flt.values.length) {
      toggle(flt.values[flt.values.length - 1]);
    }
  });
  wrap.addEventListener("focusout", (e) => {
    if (!wrap.contains(e.relatedTarget)) list.hidden = true;
  });

  renderChips();
  wrap.append(chips, input, list);
  return wrap;
}

// value control for one filter's value(s) — a searchable multi-select for
// categorical/"in"/"not_in", timeValueControl for time dimensions, a plain
// select for other enumerable (e.g. numeric) fields, else free text.
// `srcModel` is the model to pull distinct values from (falsy if none, e.g.
// a spine-only dimension).
export function filterValueControl(flt, dim, srcModel, onChange, onListLoaded) {
  const isTime = dim && dim.type === "time";
  const multi = flt.op === "in" || flt.op === "not_in";
  if (isTime && !multi && flt.op !== "contains") return timeValueControl(flt, onChange);
  const distinct = srcModel && valueCache[srcModel + ":" + flt.field];
  const haveList = Array.isArray(distinct) && distinct.length;
  if (multi) {
    if (!haveList && srcModel) fetchDimValues(srcModel, flt.field, onListLoaded);
    return multiSelectPicker(flt, haveList ? distinct : [], onChange);
  }
  if ((flt.op === "eq" || flt.op === "ne") && dim && !isTime) {
    if (!haveList && srcModel) fetchDimValues(srcModel, flt.field, onListLoaded);
    const sel = el("select", { onchange: (e) => { flt.value = e.target.value; onChange(); } });
    sel.append(el("option", { value: "" }, "— pick —"));
    for (const v of (haveList ? distinct : [])) sel.append(el("option", { value: String(v) }, String(v)));
    sel.value = flt.value || "";
    return sel;
  }
  return el("input", {
    type: "text", value: flt.value || "", placeholder: "value…",
    onchange: (e) => { flt.value = e.target.value; onChange(); },
  });
}
