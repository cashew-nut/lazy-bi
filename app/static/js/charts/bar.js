/* Bar chart: rounded data-end marks with a 2px surface gap, grouped by series. */
"use strict";

import { svgEl, fmtMeasure } from "../lib.js";
import { ctxDim, ctxGrain, fmtDimValue, tooltipHide, tooltipShow } from "./common.js";
import { drawAxisTitle, drawXLabels, drawYAxis, logSafeExtent, plotFrame, yExtent } from "./frame.js";

function roundedBarPath(x, w, y, h, yZero) {
  const r = Math.min(4, w / 2, Math.abs(h));
  if (h >= 0) { // upward bar, rounded top (the data end)
    return `M${x},${yZero} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${yZero} Z`;
  }
  const yb = y; // downward bar, rounded bottom
  return `M${x},${yZero} L${x},${yb - r} Q${x},${yb} ${x + r},${yb} L${x + w - r},${yb} Q${x + w},${yb} ${x + w},${yb - r} L${x + w},${yZero} Z`;
}

export function renderBar(ctx, pivot) {
  const f = plotFrame(ctx.container);
  const { xs, series, measure, xCol } = pivot;
  const grain = xCol ? ctxGrain(ctx, xCol.name) : null;
  let [lo, hi] = yExtent(series);
  const isLog = ctx.yScale === "log";
  if (isLog) [lo, hi] = logSafeExtent(series, lo, hi);
  const yPx = drawYAxis(f, lo, hi, measure.format, { scale: ctx.yScale });
  const band = f.plotW / xs.length;
  const inner = band * 0.72;
  const slot = inner / series.length;
  const barW = Math.max(1.5, slot - 2); // 2px surface gap between adjacent bars
  const yZero = yPx(isLog ? lo : Math.max(0, lo));

  const xToPx = (i) => f.m.l + band * i + band / 2;
  const longLabels = xs.some((x) => String(fmtDimValue(ctx, x, xCol && ctxDim(ctx, xCol.name), grain)).length > 9);
  const rotateLabels = longLabels && band < 90;
  drawXLabels(ctx, f, xs, xToPx, xCol, grain, rotateLabels);
  const onTitleChange = ctx.onAxisTitleChange;
  if (!rotateLabels) {
    const xTitle = ctx.xAxisTitle || (xCol && (ctxDim(ctx, xCol.name) || xCol).label) || "";
    drawAxisTitle(f, ctx.container, "x", xTitle, onTitleChange && ((v) => onTitleChange("x", v)));
  }
  drawAxisTitle(f, ctx.container, "y", ctx.yAxisTitle || measure.label, onTitleChange && ((v) => onTitleChange("y", v)));

  const marks = svgEl("g");
  xs.forEach((xv, i) => {
    series.forEach((s, si) => {
      const v = s.values[i];
      if (v == null) return;
      const x = f.m.l + band * i + (band - inner) / 2 + slot * si + 1;
      const yTop = yPx(Math.max(0, v)), yBot = yPx(Math.min(0, v));
      const up = v >= 0;
      const path = svgEl("path", {
        d: roundedBarPath(x, barW, up ? yTop : yBot, up ? yBot - yTop : yTop - yBot, yZero),
        fill: s.color,
      });
      // hover hit target wider than the mark
      const hit = svgEl("rect", {
        x: x - 1, y: f.m.t, width: barW + 2, height: f.plotH, fill: "transparent",
      });
      const title = fmtDimValue(ctx, xv, xCol && ctxDim(ctx, xCol.name), grain);
      const move = (evt) => {
        path.setAttribute("fill-opacity", "0.82");
        tooltipShow(evt, title, [{ color: s.color, label: s.label, value: fmtMeasure(v, s.format || measure.format, false) }]);
      };
      // cross-filter: prefer the series dimension, else a categorical x
      let crossField = null, crossValue = null;
      if (ctx.onCross) {
        if (pivot.seriesCol && (ctxDim(ctx, pivot.seriesCol.name) || {}).type !== "time") {
          crossField = pivot.seriesCol.name; crossValue = s.key;
        } else if (xCol && (ctxDim(ctx, xCol.name) || {}).type !== "time") {
          crossField = xCol.name; crossValue = String(xv);
        }
      }
      for (const target of [path, hit]) {
        target.addEventListener("mousemove", move);
        target.addEventListener("mouseleave", () => { path.removeAttribute("fill-opacity"); tooltipHide(); });
        if (crossField) {
          target.classList.add("cross-mark");
          target.addEventListener("click", () => ctx.onCross(crossField, crossValue));
        }
      }
      marks.append(path, hit);
    });
  });
  f.svg.append(marks);
}
