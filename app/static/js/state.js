/* Shared application state + view switching.
   Cross-module notifications go through `hooks` (registered at import time by
   the owning module) so modules don't need circular imports. */
"use strict";

import { $, api } from "./lib.js";

export const state = {
  models: [],
  bundles: [],          // common dimensional models from /api/dimensions
  model: null,          // selected model public dict (builder)
  dims: [],             // [{name, grain?}]
  measures: [],         // [name]
  inlineMeasures: [],   // visual-scoped measure defs {name,label,expr,format}
  parameters: [],        // visual-scoped parameter declarations {name,values,default}
  parameterValues: {},   // current picks {name: value}; missing name -> that parameter's default
  filters: [],          // [{field, op, value, values}]
  chartType: "auto",
  xAxisTitle: "",        // user override; "" falls back to the auto-derived label
  yAxisTitle: "",
  yScale: "linear",      // "linear" | "log" — y/value axis scale
  sort: { by: "", desc: true },
  limit: 1000,
  visualId: null,
  visualName: "",
  showTable: false,
  result: null,
  queryToken: 0,
  view: "builder",      // builder | dashboard | editor | portal | modelling | modelform | bundleform
  dashboards: [],       // list from /api/dashboards
  dash: null,           // open dashboard {id, name, items, views, visuals}
  tileCtxs: [],         // rendered tile ctxs, for resize re-render
  tiles: [],            // tile records with per-tile run closures
  crossFilter: null,    // ephemeral {tileIdx, field, value} — never persisted
  dashGrain: "",        // session-only grain override — never persisted
  portal: false,        // current dashboard opened from the portal (read-only)
  portalFolder: "",     // folder path being browsed in the portal
  publications: [],     // published dashboards from /api/portal
  notebooks: [],         // list from /api/notebooks
};

export const valueCache = {};  // "model:dim" -> [distinct values] | "pending"

export const hooks = {};       // {renderDashList, refreshSaved} registered by modules

export const modelByName = (name) => state.models.find((m) => m.name === name);

export async function refreshPubs() {
  state.publications = (await api("/api/portal")).publications;
}

export const pubFor = (dashId) => state.publications.find((p) => p.dashboard_id === dashId);

export function showView(view) {
  state.view = view;
  for (const v of ["home", "builder", "dashboard", "editor", "portal", "modelling", "modelform", "bundleform", "lineage", "account", "chat", "notebook"]) {
    $(`#${v}-view`).hidden = view !== v;
  }
  if (view !== "dashboard") { state.dash = null; state.tileCtxs = []; }
  const authoring = ["editor", "modelling", "modelform", "bundleform", "lineage"];
  if (view === "builder" || authoring.includes(view)) state.portal = false;
  const mode = view === "portal" || (view === "dashboard" && state.portal) ? "portal"
    : authoring.includes(view) ? "modelling"
    : view === "account" ? "account" : view === "chat" ? "chat"
    : view === "home" || view === "notebook" ? "home" : "studio";
  document.body.dataset.mode = mode;
  document.body.classList.toggle("portal-dash", view === "dashboard" && state.portal);
  for (const btn of document.querySelectorAll("#mode-nav button")) {
    btn.classList.toggle("on", btn.dataset.mode === mode);
  }
  if (hooks.renderDashList) hooks.renderDashList();
  if (hooks.refreshSaved) hooks.refreshSaved();
}
