/* Table rendering (also the accessibility fallback for every chart). */
"use strict";

import { el, fmtMeasure } from "../lib.js";
import { ctxDim, ctxGrain, fmtDimValue } from "./common.js";

export function renderTableInto(ctx, wrap) {
  const res = ctx.result;
  wrap.innerHTML = "";
  if (!res) return;
  const table = el("table", { class: "data" });
  const head = el("tr");
  for (const c of res.columns) head.append(el("th", { class: c.kind === "measure" ? "num" : "" }, c.label));
  table.append(el("thead", {}, head));
  const body = el("tbody");
  for (const row of res.rows) {
    const tr = el("tr");
    for (const c of res.columns) {
      const v = row[c.name];
      tr.append(el("td", { class: c.kind === "measure" ? "num" : "" },
        c.kind === "measure" ? fmtMeasure(v, c.format, false) : fmtDimValue(ctx, v, ctxDim(ctx, c.name), ctxGrain(ctx, c.name))));
    }
    body.append(tr);
  }
  table.append(body);
  wrap.append(table);
}
