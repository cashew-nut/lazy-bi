/* Line chart: 2px data lines with soft under-glow, crosshair + shared tooltip. */
"use strict";

import { svgEl, fmtMeasure } from "../lib.js";
import { ctxDim, ctxGrain, fmtDimValue, tooltipHide, tooltipShow } from "./common.js";
import { drawXLabels, drawYAxis, plotFrame, yExtent } from "./frame.js";

export function renderLine(ctx, pivot) {
  const f = plotFrame(ctx.container);
  const { xs, series, measure, xCol } = pivot;
  const grain = xCol ? ctxGrain(ctx, xCol.name) : null;
  const [lo, hi] = yExtent(series);
  const yPx = drawYAxis(f, lo, hi, measure.format);
  const xToPx = xs.length === 1
    ? () => f.m.l + f.plotW / 2
    : (i) => f.m.l + (i / (xs.length - 1)) * f.plotW;
  drawXLabels(ctx, f, xs, xToPx, xCol, grain, false);

  const markers = [];
  for (const s of series) {
    let d = "", pen = false;
    s.values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += (pen ? "L" : "M") + xToPx(i).toFixed(1) + "," + yPx(v).toFixed(1);
      pen = true;
    });
    if (d) {
      // soft under-glow (decorative, same hue), then the 2px data line
      f.svg.append(svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": 6, "stroke-opacity": 0.14, "stroke-linejoin": "round" }));
      f.svg.append(svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    if (xs.length === 1 && s.values[0] != null) {
      f.svg.append(svgEl("circle", { cx: xToPx(0), cy: yPx(s.values[0]), r: 4, fill: s.color }));
    }
  }

  // crosshair + shared tooltip
  const cross = svgEl("line", { class: "axis-line", y1: f.m.t, y2: f.m.t + f.plotH, visibility: "hidden", stroke: "#3a4a68" });
  f.svg.append(cross);
  for (const s of series) {
    const dot = svgEl("circle", { r: 4, fill: s.color, stroke: "#0a0e17", "stroke-width": 2, visibility: "hidden" });
    markers.push(dot);
    f.svg.append(dot);
  }
  const overlay = svgEl("rect", { x: f.m.l, y: f.m.t, width: f.plotW, height: f.plotH, fill: "transparent" });
  overlay.addEventListener("mousemove", (evt) => {
    const bounds = f.svg.getBoundingClientRect();
    const px = (evt.clientX - bounds.left) * (f.W / bounds.width);
    const i = Math.max(0, Math.min(xs.length - 1,
      xs.length === 1 ? 0 : Math.round(((px - f.m.l) / f.plotW) * (xs.length - 1))));
    const cx = xToPx(i);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
    cross.setAttribute("visibility", "visible");
    const rows = [];
    series.forEach((s, si) => {
      const v = s.values[i];
      if (v == null) { markers[si].setAttribute("visibility", "hidden"); return; }
      markers[si].setAttribute("cx", cx);
      markers[si].setAttribute("cy", yPx(v));
      markers[si].setAttribute("visibility", "visible");
      rows.push({ color: s.color, label: s.label, value: fmtMeasure(v, s.format || measure.format, false), raw: v });
    });
    rows.sort((a, b) => b.raw - a.raw);
    tooltipShow(evt, fmtDimValue(ctx, xs[i], xCol && ctxDim(ctx, xCol.name), grain), rows);
  });
  overlay.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden");
    markers.forEach((mk) => mk.setAttribute("visibility", "hidden"));
    tooltipHide();
  });
  f.svg.append(overlay);
}
