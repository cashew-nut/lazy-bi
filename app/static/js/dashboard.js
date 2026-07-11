/* Dashboards: tile grid, named filter-set views, ephemeral cross-filtering,
   grain override, and focus mode. Serves both the studio (editable) and the
   portal (view/grain/filter-value choices stay local — saveDash is a no-op
   there, so nothing a viewer sets, including values for filters the creator
   left blank, ever writes back into the saved view). */
"use strict";

import { renderViz, vizMessage } from "./charts/index.js";
import { fetchDimValues, tooltipHide } from "./charts/common.js";
import { FILTER_OPS, filterReady, timeValueControl, toApiFilter } from "./filters.js";
import { $, api, el } from "./lib.js";
import { hooks, modelByName, pubFor, refreshPubs, showView, state, valueCache } from "./state.js";

export async function refreshDashList() {
  state.dashboards = await api("/api/dashboards");
  renderDashList();
}

export function renderDashList() {
  const box = $("#dash-list");
  box.innerHTML = "";
  if (!state.dashboards.length) {
    box.append(el("div", { class: "empty-note" }, "no dashboards yet"));
    return;
  }
  for (const d of state.dashboards) {
    const tag = `${d.items.length} tile${d.items.length === 1 ? "" : "s"}`
      + (d.views.length > 1 ? ` · ${d.views.length} views` : "");
    const pub = pubFor(d.id);
    const item = el("div", { class: "saved-item" + (state.dash && d.id === state.dash.id ? " on" : "") },
      el("span", { class: "nm" }, d.name),
      ...(pub ? [el("span", { class: "tag", style: "color:#2bd97c", title: `published to /${pub.folder}` }, "◉")] : []),
      el("span", { class: "tag" }, tag),
      el("button", {
        class: "del", title: "delete",
        onclick: async (e) => {
          e.stopPropagation();
          await api(`/api/dashboards/${d.id}`, { method: "DELETE" });
          if (state.dash && state.dash.id === d.id) showView("builder");
          refreshDashList();
        },
      }, "✕"));
    item.addEventListener("click", () => openDashboard(d.id));
    box.append(item);
  }
}
hooks.renderDashList = renderDashList;

export async function openDashboard(id, portal = false) {
  const dash = await api(`/api/dashboards/${id}`);
  for (const view of dash.views) {
    view.filters = (view.filters || []).map((f) => ({ value: "", values: [], ...f }));
    // portal only: filters the creator left unset become the viewer's own
    // controls (see renderDashFilters) — decided once at load time, from
    // the as-saved value, so picking a value doesn't flip the control into
    // a fixed chip and strand the viewer unable to change it again
    if (portal) for (const f of view.filters) f.portalEditable = !!f.field && !filterReady(f);
  }
  state.portal = portal;
  showView("dashboard");
  state.dash = dash;
  state.crossFilter = null;  // ephemeral by design
  $("#dash-name").value = dash.name;
  $("#dash-name").readOnly = portal;
  $("#dash-grain").value = state.dashGrain;
  $("#dash-back").textContent = portal ? "◄ PORTAL" : "◄ BUILDER";
  renderPubStatus();
  renderDashList();
  renderDashboard();
}

// a "view" is a named filter set pushed down to every tile on the dashboard
export const activeView = () => state.dash.views[state.dash.active_view];

export async function saveDash() {
  if (!state.dash) return;
  if (state.portal) return;  // consumption mode: view/grain choices stay local
  const saved = await api(`/api/dashboards/${state.dash.id}`, {
    method: "PUT",
    body: {
      name: $("#dash-name").value.trim() || "untitled_dashboard",
      items: state.dash.items,
      views: state.dash.views,
      active_view: state.dash.active_view,
    },
  });
  state.dash.name = saved.name;
  state.dash.items = saved.items;
  state.dash.views = saved.views;
  state.dash.active_view = saved.active_view;
  refreshDashList();
}

export function renderViewBar() {
  const sel = $("#dash-view-select");
  sel.innerHTML = "";
  state.dash.views.forEach((v, i) => {
    const n = v.filters.length;
    sel.append(el("option", { value: i }, v.name + (n ? ` (${n} filter${n === 1 ? "" : "s"})` : "")));
  });
  sel.value = String(state.dash.active_view);
  $("#view-del").disabled = state.dash.views.length < 2;
}

// union of dimensions across the models behind this dashboard's tiles;
// a filter applies to every tile whose model has that dimension name
export function dashDimUnion() {
  const union = new Map();
  for (const item of state.dash.items) {
    const visual = state.dash.visuals[String(item.visual_id)];
    const model = visual && modelByName(visual.model);
    if (!model) continue;
    for (const d of model.dimensions) {
      if (!union.has(d.name)) union.set(d.name, { ...d, models: [] });
      union.get(d.name).models.push(model.name);
    }
  }
  return union;
}

// value control for one filter's value(s) — a plain/multi select for
// enumerable dimensions, timeValueControl for time dimensions, else text.
// `dim` may be a dashDimUnion() entry (carries `.models`) or a plain model
// dimension; `srcModel` is the model to pull distinct values from (or falsy
// if none, e.g. a spine-only dimension).
function filterValueControl(flt, dim, srcModel, onChange, onListLoaded) {
  const isTime = dim && dim.type === "time";
  const multi = flt.op === "in" || flt.op === "not_in";
  if (isTime && !multi && flt.op !== "contains") return timeValueControl(flt, onChange);
  const distinct = srcModel && valueCache[srcModel + ":" + flt.field];
  const haveList = Array.isArray(distinct) && distinct.length;
  if (multi || ((flt.op === "eq" || flt.op === "ne") && dim && !isTime)) {
    if (!haveList && srcModel) fetchDimValues(srcModel, flt.field, onListLoaded);
    const sel = el("select", multi
      ? { multiple: "multiple", onchange: (e) => { flt.values = [...e.target.selectedOptions].map((o) => o.value); onChange(); } }
      : { onchange: (e) => { flt.value = e.target.value; onChange(); } });
    if (!multi) sel.append(el("option", { value: "" }, "— pick —"));
    for (const v of (haveList ? distinct : [])) {
      const opt = el("option", { value: String(v) }, String(v));
      if (multi && flt.values.includes(String(v))) opt.selected = true;
      sel.append(opt);
    }
    if (!multi) sel.value = flt.value || "";
    return sel;
  }
  return el("input", {
    type: "text", value: flt.value || "", placeholder: "value…",
    onchange: (e) => { flt.value = e.target.value; onChange(); },
  });
}

// a dashboard-view dimension's source model: whichever backing model holds
// it as a plain (non-spine) column, so distinct values can be fetched
const unionSrcModel = (dim, field) =>
  dim && dim.models.find((m) => !modelByName(m).dimensions.find((d) => d.name === field).spine);

export function renderDashFilters() {
  const box = $("#dash-filters");
  box.innerHTML = "";
  const view = activeView();
  const union = dashDimUnion();
  if (state.portal) {
    // consumption mode: filters the creator gave a value stay fixed; ones
    // the creator left unset become the viewer's own controls here — never
    // saved back into the view (saveDash() no-ops in portal mode)
    const opLabel = (op) => (FILTER_OPS.find(([o]) => o === op) || [op, op])[1];
    for (const flt of view.filters) {
      if (!flt.field) continue;
      if (!flt.portalEditable) {
        box.append(el("div", { class: "dash-filter readonly" },
          el("span", {}, `${flt.field} ${opLabel(flt.op)} `),
          el("b", {}, flt.op === "in" || flt.op === "not_in" ? flt.values.join(", ") : String(flt.value))));
        continue;
      }
      const dim = union.get(flt.field);
      const row = el("div", { class: "dash-filter portal-editable" },
        el("span", {}, `${flt.field} ${opLabel(flt.op)} `));
      row.append(filterValueControl(flt, dim, unionSrcModel(dim, flt.field), dashFiltersChanged, renderDashFilters));
      box.append(row);
    }
    return;
  }
  view.filters.forEach((flt, idx) => {
    const row = el("div", { class: "dash-filter" });
    const dimSel = el("select", { onchange: (e) => { flt.field = e.target.value; flt.value = ""; flt.values = []; dashFiltersChanged(); } });
    for (const [name, d] of union) dimSel.append(el("option", { value: name }, d.label));
    if (!union.has(flt.field) && flt.field) dimSel.append(el("option", { value: flt.field }, flt.field));
    dimSel.value = flt.field;
    const opSel = el("select", { onchange: (e) => { flt.op = e.target.value; flt.value = ""; flt.values = []; dashFiltersChanged(); } });
    for (const [op, label] of FILTER_OPS) opSel.append(el("option", { value: op }, label));
    opSel.value = flt.op;
    row.append(dimSel, opSel);

    const dim = union.get(flt.field);
    row.append(filterValueControl(flt, dim, unionSrcModel(dim, flt.field), dashFiltersChanged, renderDashFilters));
    row.append(el("button", { class: "rm", onclick: () => { view.filters.splice(idx, 1); dashFiltersChanged(); } }, "✕"));
    box.append(row);
  });
}

let dashFilterTimer = null;
export function dashFiltersChanged() {
  clearTimeout(dashFilterTimer);
  dashFilterTimer = setTimeout(async () => {
    await saveDash();       // filters auto-save into the active view
    renderDashboard();      // re-run every tile with the new pushdown
  }, 250);
}

function renderDashAddSelect() {
  const sel = $("#dash-add-select");
  sel.innerHTML = "";
  api("/api/visuals").then((visuals) => {
    if (!visuals.length) { sel.append(el("option", { value: "" }, "— no saved visuals —")); return; }
    for (const v of visuals) sel.append(el("option", { value: v.id }, `${v.name} [${v.model}]`));
  });
}

export function renderDashboard() {
  const grid = $("#dash-grid");
  grid.innerHTML = "";
  state.tileCtxs = [];
  state.tiles = [];
  renderDashAddSelect();
  renderViewBar();
  renderDashFilters();
  renderCrossChip();
  if (!state.dash.items.length) {
    grid.append(el("div", { class: "msg", style: "grid-column: span 2" },
      "empty dashboard — save visuals in the builder, then + ADD them here"));
    return;
  }
  state.dash.items.forEach((item, idx) => {
    const visual = state.dash.visuals[String(item.visual_id)];
    const tile = el("div", { class: "tile" + (item.w === 2 ? " w2" : "") });
    const focusBtn = el("button", {
      title: "expand with ad-hoc filters (nothing is saved)",
      onclick: () => visual && openFocus(visual),
    }, "⤢");
    const widthBtn = el("button", {
      title: "toggle width",
      onclick: () => { item.w = item.w === 2 ? 1 : 2; saveDash().then(renderDashboard); },
    }, item.w === 2 ? "◨" : "◧");
    const rmBtn = el("button", {
      class: "rm", title: "remove from dashboard",
      onclick: () => { state.dash.items.splice(idx, 1); saveDash().then(renderDashboard); },
    }, "✕");
    const badge = el("span", { class: "tag", title: "pushed-down filters applied", style: "color:var(--neon)" });
    const head = el("div", { class: "tile-head" },
      el("span", { class: "nm" }, visual ? visual.name : "(deleted visual)"),
      el("span", { class: "tag" }, visual ? visual.model : "?"),
      badge, focusBtn, widthBtn, rmBtn);
    const legend = el("div", { class: "legend-box" });
    const body = el("div", { class: "chart-box" });
    tile.append(head, legend, body);
    grid.append(tile);
    const rec = { idx, item, visual, tile, body, legend, badge, run: () => runTile(rec) };
    state.tiles.push(rec);
    if (visual) rec.run();
    else vizMessage(body, "the saved visual behind this tile was deleted", true);
  });
  markCrossSource();
  updateTileBadges();
}

function updateTileBadges() {
  for (const rec of state.tiles) {
    const n = rec.visual ? tileFilters(rec.visual, rec.idx).length : 0;
    rec.badge.textContent = n ? `⧩${n}` : "";
    rec.badge.hidden = !n;
  }
}

// pushed-down filters for a tile: active-view filters plus the ephemeral
// cross-filter (never applied to the tile it came from), matched by dimension
export function tileFilters(visual, tileIdx) {
  const model = modelByName(visual.model);
  if (!model) return [];
  const has = (f) => model.dimensions.some((d) => d.name === f.field);
  const filters = activeView().filters.filter((f) => filterReady(f) && has(f));
  if (state.crossFilter && state.crossFilter.tileIdx !== tileIdx && has(state.crossFilter)) {
    filters.push({ field: state.crossFilter.field, op: "eq", value: state.crossFilter.value, values: [] });
  }
  return filters;
}

// effective query for a tile: saved spec + pushdown filters + grain override
export function tileQuery(visual, tileIdx) {
  const model = modelByName(visual.model);
  const q = visual.spec.query || {};
  const dims = (q.dimensions || []).map((d) => (typeof d === "string" ? { name: d } : { name: d.name, grain: d.grain }));
  if (state.dashGrain) {
    for (const d of dims) {
      const md = model.dimensions.find((x) => x.name === d.name);
      if (md && md.type === "time") d.grain = state.dashGrain;
    }
  }
  const pushdown = tileFilters(visual, tileIdx).map(toApiFilter);
  return {
    query: {
      ...q,
      dimensions: dims.map((d) => (d.grain ? { name: d.name, grain: d.grain } : d.name)),
      filters: [...(q.filters || []), ...pushdown],
    },
    dims,
  };
}

async function runTile(rec) {
  const { visual, body, legend, idx } = rec;
  const model = modelByName(visual.model);
  if (!model) return vizMessage(body, `model '${visual.model}' is gone`, true);
  vizMessage(body, "querying…");
  const { query, dims } = tileQuery(visual, idx);
  const ctx = {
    model, dims,
    chartType: visual.spec.chartType || "auto",
    container: body, legendBox: legend,
    onCross: (field, value) => toggleCross(idx, field, value),
  };
  ctx.rerender = () => renderViz(ctx);
  try {
    ctx.result = await api("/api/query", { method: "POST", body: query });
    state.tileCtxs.push(ctx);
    renderViz(ctx);
  } catch (err) {
    vizMessage(body, "QUERY ERROR // " + err.message, true);
  }
}

// ── ephemeral cross-filtering (never persisted) ──────────────

export function toggleCross(tileIdx, field, value) {
  const same = state.crossFilter && state.crossFilter.field === field && state.crossFilter.value === value;
  state.crossFilter = same ? null : { tileIdx, field, value };
  renderCrossChip();
  markCrossSource();
  updateTileBadges();
  for (const rec of state.tiles) {
    if (!rec.visual) continue;
    if (state.crossFilter && rec.idx === state.crossFilter.tileIdx) continue; // source keeps its render
    rec.run();
  }
}

function renderCrossChip() {
  const box = $("#cross-chip");
  box.innerHTML = "";
  if (!state.crossFilter) return;
  const cf = state.crossFilter;
  const chip = el("span", { class: "chip-x", title: "cross-filter from a tile click — click to clear" },
    `⧉ ${cf.field} = ${cf.value}`, el("b", {}, "✕"));
  chip.addEventListener("click", () => toggleCross(cf.tileIdx, cf.field, cf.value));
  box.append(chip);
}

function markCrossSource() {
  state.tiles.forEach((rec) =>
    rec.tile.classList.toggle("cross-source", !!state.crossFilter && rec.idx === state.crossFilter.tileIdx));
}

// ── publishing status (studio side) ──────────────────────────

export function renderPubStatus() {
  const box = $("#pub-status");
  box.innerHTML = "";
  if (!state.dash || state.portal) return;
  const pub = pubFor(state.dash.id);
  if (!pub) return;
  const un = el("a", { style: "color:var(--bad);cursor:pointer;margin-left:6px", title: "unpublish" }, "✕");
  un.addEventListener("click", async () => {
    if (!confirm(`Unpublish '${state.dash.name}' from the portal?`)) return;
    await api(`/api/publish/${state.dash.id}`, { method: "DELETE" });
    await refreshPubs();
    renderPubStatus();
    renderDashList();
  });
  box.append(el("b", {}, "◉ live"), ` /${pub.folder}`, un);
}

export async function publishCurrent() {
  if (!state.dash) return;
  const pub = pubFor(state.dash.id);
  const folder = prompt(
    "Publish to folder (slash-separated path, e.g. ops/street — empty publishes to the portal root):",
    pub ? pub.folder : "");
  if (folder === null) return;
  await api("/api/publish", { method: "POST", body: { dashboard_id: state.dash.id, folder } });
  await refreshPubs();
  renderPubStatus();
  renderDashList();
}

// ── focus mode: expand a tile with throwaway filters ─────────

export const focus = { visual: null, filters: [] };

export function openFocus(visual) {
  focus.visual = visual;
  focus.filters = [];
  $("#focus-name").textContent = visual.name;
  $("#focus-tag").textContent = visual.model;
  $("#focus-modal").hidden = false;
  renderFocusFilters();
  runFocus();
}

export function closeFocus() {
  focus.visual = null;
  $("#focus-modal").hidden = true;
  tooltipHide();
}

async function runFocus() {
  if (!focus.visual) return;
  const model = modelByName(focus.visual.model);
  const idx = (state.tiles.find((r) => r.visual && r.visual.id === focus.visual.id) || { idx: -1 }).idx;
  const { query, dims } = tileQuery(focus.visual, idx);
  const adhoc = focus.filters.filter(filterReady).map(toApiFilter);
  const ctx = {
    model, dims,
    chartType: focus.visual.spec.chartType || "auto",
    container: $("#focus-chart"), legendBox: $("#focus-legend"),
  };
  ctx.rerender = () => renderViz(ctx);
  vizMessage(ctx.container, "querying…");
  try {
    ctx.result = await api("/api/query", { method: "POST", body: { ...query, filters: [...query.filters, ...adhoc] } });
    $("#focus-meta").textContent = `${ctx.result.row_count} rows · ${ctx.result.elapsed_ms}ms`;
    renderViz(ctx);
  } catch (err) {
    vizMessage(ctx.container, "QUERY ERROR // " + err.message, true);
  }
}

let focusTimer = null;
const focusChanged = () => { clearTimeout(focusTimer); focusTimer = setTimeout(runFocus, 250); };

export function renderFocusFilters() {
  const box = $("#focus-filters");
  box.innerHTML = "";
  const model = modelByName(focus.visual.model);
  focus.filters.forEach((flt, idx) => {
    const row = el("div", { class: "dash-filter" });
    const dimSel = el("select", { onchange: (e) => { flt.field = e.target.value; flt.value = ""; flt.values = []; renderFocusFilters(); focusChanged(); } });
    for (const d of model.dimensions) dimSel.append(el("option", { value: d.name }, d.label));
    dimSel.value = flt.field;
    const opSel = el("select", { onchange: (e) => { flt.op = e.target.value; flt.value = ""; flt.values = []; renderFocusFilters(); focusChanged(); } });
    for (const [op, label] of FILTER_OPS) opSel.append(el("option", { value: op }, label));
    opSel.value = flt.op;
    row.append(dimSel, opSel);

    const dim = model.dimensions.find((d) => d.name === flt.field);
    const srcModel = dim && !dim.spine ? model.name : null;
    row.append(filterValueControl(flt, dim, srcModel, focusChanged, renderFocusFilters));
    row.append(el("button", { class: "rm", onclick: () => { focus.filters.splice(idx, 1); renderFocusFilters(); focusChanged(); } }, "✕"));
    box.append(row);
  });
}
