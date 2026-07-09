/* Sankey: flow across the query's dimensions in order; first measure = width. */
"use strict";

import { svgEl, fmtMeasure } from "../lib.js";
import { MAX_SERIES, OTHER_COLOR, PALETTE, tooltipHide, tooltipShow, vizMessage } from "./common.js";
import { plotFrame } from "./frame.js";

export function renderSankey(ctx) {
  const res = ctx.result;
  const dimCols = res.columns.filter((c) => c.kind === "dimension");
  const mea = res.columns.find((c) => c.kind === "measure");
  const f = plotFrame(ctx.container);
  const GAP = 8, NODE_W = 12;

  // per-stage node totals (positive flows only)
  const stages = dimCols.map((col) => {
    const totals = new Map();
    for (const row of res.rows) {
      const v = row[mea.name];
      if (v == null || v <= 0) continue;
      const k = String(row[col.name]);
      totals.set(k, (totals.get(k) || 0) + v);
    }
    const nodes = [...totals.entries()].sort((a, b) => b[1] - a[1])
      .map(([key, total], i) => ({ key, total, color: i < MAX_SERIES ? PALETTE[i] : OTHER_COLOR }));
    return { col, nodes, total: nodes.reduce((s, n) => s + n.total, 0) };
  });
  if (!stages.every((s) => s.nodes.length)) return vizMessage(ctx.container, "no positive flows to draw");
  const scale = Math.min(...stages.map((s) => (f.plotH - GAP * (s.nodes.length - 1)) / s.total));

  const stageX = (i) => f.m.l + (i / (stages.length - 1)) * (f.plotW - NODE_W);
  for (const [i, stage] of stages.entries()) {
    let y = f.m.t + (f.plotH - (stage.total * scale + GAP * (stage.nodes.length - 1))) / 2;
    for (const node of stage.nodes) {
      node.x = stageX(i);
      node.y = y;
      node.h = Math.max(1, node.total * scale);
      node.inOff = 0; node.outOff = 0;
      y += node.h + GAP;
    }
  }

  // links between adjacent stages; the key separator must never occur in
  // real dimension values, so use NUL rather than a space
  const SEP = "\u0000";
  const linkGroup = svgEl("g");
  for (let i = 0; i < stages.length - 1; i++) {
    const [a, b] = [stages[i], stages[i + 1]];
    const flows = new Map();
    for (const row of res.rows) {
      const v = row[mea.name];
      if (v == null || v <= 0) continue;
      const k = String(row[a.col.name]) + SEP + String(row[b.col.name]);
      flows.set(k, (flows.get(k) || 0) + v);
    }
    const byNode = (stage, key) => stage.nodes.find((n) => n.key === key);
    const ordered = [...flows.entries()].sort((p, q) => {
      const [pa, pb] = p[0].split(SEP), [qa, qb] = q[0].split(SEP);
      return a.nodes.indexOf(byNode(a, pa)) - a.nodes.indexOf(byNode(a, qa))
        || b.nodes.indexOf(byNode(b, pb)) - b.nodes.indexOf(byNode(b, qb));
    });
    for (const [k, v] of ordered) {
      const [ka, kb] = k.split(SEP);
      const na = byNode(a, ka), nb = byNode(b, kb);
      const h = Math.max(1, v * scale);
      const x0 = na.x + NODE_W, x1 = nb.x;
      const y0 = na.y + na.outOff, y1 = nb.y + nb.inOff;
      na.outOff += h; nb.inOff += h;
      const xm = (x0 + x1) / 2;
      const path = svgEl("path", {
        d: `M${x0},${y0} C${xm},${y0} ${xm},${y1} ${x1},${y1} L${x1},${y1 + h} C${xm},${y1 + h} ${xm},${y0 + h} ${x0},${y0 + h} Z`,
        fill: na.color, "fill-opacity": 0.3,
      });
      path.addEventListener("mousemove", (evt) => {
        path.setAttribute("fill-opacity", "0.55");
        tooltipShow(evt, `${ka} → ${kb}`, [{ color: na.color, label: mea.label, value: fmtMeasure(v, mea.format, false) }]);
      });
      path.addEventListener("mouseleave", () => { path.setAttribute("fill-opacity", "0.3"); tooltipHide(); });
      linkGroup.append(path);
    }
  }
  f.svg.append(linkGroup);

  for (const [i, stage] of stages.entries()) {
    for (const node of stage.nodes) {
      const rect = svgEl("rect", {
        x: node.x, y: node.y, width: NODE_W, height: node.h,
        fill: node.color, rx: 2, class: ctx.onCross ? "cross-mark" : "",
      });
      rect.addEventListener("mousemove", (evt) =>
        tooltipShow(evt, node.key, [{ color: node.color, label: mea.label, value: fmtMeasure(node.total, mea.format, false) }]));
      rect.addEventListener("mouseleave", tooltipHide);
      if (ctx.onCross) rect.addEventListener("click", () => ctx.onCross(stage.col.name, node.key));
      const last = i === stages.length - 1;
      const label = svgEl("text", {
        x: last ? node.x - 6 : node.x + NODE_W + 6,
        y: node.y + node.h / 2 + 3,
        "text-anchor": last ? "end" : "start",
      });
      let text = node.key;
      if (text.length > 16) text = text.slice(0, 15) + "…";
      label.textContent = text;
      const g = svgEl("g", { class: "axis" });
      g.append(label);
      f.svg.append(rect, g);
    }
  }
}
