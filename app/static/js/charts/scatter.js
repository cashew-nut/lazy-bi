/* Scatter plot. Color alone fails all-pairs CVD checks, so each series also
   gets a distinct marker shape (secondary encoding); the glyph repeats in the
   legend label. */
"use strict";

import { svgEl, fmtMeasure } from "../lib.js";
import { MAX_SERIES, PALETTE, ctxDim, ctxGrain, fmtDimValue, renderLegend, tooltipHide, tooltipShow, vizMessage } from "./common.js";
import { drawYAxis, niceTicks, plotFrame } from "./frame.js";

export const MARKER_GLYPHS = ["●", "■", "▲", "◆", "✕", "○", "✚", "★"];

export function drawMarker(shapeIdx, x, y, r, color) {
  const i = shapeIdx % MARKER_GLYPHS.length;
  const attrs = { fill: color, stroke: "#0a0e17", "stroke-width": 1.5, class: "cross-mark" };
  switch (i) {
    case 1: return svgEl("rect", { ...attrs, x: x - r, y: y - r, width: 2 * r, height: 2 * r });
    case 2: return svgEl("path", { ...attrs, d: `M${x},${y - r * 1.2} L${x + r * 1.1},${y + r} L${x - r * 1.1},${y + r} Z` });
    case 3: return svgEl("path", { ...attrs, d: `M${x},${y - r * 1.3} L${x + r * 1.3},${y} L${x},${y + r * 1.3} L${x - r * 1.3},${y} Z` });
    case 4: return svgEl("path", { fill: "none", stroke: color, "stroke-width": 2.5, class: "cross-mark", d: `M${x - r},${y - r} L${x + r},${y + r} M${x - r},${y + r} L${x + r},${y - r}` });
    case 5: return svgEl("circle", { fill: "none", stroke: color, "stroke-width": 2.5, class: "cross-mark", cx: x, cy: y, r });
    case 6: return svgEl("path", { fill: "none", stroke: color, "stroke-width": 2.5, class: "cross-mark", d: `M${x},${y - r * 1.2} L${x},${y + r * 1.2} M${x - r * 1.2},${y} L${x + r * 1.2},${y}` });
    case 7: return svgEl("path", { ...attrs, d: `M${x},${y - r * 1.4} L${x + r * 0.45},${y - r * 0.4} L${x + r * 1.4},${y - r * 0.3} L${x + r * 0.7},${y + r * 0.4} L${x + r * 0.9},${y + r * 1.4} L${x},${y + r * 0.8} L${x - r * 0.9},${y + r * 1.4} L${x - r * 0.7},${y + r * 0.4} L${x - r * 1.4},${y - r * 0.3} L${x - r * 0.45},${y - r * 0.4} Z` });
    default: return svgEl("circle", { ...attrs, cx: x, cy: y, r });
  }
}

export function renderScatter(ctx) {
  const res = ctx.result;
  const dimCols = res.columns.filter((c) => c.kind === "dimension");
  const meaCols = res.columns.filter((c) => c.kind === "measure");
  const labelCol = dimCols[0], seriesCol = dimCols[1] || null;
  const mx = meaCols[0], my = meaCols[1];

  // stable series slots: order of first appearance, folded past the palette
  const seriesKeys = [];
  for (const row of res.rows) {
    const k = seriesCol ? String(row[seriesCol.name]) : "__all__";
    if (!seriesKeys.includes(k)) seriesKeys.push(k);
  }
  const slot = (k) => Math.min(seriesKeys.indexOf(k), MAX_SERIES - 1);

  const f = plotFrame(ctx.container);
  const xs = res.rows.map((r) => r[mx.name]).filter((v) => v != null);
  const ys = res.rows.map((r) => r[my.name]).filter((v) => v != null);
  if (!xs.length) return vizMessage(ctx.container, "no data points");
  const pad = (lo, hi) => { const s = (hi - lo) * 0.06 || Math.abs(hi) * 0.06 || 1; return [lo - s, hi + s]; };
  const [xlo, xhi] = pad(Math.min(...xs), Math.max(...xs));
  const [ylo, yhi] = pad(Math.min(...ys), Math.max(...ys));
  const yPx = drawYAxis(f, ylo, yhi, my.format);
  const xPx = (v) => f.m.l + ((v - xlo) / (xhi - xlo)) * f.plotW;

  const xAxis = svgEl("g", { class: "axis" });
  for (const t of niceTicks(xlo, xhi, 6)) {
    if (t < xlo || t > xhi) continue;
    const label = svgEl("text", { x: xPx(t), y: f.m.t + f.plotH + 16, "text-anchor": "middle" });
    label.textContent = fmtMeasure(t, mx.format);
    xAxis.append(label);
  }
  f.svg.append(xAxis);
  const xt = svgEl("text", { class: "axis-title", x: f.m.l + f.plotW, y: f.m.t + f.plotH + 32, "text-anchor": "end" });
  xt.textContent = mx.label + " →";
  const yt = svgEl("text", { class: "axis-title", x: f.m.l, y: f.m.t - 4 });
  yt.textContent = "↑ " + my.label;
  f.svg.append(xt, yt);

  if (seriesCol) {
    renderLegend(ctx, { series: seriesKeys.slice(0, MAX_SERIES).map((k, i) => ({
      label: `${MARKER_GLYPHS[i]} ${k}`, color: PALETTE[i] })) });
  }
  for (const row of res.rows) {
    const xv = row[mx.name], yv = row[my.name];
    if (xv == null || yv == null) continue;
    const k = seriesCol ? String(row[seriesCol.name]) : "__all__";
    const si = seriesCol ? slot(k) : 0;
    const mark = drawMarker(si, xPx(xv), yPx(yv), 4.5, PALETTE[si]);
    const title = fmtDimValue(ctx, row[labelCol.name], ctxDim(ctx, labelCol.name), ctxGrain(ctx, labelCol.name));
    mark.addEventListener("mousemove", (evt) => tooltipShow(evt, title + (seriesCol ? ` · ${k}` : ""), [
      { color: PALETTE[si], label: mx.label, value: fmtMeasure(xv, mx.format, false) },
      { color: PALETTE[si], label: my.label, value: fmtMeasure(yv, my.format, false) },
    ]));
    mark.addEventListener("mouseleave", tooltipHide);
    if (ctx.onCross) {
      const field = seriesCol ? seriesCol.name : labelCol.name;
      const value = seriesCol ? k : String(row[labelCol.name]);
      if ((ctxDim(ctx, field) || {}).type !== "time") {
        mark.addEventListener("click", () => ctx.onCross(field, value));
      }
    }
    f.svg.append(mark);
  }
}
