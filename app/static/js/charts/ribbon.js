/* Ribbon chart: stacked bands re-ranked at every x — rank 1 rides on top. */
"use strict";

import { svgEl, fmtMeasure } from "../lib.js";
import { ctxDim, ctxGrain, fmtDimValue, tooltipHide, tooltipShow, vizMessage } from "./common.js";
import { drawXLabels, drawYAxis, plotFrame } from "./frame.js";

export function renderRibbon(ctx, pivot) {
  const f = plotFrame(ctx.container);
  const { xs, series, measure, xCol, seriesCol } = pivot;
  if (xs.length < 2) return vizMessage(ctx.container, "ribbon needs at least two points on the x axis");
  const grain = xCol ? ctxGrain(ctx, xCol.name) : null;
  const sums = xs.map((_, i) => series.reduce((s, sr) => s + Math.max(0, sr.values[i] || 0), 0));
  const yPx = drawYAxis(f, 0, Math.max(...sums) * 1.05 || 1, measure.format);
  const xToPx = (i) => f.m.l + (i / (xs.length - 1)) * f.plotW;
  drawXLabels(ctx, f, xs, xToPx, xCol, grain, false);

  // per-x stacked intervals in rank order (largest on top)
  const layout = xs.map((_, i) => {
    const ranked = [...series].sort((a, b) => (b.values[i] || 0) - (a.values[i] || 0));
    let cum = sums[i];
    const pos = new Map();
    ranked.forEach((s, rank) => {
      const v = Math.max(0, s.values[i] || 0);
      pos.set(s.key, { top: yPx(cum), bottom: yPx(cum - v), v, rank: rank + 1 });
      cum -= v;
    });
    return pos;
  });

  for (const s of series) {
    let d = "", back = "";
    for (let i = 0; i < xs.length - 1; i++) {
      const p0 = layout[i].get(s.key), p1 = layout[i + 1].get(s.key);
      const x0 = xToPx(i), x1 = xToPx(i + 1), xm = (x0 + x1) / 2;
      const t0 = p0.top + 0.5, t1 = p1.top + 0.5, b0 = p0.bottom - 0.5, b1 = p1.bottom - 0.5;
      d += (i === 0 ? `M${x0},${t0} ` : "") + `C${xm},${t0} ${xm},${t1} ${x1},${t1} `;
      back = `C${xm},${b1} ${xm},${b0} ${x0},${b0} ` + back;
    }
    const xEnd = xToPx(xs.length - 1);
    const pEnd = layout[xs.length - 1].get(s.key);
    const path = svgEl("path", {
      d: `${d}L${xEnd},${pEnd.bottom - 0.5} ${back}Z`,
      fill: s.color, "fill-opacity": 0.82, class: ctx.onCross && seriesCol ? "cross-mark" : "",
    });
    path.addEventListener("mousemove", (evt) => {
      path.setAttribute("fill-opacity", "1");
      const bounds = f.svg.getBoundingClientRect();
      const px = (evt.clientX - bounds.left) * (f.W / bounds.width);
      const i = Math.max(0, Math.min(xs.length - 1, Math.round(((px - f.m.l) / f.plotW) * (xs.length - 1))));
      const p = layout[i].get(s.key);
      tooltipShow(evt, `${s.label} · ${fmtDimValue(ctx, xs[i], xCol && ctxDim(ctx, xCol.name), grain)}`, [
        { color: s.color, label: measure.label, value: fmtMeasure(p.v, s.format || measure.format, false) },
        { color: s.color, label: "rank", value: "#" + p.rank },
      ]);
    });
    path.addEventListener("mouseleave", () => { path.setAttribute("fill-opacity", "0.82"); tooltipHide(); });
    if (ctx.onCross && seriesCol && (ctxDim(ctx, seriesCol.name) || {}).type !== "time") {
      path.addEventListener("click", () => ctx.onCross(seriesCol.name, s.key));
    }
    f.svg.append(path);
  }
}
