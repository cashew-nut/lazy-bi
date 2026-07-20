/* Field-role readout: shows which selected dims/measures are actually
   driving each visual encoding (x axis, legend, y axis, sort) for the
   chart form currently in play — the auto-picked roles aren't otherwise
   visible anywhere in the builder UI. */
"use strict";

import { $, el } from "../lib.js";
import { GRAINS, ctxDim, ctxGrain } from "./common.js";
import { decideChart } from "./index.js";
import { pivotResult } from "./pivot.js";

function dimLabel(ctx, col) {
  if (!col) return null;
  const dim = ctxDim(ctx, col.name);
  const grain = ctxGrain(ctx, col.name);
  const label = (dim || col).label || col.name;
  return grain ? `${label} (${GRAINS[grain] || grain})` : label;
}

export function computeRoleMap(ctx) {
  if (!ctx.result || !ctx.result.rows.length) return null;
  const kind = decideChart(ctx);
  const dimCols = ctx.result.columns.filter((c) => c.kind === "dimension");
  const meaCols = ctx.result.columns.filter((c) => c.kind === "measure");
  const roles = [];

  if (kind === "bar" || kind === "line" || kind === "ribbon") {
    const pivot = pivotResult(ctx);
    roles.push({ role: "X-AXIS", value: dimLabel(ctx, pivot.xCol) || "—" });
    if (pivot.seriesCol) roles.push({ role: "LEGEND", value: dimLabel(ctx, pivot.seriesCol) });
    roles.push({
      role: "Y-AXIS",
      value: pivot.seriesCol ? pivot.measure.label : pivot.series.map((s) => s.label).join(", "),
    });
  } else if (kind === "scatter") {
    roles.push({ role: "X-AXIS", value: meaCols[0] ? meaCols[0].label : "—" });
    roles.push({ role: "Y-AXIS", value: meaCols[1] ? meaCols[1].label : "—" });
    if (dimCols[0]) roles.push({ role: "LABEL", value: dimLabel(ctx, dimCols[0]) });
    if (dimCols[1]) roles.push({ role: "LEGEND", value: dimLabel(ctx, dimCols[1]) });
  } else if (kind === "sankey") {
    roles.push({ role: "STAGES", value: dimCols.map((c) => dimLabel(ctx, c)).join(" → ") || "—" });
    if (meaCols[0]) roles.push({ role: "FLOW", value: meaCols[0].label });
  } else if (kind === "geo") {
    if (dimCols[0]) roles.push({ role: "REGION", value: dimLabel(ctx, dimCols[0]) });
    if (meaCols[0]) roles.push({ role: "VALUE", value: meaCols[0].label });
  } else if (kind === "stat") {
    if (meaCols[0]) roles.push({ role: "VALUE", value: meaCols[0].label });
  } else if (kind === "table") {
    roles.push({ role: "VIEW", value: "table — no chart roles" });
  }

  const sort = ctx.sort;
  if (sort && sort.by) {
    const col = dimCols.find((c) => c.name === sort.by) || meaCols.find((c) => c.name === sort.by);
    const label = col ? dimLabel(ctx, col) || col.label || sort.by : sort.by;
    roles.push({ role: "SORT", value: `${label} ${sort.desc ? "↓" : "↑"}` });
  } else {
    roles.push({ role: "SORT", value: "auto" });
  }
  return roles;
}

export function renderRoleMap(ctx) {
  const box = $("#role-map");
  if (!box) return;
  box.innerHTML = "";
  const roles = computeRoleMap(ctx);
  if (!roles || !roles.length) { box.hidden = true; return; }
  box.hidden = false;
  for (const r of roles) {
    box.append(el("div", { class: "role-chip" },
      el("span", { class: "role-key" }, r.role),
      el("span", { class: "role-val" }, r.value)));
  }
}
