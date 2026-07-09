/* Pivot long query rows into {xs, series} for bar/line/ribbon charts. */
"use strict";

import { MAX_SERIES, OTHER_COLOR, PALETTE, fetchDimValues } from "./common.js";

export function pivotResult(ctx) {
  const res = ctx.result;
  const dimCols = res.columns.filter((c) => c.kind === "dimension");
  const meaCols = res.columns.filter((c) => c.kind === "measure");
  // x axis: prefer the time dimension
  const xCol = dimCols.find((c) => c.type === "time") || dimCols[0] || null;
  const seriesCol = dimCols.find((c) => c !== xCol) || null;
  const xs = [];
  const xIndex = new Map();
  for (const row of res.rows) {
    const xv = xCol ? row[xCol.name] : "total";
    if (!xIndex.has(xv)) { xIndex.set(xv, xs.length); xs.push(xv); }
  }

  let series = [];
  if (seriesCol) {
    // series from a dimension; only the first measure is charted
    const mea = meaCols[0];
    const byKey = new Map();
    for (const row of res.rows) {
      const key = String(row[seriesCol.name]);
      if (!byKey.has(key)) byKey.set(key, { key, label: key, values: new Array(xs.length).fill(null), total: 0 });
      const s = byKey.get(key);
      const v = row[mea.name];
      s.values[xIndex.get(xCol ? row[xCol.name] : "total")] = v;
      s.total += Math.abs(v || 0);
    }
    series = [...byKey.values()];
    // color follows the entity: slot = position in the dimension's full distinct
    // value list (stable under filters) when it fits the palette
    const fullValues = fetchDimValues(ctx.model.name, seriesCol.name, ctx.rerender);
    if (fullValues && fullValues.length <= MAX_SERIES) {
      for (const s of series) s.color = PALETTE[fullValues.map(String).indexOf(s.key)] || OTHER_COLOR;
      series.sort((a, b) => b.total - a.total);
    } else {
      // too many entities for fixed slots: keep top N-1, fold the rest into "Other"
      series.sort((a, b) => b.total - a.total);
      if (series.length > MAX_SERIES) {
        const kept = series.slice(0, MAX_SERIES - 1);
        const other = { key: "__other__", label: "Other", values: new Array(xs.length).fill(null), total: 0 };
        for (const s of series.slice(MAX_SERIES - 1)) {
          s.values.forEach((v, i) => { if (v != null) other.values[i] = (other.values[i] || 0) + v; });
          other.total += s.total;
        }
        series = [...kept, other];
      }
      series.forEach((s, i) => { s.color = s.key === "__other__" ? OTHER_COLOR : PALETTE[i]; });
    }
    return { xs, xCol, seriesCol, series, measure: mea, extraMeasures: meaCols.length - 1 };
  }

  // series = the selected measures; slot = measure's position in the model (stable)
  series = meaCols.map((mea) => ({
    key: mea.name,
    label: mea.label,
    format: mea.format,
    color: PALETTE[Math.max(0, ctx.model.measures.findIndex((m) => m.name === mea.name)) % PALETTE.length],
    values: res.rows.map((r) => r[mea.name]),
  }));
  return { xs, xCol, seriesCol: null, series, measure: meaCols[0], extraMeasures: 0 };
}
