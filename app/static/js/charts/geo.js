/* Geo bubble map over a vendored world outline (no external tiles). */
"use strict";

import { svgEl, fmtMeasure } from "../lib.js";
import { PALETTE, ctxDim, tooltipHide, tooltipShow, vizMessage } from "./common.js";
import { plotFrame } from "./frame.js";

let worldGeo = null;   // cached fetch of the vendored country outlines
function loadWorld() {
  if (!worldGeo) worldGeo = fetch("/static/world.geo.json").then((r) => r.json());
  return worldGeo;
}

export function renderGeo(ctx) {
  const res = ctx.result;
  const geoCol = res.columns.find((c) => c.kind === "dimension" && (ctxDim(ctx, c.name) || {}).geo);
  if (!geoCol || res.rows[0][`__lat_${geoCol.name}`] === undefined) {
    return vizMessage(ctx.container, "geo needs a map-enabled dimension (add geo: {lat, lon} to it in the model yaml)");
  }
  const mea = res.columns.find((c) => c.kind === "measure");
  const box = ctx.container;
  const token = String(Math.random());
  box.dataset.geoToken = token;
  vizMessage(box, "loading map…");
  loadWorld().then((world) => {
    if (!box.isConnected || box.dataset.geoToken !== token) return; // stale render
    box.innerHTML = "";
    const f = plotFrame(box);
    // equirectangular projection, fit 2:1 into the plot area
    const mapW = Math.min(f.W - 8, (f.H - 8) * 2), mapH = mapW / 2;
    const ox = (f.W - mapW) / 2, oy = (f.H - mapH) / 2;
    const px = (lon, lat) => [ox + ((lon + 180) / 360) * mapW, oy + ((90 - lat) / 180) * mapH];
    const land = svgEl("g");
    const ringPath = (ring) => "M" + ring.map(([lon, lat]) => px(lon, lat).map((n) => n.toFixed(1)).join(",")).join("L") + "Z";
    for (const feat of world.features) {
      const g = feat.geometry;
      const polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
      let d = "";
      for (const poly of polys) for (const ring of poly) d += ringPath(ring);
      if (d) land.append(svgEl("path", { class: "geo-land", d }));
    }
    f.svg.append(land);

    const vals = res.rows.map((r) => r[mea.name]).filter((v) => v != null);
    const vmax = Math.max(...vals.map(Math.abs), 1);
    for (const row of res.rows) {
      const lat = row[`__lat_${geoCol.name}`], lon = row[`__lon_${geoCol.name}`];
      const v = row[mea.name];
      if (lat == null || lon == null || v == null) continue;
      const [cx, cy] = px(lon, lat);
      const r = 4 + 22 * Math.sqrt(Math.abs(v) / vmax);
      const bubble = svgEl("circle", {
        cx, cy, r, fill: PALETTE[0], "fill-opacity": 0.4,
        stroke: PALETTE[0], "stroke-width": 1.5, class: ctx.onCross ? "cross-mark" : "",
      });
      const name = String(row[geoCol.name]);
      bubble.addEventListener("mousemove", (evt) => {
        bubble.setAttribute("fill-opacity", "0.65");
        tooltipShow(evt, name, res.columns.filter((c) => c.kind === "measure").map((m) => (
          { color: PALETTE[0], label: m.label, value: fmtMeasure(row[m.name], m.format, false) })));
      });
      bubble.addEventListener("mouseleave", () => { bubble.setAttribute("fill-opacity", "0.4"); tooltipHide(); });
      if (ctx.onCross) bubble.addEventListener("click", () => ctx.onCross(geoCol.name, name));
      f.svg.append(bubble);
      const label = svgEl("g", { class: "axis" });
      const t = svgEl("text", { x: cx, y: cy - r - 4, "text-anchor": "middle" });
      t.textContent = name;
      label.append(t);
      f.svg.append(label);
    }
  });
}
