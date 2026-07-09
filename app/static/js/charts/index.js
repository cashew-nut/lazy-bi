/* Chart dispatch: decide the form from the query shape, check requirements,
   render into the ctx. The same code path serves the builder canvas, dashboard
   tiles, and the focus modal. */
"use strict";

import { el } from "../lib.js";
import { renderBar } from "./bar.js";
import { ctxDim, renderLegend, tooltipHide, vizMessage } from "./common.js";
import { renderGeo } from "./geo.js";
import { renderLine } from "./line.js";
import { pivotResult } from "./pivot.js";
import { renderRibbon } from "./ribbon.js";
import { renderSankey } from "./sankey.js";
import { renderScatter } from "./scatter.js";
import { renderStat } from "./stat.js";
import { renderTableInto } from "./table.js";

export { vizMessage } from "./common.js";

export function decideChart(ctx) {
  if (ctx.chartType && ctx.chartType !== "auto") return ctx.chartType;
  const dimCount = (ctx.dims || []).length;
  if (dimCount === 0) return "stat";
  if (dimCount > 2) return "table";
  const hasTime = ctx.dims.some((d) => (ctxDim(ctx, d.name) || {}).type === "time");
  return hasTime ? "line" : "bar";
}

function vizRequirementError(kind, ctx) {
  const dims = ctx.result.columns.filter((c) => c.kind === "dimension").length;
  const meas = ctx.result.columns.filter((c) => c.kind === "measure").length;
  if (kind === "scatter" && (dims < 1 || meas < 2)) return "scatter needs ≥1 dimension and ≥2 measures (x, y)";
  if (kind === "sankey" && dims < 2) return "sankey needs ≥2 dimensions (the flow stages) and a measure";
  if (kind === "ribbon" && dims < 1) return "ribbon needs an x dimension plus categories (or several measures)";
  if (kind === "ribbon" && dims < 2 && meas < 2) return "ribbon needs a second dimension or a second measure to make bands";
  return null;
}

// Renders chart + legend for a ctx. Table kind renders into the container too
// (the builder handles its dedicated table pane separately).
export function renderViz(ctx) {
  tooltipHide();
  ctx.container.innerHTML = "";
  delete ctx.container.dataset.geoToken;
  if (ctx.legendBox) ctx.legendBox.innerHTML = "";
  if (!ctx.result) return;
  if (!ctx.result.rows.length) return vizMessage(ctx.container, "no rows — loosen the filters");

  const kind = decideChart(ctx);
  const problem = vizRequirementError(kind, ctx);
  if (problem) return vizMessage(ctx.container, problem);
  if (kind === "stat") return renderStat(ctx);
  if (kind === "scatter") return renderScatter(ctx);
  if (kind === "sankey") return renderSankey(ctx);
  if (kind === "geo") return renderGeo(ctx);
  if (kind === "table") {
    const scroll = el("div", { class: "table-scroll" });
    ctx.container.append(scroll);
    renderTableInto(ctx, scroll);
    return;
  }
  const pivot = pivotResult(ctx);
  renderLegend(ctx, pivot);
  if (kind === "ribbon") renderRibbon(ctx, pivot);
  else if (kind === "line") renderLine(ctx, pivot);
  else renderBar(ctx, pivot);
  return pivot;
}
