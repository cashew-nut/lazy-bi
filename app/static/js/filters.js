/* Filter vocabulary shared by the builder, dashboard views, and focus mode. */
"use strict";

import { el } from "./lib.js";

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
