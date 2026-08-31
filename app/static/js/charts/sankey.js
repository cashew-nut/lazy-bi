/* Sankey: flow across the query's dimensions in order; first measure = width. */
"use strict";

import { svgEl, fmtMeasure } from "../lib.js";
import { MAX_SERIES, OTHER_COLOR, PALETTE, tooltipHide, tooltipShow, vizMessage } from "./common.js";
import { plotFrame, plotSpace } from "./frame.js";

const GAP = 8;           // vertical space between two nodes of the same stage
const NODE_W = 12;
const MIN_NODE_H = 6;    // floor, so a hairline flow keeps a node you can see and hit
const SLOT_H = 20;       // vertical room one node's label wants (node + its gap)
const STAGE_W = 150;     // horizontal room a stage's labels want before the next stage

export function renderSankey(ctx) {
  const res = ctx.result;
  const dimCols = res.columns.filter((c) => c.kind === "dimension");
  const mea = res.columns.find((c) => c.kind === "measure");

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

  // The canvas the flows need, which is not the canvas the pane offers: every
  // node wants a slot its label fits in, and every stage wants room for those
  // labels before the next one. Fitting a crowded diagram into the pane
  // regardless is what collapsed it into hairlines — and once the gaps alone
  // outgrew the pane, the scale went negative and the whole layout landed
  // outside the frame, clipped, with no way to reach the rest of it. Ask for
  // the room instead; plotFrame gives the pane a scrollbar (frame.js).
  const space = plotSpace(ctx.container);
  const crowd = Math.max(...stages.map((s) => s.nodes.length));
  const f = plotFrame(ctx.container, {
    width: space.m.l + space.m.r + NODE_W + (stages.length - 1) * STAGE_W,
    height: space.m.t + space.m.b + crowd * SLOT_H,
  });

  // Proportional scale against the room we actually got — which is the room
  // asked for, unless MAX_CANVAS capped it (a pathological result set).
  let gap = GAP, floorH = MIN_NODE_H;
  let scale = Math.min(...stages.map((s) => Math.max(1, f.plotH - gap * (s.nodes.length - 1)) / s.total));
  // flooring the tiny nodes can push the tallest stage past the canvas; when
  // it does, squeeze heights, floor and gaps by one factor so the layout
  // lands inside the frame with every proportion intact
  const stageH = (s) => s.nodes.reduce((h, n) => h + Math.max(floorH, n.total * scale), 0) + gap * (s.nodes.length - 1);
  const tallest = Math.max(...stages.map(stageH));
  if (tallest > f.plotH) {
    const squeeze = f.plotH / tallest;
    scale *= squeeze; floorH *= squeeze; gap *= squeeze;
  }

  const stageX = (i) => f.m.l + (i / (stages.length - 1)) * (f.plotW - NODE_W);
  for (const [i, stage] of stages.entries()) {
    let y = f.m.t + (f.plotH - stageH(stage)) / 2;
    for (const node of stage.nodes) {
      node.x = stageX(i);
      node.y = y;
      node.h = Math.max(floorH, node.total * scale);
      node.inOff = 0; node.outOff = 0;
      y += node.h + gap;
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
      // a link's share of each end's node, so both ends fill their node
      // exactly even where the node was floored up off its true height
      const h0 = (v / na.total) * na.h, h1 = (v / nb.total) * nb.h;
      const x0 = na.x + NODE_W, x1 = nb.x;
      const y0 = na.y + na.outOff, y1 = nb.y + nb.inOff;
      na.outOff += h0; nb.inOff += h1;
      const xm = (x0 + x1) / 2;
      const path = svgEl("path", {
        d: `M${x0},${y0} C${xm},${y0} ${xm},${y1} ${x1},${y1} L${x1},${y1 + h1} C${xm},${y1 + h1} ${xm},${y0 + h0} ${x0},${y0 + h0} Z`,
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
