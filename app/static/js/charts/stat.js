/* Stat tiles: hero numbers for dimensionless queries. */
"use strict";

import { el, fmtMeasure } from "../lib.js";

export function renderStat(ctx) {
  const res = ctx.result;
  const meaCols = res.columns.filter((c) => c.kind === "measure");
  const grid = el("div", { class: "stat-grid" });
  for (const mea of meaCols) {
    grid.append(el("div", { class: "stat-tile" },
      el("div", { class: "val" }, fmtMeasure(res.rows[0][mea.name], mea.format)),
      el("div", { class: "lbl" }, mea.label)));
  }
  ctx.container.append(grid);
}
