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

export function drawYAxis(f, lo, hi, format) {
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
