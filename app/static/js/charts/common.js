/* Chart constants + ctx helpers + tooltip + legend.
   ctx = { model, dims, chartType, result, container, legendBox, rerender, onCross? }
   Data marks use PALETTE (validated for the dark surface: lightness band,
   chroma floor, CVD separation, contrast). Neon chrome colors live in CSS. */
"use strict";

import { $, el, api, fmtDateLabel } from "../lib.js";
import { valueCache } from "../state.js";

// Validated categorical palette — fixed slot order, assigned in sequence, never cycled.
export const PALETTE = ["#0099ad", "#a68f00", "#d633b8", "#eb6234", "#3d7dd6", "#1fae57", "#8b63f2", "#d64f75"];
export const OTHER_COLOR = "#5b6b84"; // neutral for the folded "Other" series
export const MAX_SERIES = 8;
export const GRAINS = { "1d": "day", "1w": "week", "1mo": "month", "1q": "quarter", "1y": "year" };

export const ctxDim = (ctx, name) => ctx.model.dimensions.find((d) => d.name === name);
export const ctxGrain = (ctx, name) => ((ctx.dims || []).find((d) => d.name === name) || {}).grain;

export function fmtDimValue(ctx, v, dim, grain) {
  if (v === null || v === undefined) return "∅";
  if (dim && dim.type === "time") return fmtDateLabel(v, grain);
  return String(v);
}

export function fetchDimValues(modelName, dimName, onReady) {
  const key = modelName + ":" + dimName;
  if (valueCache[key]) return valueCache[key] === "pending" ? null : valueCache[key];
  valueCache[key] = "pending";
  api(`/api/models/${modelName}/dimensions/${dimName}/values`)
    .then((vals) => { valueCache[key] = vals; if (onReady) onReady(); })
    .catch(() => { valueCache[key] = []; });
  return null;
}

export function vizMessage(container, text, isError = false) {
  container.innerHTML = "";
  container.append(el("div", { class: "msg" + (isError ? " error" : "") }, text));
}

export function renderLegend(ctx, pivot) {
  if (!ctx.legendBox) return;
  ctx.legendBox.innerHTML = "";
  if (pivot.series.length < 2) return; // single series: the title names it
  for (const s of pivot.series) {
    ctx.legendBox.append(el("div", { class: "leg-item" },
      el("span", { class: "sw", style: `background:${s.color}` }), s.label));
  }
}

// ── tooltip (shared singleton) ───────────────────────────────

export function tooltipShow(evt, title, rows) {
  const tip = $("#tooltip");
  tip.innerHTML = "";
  tip.append(el("div", { class: "t-title" }, title));
  for (const r of rows) {
    tip.append(el("div", { class: "t-row" },
      el("span", { class: "sw", style: `background:${r.color}` }),
      el("span", {}, r.label),
      el("span", { class: "v" }, r.value)));
  }
  tip.style.display = "block";
  const rect = tip.getBoundingClientRect();
  let x = evt.clientX + 14, y = evt.clientY + 12;
  if (x + rect.width > window.innerWidth - 8) x = evt.clientX - rect.width - 14;
  if (y + rect.height > window.innerHeight - 8) y = evt.clientY - rect.height - 12;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}

export const tooltipHide = () => { const tip = $("#tooltip"); if (tip) tip.style.display = "none"; };
