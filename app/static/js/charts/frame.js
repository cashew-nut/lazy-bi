/* Shared plot scaffolding: frame, scales, axes. */
"use strict";

import { svgEl, fmtMeasure } from "../lib.js";
import { ctxDim, fmtDimValue } from "./common.js";

export function plotFrame(box) {
  const W = Math.max(280, box.clientWidth - 16);
  const H = Math.max(160, box.clientHeight - 14);
  const m = { l: 62, r: 18, t: 16, b: 44 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
  box.append(svg);
  return { svg, W, H, m, plotW: W - m.l - m.r, plotH: H - m.t - m.b };
}

export function niceTicks(lo, hi, n = 5) {
  if (lo === hi) { hi = lo + 1; }
  const span = hi - lo;
  const step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => span / s <= n) || mag * 10;
  const start = Math.ceil(lo / step) * step;
  const ticks = [];
  for (let v = start; v <= hi + step * 1e-9; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

export function yExtent(series) {
  let lo = 0, hi = -Infinity;
  for (const s of series) for (const v of s.values) {
    if (v == null) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (hi === -Infinity) hi = 1;
  if (hi < 0) hi = 0;
  return [lo, hi * 1.05 || 1];
}

// series-aware extent adjustment for a log y-axis: log scale can't include
// zero/negative values, so the floor becomes the smallest positive value
// seen (falling back to a sane default when nothing is positive at all)
export function logSafeExtent(series, lo, hi) {
  const positives = [];
  for (const s of series) for (const v of s.values) { if (v != null && v > 0) positives.push(v); }
  if (!positives.length) return [(hi > 0 ? hi : 1) / 1000, hi > 0 ? hi : 1];
  return [Math.min(...positives), hi > 0 ? hi : Math.max(...positives)];
}

function niceLogTicks(lo, hi) {
  const startExp = Math.floor(Math.log10(lo));
  const endExp = Math.ceil(Math.log10(hi));
  const ticks = [];
  for (let e = startExp; e <= endExp; e++) {
    for (const m of [1, 2, 5]) {
      const v = m * Math.pow(10, e);
      if (v >= lo * 0.999 && v <= hi * 1.001) ticks.push(v);
    }
  }
  return ticks.length ? ticks : [lo, hi];
}

function drawYAxisLog(f, lo, hi, format) {
  if (lo <= 0) lo = hi > 0 ? hi / 1000 : 0.001;
  if (hi <= lo) hi = lo * 10;
  const ticks = niceLogTicks(lo, hi);
  const logLo = Math.log10(lo), logHi = Math.log10(hi);
  const toPx = (v) => {
    const vv = v > 0 ? v : lo; // clamp non-positive values onto the axis floor rather than NaN
    return f.m.t + f.plotH - ((Math.log10(vv) - logLo) / (logHi - logLo)) * f.plotH;
  };
  const grid = svgEl("g", { class: "grid" });
  const axis = svgEl("g", { class: "axis" });
  for (const t of ticks) {
    const y = toPx(t);
    grid.append(svgEl("line", { x1: f.m.l, x2: f.m.l + f.plotW, y1: y, y2: y }));
    const label = svgEl("text", { x: f.m.l - 8, y: y + 3, "text-anchor": "end" });
    label.textContent = fmtMeasure(t, format);
    axis.append(label);
  }
  f.svg.append(grid, axis);
  f.svg.append(svgEl("line", { class: "axis-line", x1: f.m.l, x2: f.m.l + f.plotW, y1: f.m.t + f.plotH, y2: f.m.t + f.plotH }));
  return toPx;
}

export function drawYAxis(f, lo, hi, format, opts = {}) {
  if (opts.scale === "log") return drawYAxisLog(f, lo, hi, format);
  const ticks = niceTicks(lo, hi);
  const grid = svgEl("g", { class: "grid" });
  const axis = svgEl("g", { class: "axis" });
  for (const t of ticks) {
    const y = f.m.t + f.plotH - ((t - lo) / (hi - lo)) * f.plotH;
    grid.append(svgEl("line", { x1: f.m.l, x2: f.m.l + f.plotW, y1: y, y2: y }));
    const label = svgEl("text", { x: f.m.l - 8, y: y + 3, "text-anchor": "end" });
    label.textContent = fmtMeasure(t, format);
    axis.append(label);
  }
  f.svg.append(grid, axis);
  f.svg.append(svgEl("line", { class: "axis-line", x1: f.m.l, x2: f.m.l + f.plotW, y1: f.m.t + f.plotH, y2: f.m.t + f.plotH }));
  return (v) => f.m.t + f.plotH - ((v - lo) / (hi - lo)) * f.plotH;
}

// ── axis titles: rendered text with an optional click-to-edit affordance,
// an absolutely-positioned <input> overlaid right over the clicked title
// (the container, e.g. .chart-box, is already position:relative) ──

function attachTitleEditor(container, textEl, value, onCommit) {
  textEl.classList.add("editable");
  textEl.addEventListener("click", (e) => {
    e.stopPropagation();
    if (container.querySelector(".axis-title-input")) return; // already editing
    const boxRect = container.getBoundingClientRect();
    const elRect = textEl.getBoundingClientRect();
    const input = document.createElement("input");
    input.type = "text";
    input.className = "axis-title-input";
    input.value = value;
    input.style.left = Math.max(0, elRect.left - boxRect.left - 4) + "px";
    input.style.top = Math.max(0, elRect.top - boxRect.top - 3) + "px";
    input.style.width = Math.max(70, elRect.width + 50) + "px";
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      input.remove();
      const v = input.value.trim();
      if (commit && v !== value) onCommit(v);
    };
    input.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
      else if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("click", (ev) => ev.stopPropagation());
    container.append(input);
    input.focus();
    input.select();
  });
}

// axis: "x" | "y". onCommit, if given, makes the title clickable-to-edit
// directly in the chart box; omit it for read-only contexts (dashboard tiles).
export function drawAxisTitle(f, container, axis, text, onCommit) {
  if (!text) return null;
  const attrs = axis === "y"
    ? { class: "axis-title", x: f.m.l, y: f.m.t - 4 }
    : { class: "axis-title", x: f.m.l + f.plotW, y: f.m.t + f.plotH + 32, "text-anchor": "end" };
  const t = svgEl("text", attrs);
  t.textContent = axis === "y" ? "↑ " + text : text + " →";
  f.svg.append(t);
  if (onCommit) attachTitleEditor(container, t, text, onCommit);
  return t;
}

export function drawXLabels(ctx, f, xs, xToPx, xCol, grain, rotate) {
  const axis = svgEl("g", { class: "axis" });
  const maxLabels = Math.max(2, Math.floor(f.plotW / 78));
  const step = Math.ceil(xs.length / maxLabels);
  xs.forEach((xv, i) => {
    if (i % step !== 0 && i !== xs.length - 1) return;
    if (i === xs.length - 1 && xs.length > 1 && (i % step) < step / 2 && i - (i % step) !== i) {
      if (i % step !== 0 && (i - (i % step)) > i - step / 2) return;
    }
    let text = fmtDimValue(ctx, xv, xCol && ctxDim(ctx, xCol.name), grain);
    if (text.length > 14) text = text.slice(0, 13) + "…";
    const x = xToPx(i);
    const label = svgEl("text", rotate
      ? { x, y: f.m.t + f.plotH + 12, "text-anchor": "end", transform: `rotate(-28 ${x} ${f.m.t + f.plotH + 12})` }
      : { x, y: f.m.t + f.plotH + 16, "text-anchor": "middle" });
    label.textContent = text;
    axis.append(label);
  });
  f.svg.append(axis);
}
