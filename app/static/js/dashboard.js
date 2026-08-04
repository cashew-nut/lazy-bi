/* Dashboards: tile grid, named filter-set views, ephemeral cross-filtering,
   grain override, and focus mode. Serves both the studio (editable) and the
   portal (view/grain/filter-value choices stay local — saveDash is a no-op
   there, so nothing a viewer sets, including values for filters the creator
   left blank, ever writes back into the saved view). */
"use strict";

import { renderViz, vizMessage } from "./charts/index.js";
import { tooltipHide } from "./charts/common.js";
import {
  FILTER_OPS, filterReady, filterValueControl, normalizeCategoricalOp,
  opsForDim, resetFilterForField, toApiFilter,
} from "./filters.js";
import { canAuthor } from "./auth.js";
import { $, api, apiRaw, el, fmtBytes } from "./lib.js";
import { navigate, paths } from "./router.js";
import { hooks, modelByName, pubFor, refreshPubs, showView, state } from "./state.js";

export async function refreshDashList() {
  state.dashboards = await api("/api/dashboards");
  renderDashList();
}

let dashListFilter = "";
export function setDashListFilter(text) { dashListFilter = text.trim().toLowerCase(); renderDashList(); }

export function renderDashList() {
  const box = $("#dash-list");
  box.innerHTML = "";
  if ($("#footer-dash-count")) $("#footer-dash-count").textContent = String(state.dashboards.length);
  if (!state.dashboards.length) {
    box.append(el("div", { class: "empty-note" }, "no dashboards yet"));
    return;
  }
  const dashboards = dashListFilter
    ? state.dashboards.filter((d) => d.name.toLowerCase().includes(dashListFilter))
    : state.dashboards;
  if (!dashboards.length) { box.append(el("div", { class: "empty-note" }, "no matches")); return; }
  for (const d of dashboards) {
    const tag = `${d.items.length} tile${d.items.length === 1 ? "" : "s"}`
      + (d.views.length > 1 ? ` · ${d.views.length} views` : "");
    const pub = pubFor(d.id);
    const item = el("div", { class: "saved-item" + (state.dash && d.id === state.dash.id ? " on" : "") },
      el("span", { class: "nm" }, d.name),
      ...(pub ? [el("span", { class: "tag", style: "color:var(--ok)", title: `published to /${pub.folder}` }, "◉")] : []),
      el("span", { class: "tag" }, tag),
      el("button", {
        class: "del", title: "delete",
        onclick: async (e) => {
          e.stopPropagation();
          await api(`/api/dashboards/${d.id}`, { method: "DELETE" });
          if (state.dash && state.dash.id === d.id) navigate(paths.studio());
          refreshDashList();
        },
      }, "✕"));
    item.addEventListener("click", () => navigate(paths.studioDashboard(d.id)));
    box.append(item);
  }
}
hooks.renderDashList = renderDashList;
hooks.openDashboard = openDashboard;

// portal-mode parameter picks: session-local, never saved (mirrors how
// portal-editable filter values and crossFilter/dashGrain stay ephemeral) —
// keyed by parameter name, reset whenever a dashboard is (re)opened
const portalParams = {};

export async function openDashboard(id, portal = false) {
  const dash = await api(`/api/dashboards/${id}`);
  for (const view of dash.views) {
    view.filters = (view.filters || []).map((f) => ({ value: "", values: [], ...f }));
    view.parameters = view.parameters || {};   // {name: value}; missing name -> that parameter's default
    // portal only: filters the creator left unset become the viewer's own
    // controls (see renderDashFilters) — decided once at load time, from
    // the as-saved value, so picking a value doesn't flip the control into
    // a fixed chip and strand the viewer unable to change it again
    if (portal) for (const f of view.filters) f.portalEditable = !!f.field && !filterReady(f);
  }
  state.portal = portal;
  showView("dashboard");
  // extracts and per-tile modes are session-only and per-dashboard: a fresh
  // open re-derives both, so a tile that fell back last time gets a fair
  // hearing this time (Principle V — nothing here is persisted)
  discardExtracts();
  tileModes.clear();
  state.dash = dash;
  state.crossFilter = null;  // ephemeral by design
  for (const k of Object.keys(portalParams)) delete portalParams[k];
  $("#dash-name").value = dash.name;
  $("#dash-name").readOnly = portal;
  $("#dash-grain").value = state.dashGrain;
  $("#dash-instant").checked = !!dash.instant;
  $("#dash-instant").disabled = portal || !canAuthor();
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
  if (!canAuthor()) return;  // viewers get the same consumption mode: edits stay local
  const saved = await api(`/api/dashboards/${state.dash.id}`, {
    method: "PUT",
    body: {
      name: $("#dash-name").value.trim() || "untitled_dashboard",
      items: state.dash.items,
      views: state.dash.views,
      active_view: state.dash.active_view,
      instant: !!state.dash.instant,
    },
  });
  state.dash.name = saved.name;
  state.dash.items = saved.items;
  state.dash.views = saved.views;
  state.dash.active_view = saved.active_view;
  state.dash.instant = saved.instant;
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

// a dashboard-view dimension's source model: whichever backing model holds
// it as a plain (non-spine) column, so distinct values can be fetched
const unionSrcModel = (dim, field) =>
  dim && dim.models.find((m) => !modelByName(m).dimensions.find((d) => d.name === field).spine);

// two parameter declarations count as "the same" only if every field
// matches exactly (name is compared by the caller) — values as a set
// (order-independent, de-duped) plus an exact default match (FR-014)
export function sameParamDef(a, b) {
  const aType = a.type || "int";
  const bType = b.type || "int";
  if (aType !== bType) return false;
  // numeric types sort numerically; string sorts lexicographically (default)
  const sorter = aType === "string" ? undefined : (x, y) => x - y;
  const av = [...new Set(a.values)].sort(sorter);
  const bv = [...new Set(b.values)].sort(sorter);
  return a.default === b.default && av.length === bv.length && av.every((v, i) => v === bv[i]);
}

// union of declared parameters across this dashboard's tiles, grouped by
// name -> [{visualId, visualName, def}] — the parameter-equivalent of
// dashDimUnion(), and the basis for both sharing (US4) and conflict
// detection (US5): a group of 1 is an ordinary independent parameter, a
// group of 2+ with identical defs is shared, a group of 2+ with differing
// defs is a conflict that must never be allowed to persist
export function dashParamUnion() {
  const byName = new Map();
  for (const item of state.dash.items) {
    const visual = state.dash.visuals[String(item.visual_id)];
    const params = visual && ((visual.spec.query || {}).parameters || []);
    for (const p of params || []) {
      if (!byName.has(p.name)) byName.set(p.name, []);
      byName.get(p.name).push({ visualId: item.visual_id, visualName: visual.name, def: p });
    }
  }
  return byName;
}

// classify dashParamUnion() into controllable groups (one control per name,
// applied to every visual in the group) and unresolved conflicts (name
// collision, non-identical defs) — conflicts should never actually reach
// here (blocked at add-tile/save time, see paramConflictMessage below), but
// this stays defensive against direct API edits or legacy saved state
export function classifyParams() {
  const shared = new Map();   // name -> {def, entries}
  const conflicts = [];       // [{name, entries}]
  for (const [name, entries] of dashParamUnion()) {
    const allSame = entries.every((e) => sameParamDef(e.def, entries[0].def));
    if (allSame) shared.set(name, { def: entries[0].def, entries });
    else conflicts.push({ name, entries });
  }
  return { shared, conflicts };
}

// would adding `candidate` to this dashboard create a parameter conflict
// with any visual already on it? Returns a human-readable message, or null.
// Checked before the tile is added (main.js) and re-checked authoritatively
// by the server on save (FR-015/FR-016).
export function paramConflictMessage(candidate) {
  const candidateParams = (candidate.spec.query || {}).parameters || [];
  if (!candidateParams.length) return null;
  for (const item of state.dash.items) {
    const existing = state.dash.visuals[String(item.visual_id)];
    if (!existing || existing.id === candidate.id) continue;
    const existingParams = (existing.spec.query || {}).parameters || [];
    for (const cp of candidateParams) {
      const ep = existingParams.find((p) => p.name === cp.name);
      if (ep && !sameParamDef(cp, ep)) {
        return `parameter '${cp.name}' conflicts between '${candidate.name}' and '${existing.name}' — `
          + "their declared values/default don't match, so both can't be on this dashboard together";
      }
    }
  }
  return null;
}

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
      // the creator left this filter's value unset — the viewer can pick
      // both the value and the comparison, not saved back to the view
      const dim = union.get(flt.field);
      normalizeCategoricalOp(flt, dim);
      const row = el("div", { class: "dash-filter portal-editable" }, el("span", {}, `${flt.field} `));
      const opSel = el("select", { onchange: (e) => { flt.op = e.target.value; flt.value = ""; flt.values = []; dashFiltersChanged(); renderDashFilters(); } });
      for (const [op, label] of opsForDim(dim)) opSel.append(el("option", { value: op }, label));
      opSel.value = flt.op;
      row.append(opSel);
      row.append(filterValueControl(flt, dim, unionSrcModel(dim, flt.field), dashFiltersChanged));
      box.append(row);
    }
    return;
  }
  view.filters.forEach((flt, idx) => {
    const row = el("div", { class: "dash-filter" });
    const dimSel = el("select", {
      onchange: (e) => { flt.field = e.target.value; resetFilterForField(flt, union.get(flt.field)); dashFiltersChanged(); renderDashFilters(); },
    });
    for (const [name, d] of union) dimSel.append(el("option", { value: name }, d.label));
    if (!union.has(flt.field) && flt.field) dimSel.append(el("option", { value: flt.field }, flt.field));
    dimSel.value = flt.field;

    const dim = union.get(flt.field);
    normalizeCategoricalOp(flt, dim);
    const opSel = el("select", { onchange: (e) => { flt.op = e.target.value; flt.value = ""; flt.values = []; dashFiltersChanged(); renderDashFilters(); } });
    for (const [op, label] of opsForDim(dim)) opSel.append(el("option", { value: op }, label));
    opSel.value = flt.op;
    row.append(dimSel, opSel);

    row.append(filterValueControl(flt, dim, unionSrcModel(dim, flt.field), dashFiltersChanged));
    row.append(el("button", { class: "rm", onclick: () => { view.filters.splice(idx, 1); dashFiltersChanged(); } }, "✕"));
    box.append(row);
  });
}

let dashFilterTimer = null;
export function dashFiltersChanged() {
  clearTimeout(dashFilterTimer);
  dashFilterTimer = setTimeout(async () => {
    await saveDash();       // filters auto-save into the active view
    // On an instant dashboard the tiles already hold the rows behind every
    // value of these filters, so a change is a re-slice, not a re-query —
    // re-running the tiles in place keeps the extracts. Any tile whose
    // extract can't answer the new values re-fetches its own (instantResult),
    // and a live tile does what it always did.
    if (!instantOn()) return renderDashboard();
    // saveDash() swaps state.dash.views for the server's copy, so the filter
    // controls are now bound to objects that are no longer the ones anybody
    // reads — rebuild them, exactly as the renderDashboard() path does. Skip
    // this and the *second* edit to a filter silently mutates an orphan.
    renderDashFilters();
    state.tiles.forEach((rec) => rec.visual && rec.run());
    updateTileBadges();
  }, 250);
}

// one control per declared parameter name on this dashboard (US3: even a
// single, non-shared visual's parameter gets a control here so its
// selection can be saved to the view; US4: a name shared identically by
// 2+ visuals collapses to one control applied to all of them). A name with
// conflicting definitions renders as a warning, never a control — that
// state should be unreachable (blocked at add/save time) but is handled
// defensively rather than silently guessing which definition wins.
// In portal mode the control stays live (matching portal-editable filters)
// but writes only to the session-local portalParams, never the saved view —
// setParamValue()/tileParameterValues() are what keep that split.
export function renderDashParams() {
  const box = $("#dash-params");
  box.innerHTML = "";
  const view = activeView();
  const { shared, conflicts } = classifyParams();
  for (const { name } of conflicts) {
    box.append(el("div", { class: "dash-filter readonly", style: "color:var(--bad)" },
      `⚠ '${name}' has conflicting definitions across tiles — remove one to resolve`));
  }
  for (const [name, { def }] of shared) {
    const saved = view.parameters[name] ?? def.default;
    const current = state.portal ? (portalParams[name] ?? saved) : saved;
    const seg = el("div", { class: "seg param-seg" }, el("span", { class: "lbl" }, name));
    for (const v of def.values) {
      const btn = el("button", {
        class: v === current ? "on" : "",
        onclick: () => setParamValue(name, v),
      }, String(v));
      seg.append(btn);
    }
    box.append(seg);
  }
}

function setParamValue(name, value) {
  if (state.portal) portalParams[name] = value;   // session-local, never saved
  else activeView().parameters[name] = value;
  dashParamsChanged();
}

let dashParamTimer = null;
export function dashParamsChanged() {
  renderDashParams();   // instant visual feedback on the control itself
  clearTimeout(dashParamTimer);
  dashParamTimer = setTimeout(async () => {
    await saveDash();       // no-ops in portal mode, same as filters — see saveDash()
    renderDashboard();      // re-run every affected tile with the new value
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
  // a full re-render is exactly the set of changes a cached extract can no
  // longer answer — a static filter, a parameter, a view switch, a tile added
  // or removed — so every extract is dropped here and re-fetched (FR-007)
  discardExtracts();
  renderDashAddSelect();
  renderViewBar();
  renderDashFilters();
  renderDashParams();
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
    // instant/live indicator: no data-role, so a read-only portal viewer sees
    // the same thing an author does (FR-012)
    const mode = el("span", { class: "tag mode-tag", hidden: "hidden" });
    const head = el("div", { class: "tile-head" },
      el("span", { class: "nm" }, visual ? visual.name : "(deleted visual)"),
      el("span", { class: "tag" }, visual ? visual.model : "?"),
      badge, mode, focusBtn, widthBtn, rmBtn);
    const legend = el("div", { class: "legend-box" });
    const body = el("div", { class: "chart-box" });
    tile.append(head, legend, body);
    grid.append(tile);
    const rec = { idx, item, visual, tile, body, legend, badge, mode, run: () => runTile(rec) };
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
// cross-filter (never applied to the tile it came from), matched by dimension.
// `cross: false` returns the static half alone — that's what an instant
// tile's extract is fetched with, since the cross-filter is the part the
// browser applies to the cached rows afterwards.
export function tileFilters(visual, tileIdx, cross = true) {
  const model = modelByName(visual.model);
  if (!model) return [];
  const has = (f) => model.dimensions.some((d) => d.name === f.field);
  const filters = activeView().filters.filter((f) => filterReady(f) && has(f));
  if (cross) {
    const term = crossTerm(visual, tileIdx);
    if (term) filters.push({ ...term, op: "eq", values: [] });
  }
  return filters;
}

// the ephemeral cross-filter as it applies to one tile: {field, value}, or
// null when there is none, it originated here, or this tile's model has no
// such dimension (today's silent no-op for a mismatched model)
function crossTerm(visual, tileIdx) {
  const cf = state.crossFilter;
  if (!cf || cf.tileIdx === tileIdx) return null;
  const model = modelByName(visual.model);
  if (!model || !model.dimensions.some((d) => d.name === cf.field)) return null;
  return { field: cf.field, value: cf.value };
}

// this tile's parameter values, sourced from the active dashboard view
// (never from whatever was baked into the visual's own saved spec) — a name
// with no saved view entry falls back to that parameter's own declared
// default (FR-012's "view saved before the parameter existed" edge case
// included, since a missing key behaves identically either way). In portal
// mode, a session-local pick from portalParams takes precedence over the
// saved view, matching how portal-editable filters override the creator's
// saved value without writing back to it.
export function tileParameterValues(visual) {
  const q = visual.spec.query || {};
  const view = activeView();
  const values = {};
  for (const p of q.parameters || []) {
    const saved = view.parameters[p.name] ?? p.default;
    values[p.name] = state.portal && p.name in portalParams ? portalParams[p.name] : saved;
  }
  return values;
}

// effective query for a tile: saved spec + pushdown filters + grain override
export function tileQuery(visual, tileIdx, cross = true) {
  const model = modelByName(visual.model);
  const q = visual.spec.query || {};
  const dims = (q.dimensions || []).map((d) => (typeof d === "string" ? { name: d } : { name: d.name, grain: d.grain }));
  if (state.dashGrain) {
    for (const d of dims) {
      const md = model.dimensions.find((x) => x.name === d.name);
      if (md && md.type === "time") d.grain = state.dashGrain;
    }
  }
  const pushdown = tileFilters(visual, tileIdx, cross).map(toApiFilter);
  return {
    query: {
      ...q,
      dimensions: dims.map((d) => (d.grain ? { name: d.name, grain: d.grain } : d.name)),
      filters: [...(q.filters || []), ...pushdown],
      parameter_values: tileParameterValues(visual),
    },
    dims,
  };
}

async function runTile(rec) {
  const { visual, body, legend, idx } = rec;
  const model = modelByName(visual.model);
  if (!model) return vizMessage(body, `model '${visual.model}' is gone`, true);
  // Runs for one tile can overlap — two quick edits to a dashboard filter
  // start two — and they don't necessarily finish in order. Since a filter
  // change now re-runs tiles *in place* rather than rebuilding the grid, both
  // runs write to the same live container, so the loser has to know to stay
  // quiet. Same token trick the builder uses for its own query races.
  const token = (rec.token = (rec.token || 0) + 1);
  const superseded = () => rec.token !== token;
  const { query, dims } = tileQuery(visual, idx);
  const ctx = {
    model, dims,
    chartType: visual.spec.chartType || "auto",
    sort: query.sort,
    xAxisTitle: visual.spec.xAxisTitle || "",
    yAxisTitle: visual.spec.yAxisTitle || "",
    yScale: visual.spec.yScale || "linear",
    container: body, legendBox: legend,
    onCross: (field, value) => toggleCross(idx, field, value),
  };
  ctx.rerender = () => renderViz(ctx);

  // instant first, live as the answer to anything that path can't do —
  // including the one-off interaction an otherwise-instant tile can't
  // answer from its cache (a finer grain, an unfetched dimension)
  ctx.result = await instantResult(rec, dims, query);
  if (superseded()) return;
  if (!ctx.result) {
    vizMessage(body, "querying…");
    try {
      ctx.result = await api("/api/query", { method: "POST", body: query });
    } catch (err) {
      if (superseded()) return;
      renderTileModes();
      return vizMessage(body, "QUERY ERROR // " + err.message, true);
    }
    if (superseded()) return;
  }
  state.tileCtxs.push(ctx);
  renderViz(ctx);
  renderTileModes();
}

// ── instant mode: client-side re-aggregation ─────────────────
// specs/016-instant-cross-filter/. Everything below is inert on a dashboard
// without `instant: true` — including the dynamic import(), so no other view
// ever loads Perspective or its wasm.

let instant = null;         // the lazily imported ./instant.js module
let instantDown = false;    // Perspective unavailable: live for the session

const extracts = new Map();   // visual id -> Extract, this dashboard's cache
const tileModes = new Map();  // visual id -> {mode, rows, bytes, reason, cap}

const instantOn = () => !!(state.dash && state.dash.instant) && !instantDown;

const tileMode = (rec) => tileModes.get(rec.item.visual_id) || {};

/** Load Perspective on first use. A failure here is not an error state: the
 *  whole dashboard quietly runs live for the rest of the session, exactly as
 *  it would with instant mode switched off. */
async function instantModule() {
  if (instantDown) return null;
  try {
    if (!instant) instant = await import("./instant.js");
    await instant.boot();
    return instant;
  } catch (err) {
    instantDown = true;
    instant = null;
    console.warn("[instant] engine unavailable, running live:", err.message);
    return null;
  }
}

function discardExtracts() {
  // entries are promises (see instantResult); a fetch still in flight when a
  // dashboard is torn down still has to give its wasm table back
  for (const pending of extracts.values()) {
    Promise.resolve(pending).then((ex) => ex && ex.destroy()).catch(() => {});
  }
  extracts.clear();
}

/** Every dimension any tile on this dashboard displays — the complete set of
 *  fields a cross-filter click can produce, and so what an extract has to
 *  carry to answer one locally (FR-006). The server drops the ones a given
 *  tile's model doesn't have, and the time dimensions, which no renderer
 *  cross-filters from. */
function crossDimensions() {
  const names = new Set();
  for (const item of state.dash.items) {
    const visual = state.dash.visuals[String(item.visual_id)];
    for (const d of ((visual && visual.spec.query) || {}).dimensions || []) {
      names.add(typeof d === "string" ? d : d.name);
    }
  }
  return [...names];
}

/** The active view's filter fields for one tile — the ones a viewer can change
 *  without the tile itself changing, so the extract carries them as columns
 *  rather than baking their current values in.
 *
 *  A field the *visual* also filters on is deliberately excluded: the tile's
 *  own saved filter and the view's filter arrive at the engine as one merged
 *  list, so hoisting that field would lift both out of the pushdown and
 *  quietly discard the one the visual is defined by. Rare, and not worth
 *  being clever about — that field just stays pushed down. */
function interactiveFilters(visual) {
  const ownFields = new Set((((visual || {}).spec || {}).query || {}).filters
    ?.map((f) => f.field) || []);
  return [...new Set(activeView().filters
    .filter((f) => f.field && !ownFields.has(f.field))
    .map((f) => f.field))];
}

/** The static filters a tile currently has, split into the ones its extract
 *  can answer locally and the ones that were baked into it when it was
 *  fetched. A change to anything in `baked` means the extract is stale. */
function splitFilters(rec, extract) {
  const local = [], baked = [];
  for (const f of tileFilters(rec.visual, rec.idx, false)) {
    (extract.localFilters.has(f.field) ? local : baked).push(f);
  }
  return { local, baked };
}

/** This tile's result, re-aggregated from its cached extract — or null,
 *  meaning "run it live". Null is a normal outcome, never a failure. */
async function instantResult(rec, dims, query, refetched = false) {
  if (!instantOn() || tileMode(rec).mode === "live") return null;
  const mod = await instantModule();
  if (!mod) return null;

  // the in-flight fetch is what's cached, not just its result: tiles load
  // concurrently, and the same visual placed twice on a dashboard would
  // otherwise fetch (and leak) two copies of one extract
  let pending = extracts.get(rec.item.visual_id);
  if (!pending) {
    pending = fetchExtract(rec, mod);
    extracts.set(rec.item.visual_id, pending);
  }
  const extract = await pending;
  if (!extract) {
    extracts.delete(rec.item.visual_id);
    return null;
  }

  // a filter this extract couldn't hoist is baked into it, so a change to one
  // means the cache is answering the wrong question — throw it away and fetch
  // the right one. Once only: a re-fetch that comes back still stale would be
  // a bug, not a reason to loop.
  const { local, baked } = splitFilters(rec, extract);
  if (!refetched && signature(baked) !== extract.bakedSignature) {
    discardExtract(rec);
    return instantResult(rec, dims, query, true);
  }

  const cross = crossTerm(rec.visual, rec.idx);
  try {
    return await extract.slice({
      dims,
      filters: [...local, ...(cross ? [{ ...cross, op: "eq" }] : [])],
      sort: query.sort, limit: query.limit,
    });
  } catch (err) {
    // a cache miss (finer grain, dimension not in the extract) costs this one
    // interaction a real query and nothing more — the tile stays instant
    if (err instanceof mod.NeedsRefetch) return null;
    markLive(rec, "re-aggregation failed: " + err.message);
    return null;
  }
}

// what a set of filters was, for spotting that one of them moved
const signature = (filters) => JSON.stringify(
  filters.map((f) => [f.field, f.op, f.value ?? "", ...(f.values || [])]).sort());

function discardExtract(rec) {
  const pending = extracts.get(rec.item.visual_id);
  extracts.delete(rec.item.visual_id);
  if (pending) Promise.resolve(pending).then((ex) => ex && ex.destroy()).catch(() => {});
}

async function fetchExtract(rec, mod) {
  // fetched without the cross-filter: that is the part the browser applies
  // to the cached rows, and baking it in would cache the wrong question
  const { query } = tileQuery(rec.visual, rec.idx, false);
  vizMessage(rec.body, "loading extract…");
  let res;
  try {
    res = await apiRaw("/api/query/extract", {
      ...query,
      cross_dimensions: crossDimensions(),
      interactive_filters: interactiveFilters(rec.visual),
    });
  } catch (err) {
    markLive(rec, "extract request failed: " + err.message);
    return null;
  }
  // a JSON answer means "this tile runs live" — a routine outcome, with the
  // reason an author reads off the badge (FR-013)
  if (!res.ok || !(res.headers.get("content-type") || "").includes("arrow")) {
    const body = await res.json().catch(() => ({}));
    const fallback = body.fallback || {};
    markLive(rec, fallback.reason || body.detail || `extract refused (${res.status})`, fallback.cap);
    return null;
  }
  try {
    const meta = decodeMeta(res.headers.get("X-Extract-Meta"));
    const extract = await mod.makeExtract(await res.arrayBuffer(), meta);
    // remember the filters that went in baked, so a later change to one of
    // them is recognisable as having invalidated this extract
    extract.bakedSignature = signature(
      tileFilters(rec.visual, rec.idx, false).filter((f) => !extract.localFilters.has(f.field)));
    tileModes.set(rec.item.visual_id, {
      mode: "instant", rows: meta.row_count, bytes: meta.byte_size,
      local: meta.local_filters || [],
    });
    return extract;
  } catch (err) {
    markLive(rec, "extract could not be loaded: " + err.message);
    return null;
  }
}

// base64 because the metadata rides on a response header and labels are UTF-8
function decodeMeta(header) {
  const bytes = Uint8Array.from(atob(header), (ch) => ch.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

/** Send a tile to the live path for the rest of the session (FR-009/FR-010) —
 *  never retried, so an oversized or unservable extract is fetched once, not
 *  on every interaction. */
function markLive(rec, reason, cap = null) {
  tileModes.set(rec.item.visual_id, { mode: "live", reason, cap });
}

/** Per-tile mode indicator, for authors and portal viewers alike (FR-012). */
function renderTileModes() {
  for (const rec of state.tiles) {
    if (!rec.mode) continue;
    const info = tileMode(rec);
    rec.mode.hidden = !instantOn() || !info.mode;
    if (rec.mode.hidden) continue;
    const live = info.mode === "live";
    rec.mode.textContent = live ? "live" : "⚡ instant";
    rec.mode.style.color = live ? "var(--ink-3)" : "var(--ok)";
    rec.mode.title = live
      ? `live query per interaction — ${info.reason || "not eligible for instant mode"}`
      : `re-aggregated in the browser · ${info.rows.toLocaleString()} rows · ${fmtBytes(info.bytes)}`;
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
  $("#focus-note").textContent = "ad-hoc filters — nothing here is saved";
  $("#focus-filter-add").hidden = false;
  $("#focus-modal").hidden = false;
  renderFocusFilters();
  runFocus();
}

export function closeFocus() {
  focus.visual = null;
  $("#focus-modal").hidden = true;
  tooltipHide();
}

// static focus mode: expand an already-rendered ctx (e.g. chat's grounding
// chart) into the shared modal at full size, read-only — there's no saved
// visual/tileQuery behind it to re-run against, so the ad-hoc filter row
// (which needs one) stays hidden here rather than sitting there inert
export function openFocusStatic(name, tag, ctx) {
  focus.visual = null;
  focus.filters = [];
  $("#focus-name").textContent = name;
  $("#focus-tag").textContent = tag;
  $("#focus-note").textContent = ctx.result ? `${ctx.result.row_count} rows · ${ctx.result.elapsed_ms}ms` : "";
  $("#focus-filters").innerHTML = "";
  $("#focus-filter-add").hidden = true;
  $("#focus-meta").textContent = "";
  $("#focus-modal").hidden = false;
  const focusCtx = { ...ctx, container: $("#focus-chart"), legendBox: $("#focus-legend") };
  focusCtx.rerender = () => renderViz(focusCtx);
  renderViz(focusCtx);
}

async function runFocus() {
  if (!focus.visual) return;
  const model = modelByName(focus.visual.model);
  const rec = state.tiles.find((r) => r.visual && r.visual.id === focus.visual.id);
  const idx = rec ? rec.idx : -1;
  const { query, dims } = tileQuery(focus.visual, idx);
  const adhoc = focus.filters.filter(filterReady).map(toApiFilter);
  const ctx = {
    model, dims,
    chartType: focus.visual.spec.chartType || "auto",
    sort: query.sort,
    xAxisTitle: focus.visual.spec.xAxisTitle || "",
    yAxisTitle: focus.visual.spec.yAxisTitle || "",
    yScale: focus.visual.spec.yScale || "linear",
    container: $("#focus-chart"), legendBox: $("#focus-legend"),
  };
  ctx.rerender = () => renderViz(ctx);
  // opening focus mode asks the same question the tile already answered, just
  // bigger — so an instant tile answers it from its cache too (FR-007). An
  // ad-hoc filter is a genuinely new question and goes to the engine.
  if (rec && !adhoc.length) {
    ctx.result = await instantResult(rec, dims, query);
  }
  if (!ctx.result) {
    vizMessage(ctx.container, "querying…");
    try {
      ctx.result = await api("/api/query", { method: "POST", body: { ...query, filters: [...query.filters, ...adhoc] } });
    } catch (err) {
      return vizMessage(ctx.container, "QUERY ERROR // " + err.message, true);
    }
  }
  $("#focus-meta").textContent = `${ctx.result.row_count} rows · ${ctx.result.elapsed_ms}ms`
    + (ctx.result.instant ? " · instant" : "");
  renderViz(ctx);
}

let focusTimer = null;
const focusChanged = () => { clearTimeout(focusTimer); focusTimer = setTimeout(runFocus, 250); };

export function renderFocusFilters() {
  const box = $("#focus-filters");
  box.innerHTML = "";
  const model = modelByName(focus.visual.model);
  focus.filters.forEach((flt, idx) => {
    const row = el("div", { class: "dash-filter" });
    const dimByName = (name) => model.dimensions.find((d) => d.name === name);
    const dimSel = el("select", {
      onchange: (e) => { flt.field = e.target.value; resetFilterForField(flt, dimByName(flt.field)); renderFocusFilters(); focusChanged(); },
    });
    for (const d of model.dimensions) dimSel.append(el("option", { value: d.name }, d.label));
    dimSel.value = flt.field;

    const dim = dimByName(flt.field);
    normalizeCategoricalOp(flt, dim);
    const opSel = el("select", { onchange: (e) => { flt.op = e.target.value; flt.value = ""; flt.values = []; renderFocusFilters(); focusChanged(); } });
    for (const [op, label] of opsForDim(dim)) opSel.append(el("option", { value: op }, label));
    opSel.value = flt.op;
    row.append(dimSel, opSel);

    const srcModel = dim && !dim.spine ? model.name : null;
    row.append(filterValueControl(flt, dim, srcModel, focusChanged));
    row.append(el("button", { class: "rm", onclick: () => { focus.filters.splice(idx, 1); renderFocusFilters(); focusChanged(); } }, "✕"));
    box.append(row);
  });
}
