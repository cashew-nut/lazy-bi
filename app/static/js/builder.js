/* The query builder: model switcher, field rail, query strip, chart toolbar,
   query execution, saved visuals. */
"use strict";

import { decideChart, renderViz, vizMessage } from "./charts/index.js";
import { GRAINS } from "./charts/common.js";
import { renderTableInto } from "./charts/table.js";
import { renderRoleMap } from "./charts/rolemap.js";
import {
  FILTER_OPS, filterReady, filterValueControl, normalizeCategoricalOp,
  opsForDim, resetFilterForField, toApiFilter,
} from "./filters.js";
import { $, api, el } from "./lib.js";
import { navigate, paths } from "./router.js";
import { hooks, modelByName, showView, state } from "./state.js";

export const dimByName = (name) => state.model.dimensions.find((d) => d.name === name);
export const measureByName = (name) =>
  state.model.measures.find((m) => m.name === name)
  || state.inlineMeasures.find((m) => m.name === name);

function builderCtx() {
  return {
    model: state.model,
    dims: state.dims,
    chartType: state.chartType,
    sort: state.sort,
    xAxisTitle: state.xAxisTitle,
    yAxisTitle: state.yAxisTitle,
    yScale: state.yScale,
    result: state.result,
    container: $("#chart"),
    legendBox: $("#legend"),
    rerender: renderBuilderViz,
    onAxisTitleChange: (axis, value) => {
      if (axis === "x") state.xAxisTitle = value; else state.yAxisTitle = value;
      $(`#axis-title-${axis}`).value = value;
      renderBuilderViz();
      syncDisplayBtn();
    },
  };
}

// every declared parameter resolved to its current pick (state.parameterValues)
// or, absent one, its own declared default — always fully resolved so the
// server never has to guess which parameters a request "meant" to leave unset
export function currentParameterValues() {
  return Object.fromEntries(state.parameters.map((p) => [p.name, state.parameterValues[p.name] ?? p.default]));
}

export function buildQuery() {
  return {
    model: state.model.name,
    dimensions: state.dims.map((d) => (d.grain ? { name: d.name, grain: d.grain } : d.name)),
    measures: state.measures,
    inline_measures: state.inlineMeasures,
    filters: state.filters.filter((f) => f.field && filterReady(f)).map(toApiFilter),
    sort: state.sort.by ? { by: state.sort.by, desc: state.sort.desc } : null,
    limit: state.limit,
    parameters: state.parameters,
    parameter_values: currentParameterValues(),
  };
}

let runTimer = null;
export function scheduleRun() {
  clearTimeout(runTimer);
  runTimer = setTimeout(run, 200);
}

export async function run() {
  if (!state.model) return;
  if (!state.measures.length) {
    state.result = null;
    setMeta("");
    vizMessage($("#chart"), "select at least one measure to run a query");
    $("#legend").innerHTML = "";
    return;
  }
  const token = ++state.queryToken;
  setMeta("querying…", true);
  try {
    const result = await api("/api/query", { method: "POST", body: buildQuery() });
    if (token !== state.queryToken) return; // stale response
    state.result = result;
    // a multi-fact model scans no path of its own — it runs one scan per fact
    // and merges the results, so name the facts instead
    const via = state.model.kind === "composite"
      ? `${state.model.facts.length} facts <span class="path">${state.model.facts.map((f) => f.model).join(" + ")}</span>`
      : `lazy scan <span class="path">${state.model.path}</span>`;
    setMeta(`${result.row_count} rows · <span class="ms">${result.elapsed_ms}ms</span> · ${via}`);
    renderBuilderViz();
  } catch (err) {
    if (token !== state.queryToken) return;
    state.result = null;
    setMeta("");
    $("#legend").innerHTML = "";
    vizMessage($("#chart"), "QUERY ERROR // " + err.message, true);
  }
  syncSaveBadge();
}

function setMeta(html, busy = false) {
  $("#meta").innerHTML = html + (busy ? " <span style='color:var(--pink)'>▮▯▯</span>" : "");
}

export function renderBuilderViz() {
  const ctx = builderCtx();
  renderRoleMap(ctx);
  renderTableInto(ctx, $("#table-wrap")); // keep the table pane in sync
  const wantTable = state.showTable || (state.result && decideChart(ctx) === "table");
  $("#chart").style.display = wantTable ? "none" : "";
  $("#table-wrap").hidden = !wantTable;
  if (wantTable) { $("#legend").innerHTML = ""; return; }
  const pivot = renderViz(ctx);
  if (pivot && pivot.extraMeasures > 0) {
    setMeta($("#meta").innerHTML + ` · charting <b>${pivot.measure.label}</b> (+${pivot.extraMeasures} more in table view)`);
  }
}

// ── popover management ──────────────────────────────────────
// every popover in the builder (model switcher, chart-type menu, overflow
// menu, display panel, and the "+"-pill pickers below) funnels through this
// one open/close registry so opening one always closes the others, and Esc /
// an outside click (wired in main.js) can close whichever is open without
// needing to know which one that is
const STATIC_POPOVER_IDS = ["#model-pop", "#auto-menu", "#head-menu", "#dash-pick-menu", "#display-pop"];

let qsPickerEl = null;
let qsPickerBuild = null;

function closeQsPicker() {
  if (qsPickerEl) qsPickerEl.remove();
  qsPickerEl = null;
  qsPickerBuild = null;
}

export function closeAllPopovers() {
  for (const id of STATIC_POPOVER_IDS) { const e = $(id); if (e) e.hidden = true; }
  closeQsPicker();
  syncDisplayBtn();
}

function togglePopover(id) {
  const target = $(id);
  const opening = target.hidden;
  closeAllPopovers();
  target.hidden = !opening;
  return !opening;
}

export function toggleModelPop() { togglePopover("#model-pop"); }
export function toggleAutoMenu() { togglePopover("#auto-menu"); }
export function toggleHeadMenu() { togglePopover("#head-menu"); }
export function toggleDisplayPop() { togglePopover("#display-pop"); syncDisplayBtn(); }

// a "+"-pill picker (field/filter/parameter pickers, the grain menu, the
// query-strip overflow list): appended to <body> and positioned in viewport
// coordinates so it survives a query-strip re-render (which happens on
// nearly every edit inside these pickers) instead of being torn down with it
function openQsPicker(anchorEl, key, buildFn) {
  const already = qsPickerEl && qsPickerEl.dataset.key === key;
  closeAllPopovers();
  if (already) return;
  const rect = anchorEl.getBoundingClientRect();
  const pop = el("div", { class: "qs-picker" });
  pop.dataset.key = key;
  pop.style.top = (rect.bottom + 4) + "px";
  pop.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 268)) + "px";
  qsPickerEl = pop;
  qsPickerBuild = buildFn;
  buildFn(pop);
  document.body.append(pop);
}

// re-runs the picker's own build function in place — used by pickers whose
// content depends on state that just changed (e.g. picking a filter's field
// changes which operators/value-control apply)
function refreshQsPicker() {
  if (!qsPickerEl || !qsPickerBuild) return;
  qsPickerEl.innerHTML = "";
  qsPickerBuild(qsPickerEl);
}

export function openDashPickMenu() {
  if (!state.visualId) { alert("save the visual first"); return; }
  const list = $("#dash-pick-list");
  list.innerHTML = "";
  if (!state.dashboards.length) {
    list.append(el("div", { class: "empty-note" }, "no dashboards yet"));
  } else {
    for (const d of state.dashboards) {
      const row = el("div", { class: "menu-item" }, d.name);
      row.addEventListener("click", () => { closeAllPopovers(); addCurrentToDashboard(d.id); });
      list.append(row);
    }
  }
  $("#head-menu").hidden = true;
  $("#dash-pick-menu").hidden = false;
}

// ── model switcher (zone A) ──────────────────────────────────

function modelSourceLine(model) {
  if (model.kind === "composite") {
    return `${model.facts.length} fact${model.facts.length === 1 ? "" : "s"} · ${model.facts.map((f) => f.label || f.model).join(" + ")}`;
  }
  return model.path || "";
}

export function renderModelSwitch() {
  $("#model-switch-label").textContent = state.model.label;
  $("#model-switch-source").textContent = modelSourceLine(state.model);
  const pop = $("#model-pop");
  pop.innerHTML = "";
  for (const m of state.models) {
    const item = el("div", { class: "model-pop-item" + (m.name === state.model.name ? " on" : "") },
      el("div", { class: "nm" }, m.label),
      el("div", { class: "desc" }, m.description || ""));
    item.addEventListener("click", () => { closeAllPopovers(); if (m.name !== state.model.name) navigate(paths.studioModel(m.name)); });
    pop.append(item);
  }
}

// ── field rail (zone B) ───────────────────────────────────────

let fieldFilter = "";
// dataset/window-measure folders the user has manually expanded, keyed by
// label — survives the full re-render every toggle triggers, since the
// <details> elements themselves are thrown away and rebuilt each time
let openFolders = new Set();
// {kind:"dim"|"measure", name} of the first row matched by the current
// search, for ⏎ in the field search to toggle straight into the query
let firstFieldMatch = null;

export function setFieldFilter(text) {
  fieldFilter = text.trim().toLowerCase();
  renderFieldRail();
}

function resetSidebarFilters() {
  fieldFilter = "";
  openFolders = new Set();
  firstFieldMatch = null;
  if ($("#field-search")) $("#field-search").value = "";
}

const matchesFilter = (q, ...fields) => !q || fields.some((f) => f && f.toLowerCase().includes(q));

// dims grouped by the dataset they're sourced from (the fact's own table, or
// a common-dimension bundle pulled in via an import — see
// semantic.dimension_sources), preserving declared order: native dims first,
// then each import in the order the model lists it
function groupDimsByDataset(dims) {
  const groups = new Map();
  for (const dim of dims) {
    const key = dim.dataset || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(dim);
  }
  return groups;
}

const isWindowMeasure = (m) => /running_total\(|lag\(/.test(m.expr || "");

function toggleDim(dim) {
  const active = state.dims.find((d) => d.name === dim.name);
  if (active) state.dims = state.dims.filter((d) => d.name !== dim.name);
  else state.dims.push(dim.type === "time" ? { name: dim.name, grain: "1mo" } : { name: dim.name });
  renderFieldRail();
  renderQueryStrip();
  syncSortOptions();
  scheduleRun();
}

function toggleMeasure(name) {
  if (state.measures.includes(name)) state.measures = state.measures.filter((m) => m !== name);
  else state.measures.push(name);
  renderFieldRail();
  renderQueryStrip();
  syncSortOptions();
  scheduleRun();
}

export function toggleFirstFieldMatch() {
  if (!firstFieldMatch) return;
  if (firstFieldMatch.kind === "dim") toggleDim(dimByName(firstFieldMatch.name));
  else toggleMeasure(firstFieldMatch.name);
}

function selectedDimRow(dim, entry) {
  const chip = el("div", { class: "chip on" },
    el("span", { class: "tick" }, "◈"),
    el("span", { class: "lbl" }, dim.label));
  if (dim.type === "time") {
    const grainSel = el("select", {
      class: "grain",
      onchange: (e) => { entry.grain = e.target.value; syncSortOptions(); renderQueryStrip(); scheduleRun(); },
    });
    for (const [g, label] of Object.entries(GRAINS)) grainSel.append(el("option", { value: g }, label));
    grainSel.value = entry.grain || "1mo";
    grainSel.addEventListener("click", (e) => e.stopPropagation());
    chip.append(grainSel);
  }
  chip.addEventListener("click", () => toggleDim(dim));
  return chip;
}

function selectedMeasureRow(mea) {
  const isInline = state.inlineMeasures.some((m) => m.name === mea.name);
  const chip = el("div", { class: "chip on measure" + (isInline ? " inline" : ""), title: mea.expr },
    el("span", { class: "tick" }, "◆"),
    el("span", { class: "lbl" }, mea.label || mea.name));
  if (isInline) {
    const edit = el("button", { class: "mini", title: "edit in the measure lab" }, "✎");
    edit.addEventListener("click", (e) => { e.stopPropagation(); hooks.openLab(mea); });
    const rm = el("button", { class: "mini rm", title: "remove from this visual" }, "✕");
    rm.addEventListener("click", (e) => {
      e.stopPropagation();
      state.inlineMeasures = state.inlineMeasures.filter((m) => m.name !== mea.name);
      state.measures = state.measures.filter((m) => m !== mea.name);
      renderFieldRail(); renderQueryStrip(); syncSortOptions(); scheduleRun();
    });
    chip.append(el("span", { class: "hint" }, "visual"), edit, rm);
  } else {
    chip.append(el("span", { class: "hint" }, mea.format === "number" ? "" : mea.format));
  }
  chip.addEventListener("click", () => toggleMeasure(mea.name));
  return chip;
}

function availableDimRow(dim) {
  const row = el("div", { class: "field-row" },
    el("span", { class: "tick" }, "◇"),
    el("span", { class: "lbl" }, dim.label),
    el("span", { class: "hint" }, dim.spine ? "spine" : dim.type === "time" ? "time" : ""));
  row.addEventListener("click", () => toggleDim(dim));
  return row;
}

function availableMeasureRow(mea) {
  const row = el("div", { class: "field-row", title: mea.expr },
    el("span", { class: "tick" }, "◇"),
    el("span", { class: "lbl" }, mea.label),
    el("span", { class: "hint" }, mea.format === "number" ? "" : mea.format));
  row.addEventListener("click", () => toggleMeasure(mea.name));
  return row;
}

function availableInlineMeasureRow(mea) {
  const edit = el("button", { class: "mini", title: "edit in the measure lab" }, "✎");
  edit.addEventListener("click", (e) => { e.stopPropagation(); hooks.openLab(mea); });
  const rm = el("button", { class: "mini rm", title: "remove from this visual" }, "✕");
  rm.addEventListener("click", (e) => {
    e.stopPropagation();
    state.inlineMeasures = state.inlineMeasures.filter((m) => m.name !== mea.name);
    renderFieldRail();
  });
  const row = el("div", { class: "field-row", title: mea.expr },
    el("span", { class: "tick" }, "◇"),
    el("span", { class: "lbl" }, mea.label || mea.name),
    el("span", { class: "hint" }, "visual"), edit, rm);
  row.addEventListener("click", () => toggleMeasure(mea.name));
  return row;
}

// one folder (dataset bundle, or the "window measures" bucket), collapsed by
// default. forceOpen expands it while a search is narrowing the list to
// matches; a manual expand toggles openFolders directly rather than relying
// on the <details> "toggle" event, which can also fire from the initial
// `open` attribute set below and would otherwise wrongly "stick" a search's
// forced-open folder open once the search clears
function fieldFolder(key, items, rowBuilder, forceOpen) {
  const body = el("div", { class: "tree-children chip-list" });
  for (const item of items) body.append(rowBuilder(item));
  const summary = el("summary", {},
    el("span", { class: "tree-caret" }, "▸"),
    el("span", { class: "nm" }, key),
    el("span", { class: "tree-count" }, String(items.length)));
  const attrs = { class: "tree-folder" };
  if (forceOpen || openFolders.has(key)) attrs.open = "";
  const folder = el("details", attrs, summary, body);
  summary.addEventListener("click", (e) => {
    e.preventDefault();
    folder.open = !folder.open;
    if (folder.open) openFolders.add(key); else openFolders.delete(key);
  });
  return folder;
}

function groupHeader(text, kind, count) {
  return el("div", { class: "fr-group-head" },
    el("span", { class: `fr-group-title ${kind}` }, text),
    el("span", { class: `fr-group-rule ${kind}` }),
    el("span", { class: "fr-group-count" }, String(count)));
}

export function renderFieldRail() {
  const list = $("#field-rail-list");
  if (!list || !state.model) return;
  list.innerHTML = "";
  firstFieldMatch = null;

  const qDims = state.dims
    .map((entry) => ({ entry, dim: dimByName(entry.name) }))
    .filter((x) => x.dim && matchesFilter(fieldFilter, x.dim.label, x.dim.name));
  const qMeas = state.measures
    .map((n) => measureByName(n))
    .filter((m) => m && matchesFilter(fieldFilter, m.label || m.name, m.name));

  const nativeDims = state.model.dimensions.filter((d) => (d.dataset || state.model.label) === state.model.label);
  const importedGroups = [...groupDimsByDataset(state.model.dimensions)].filter(([key]) => key && key !== state.model.label);
  const nativeMeasures = state.model.measures.filter((m) => !isWindowMeasure(m));
  const windowMeasures = state.model.measures.filter(isWindowMeasure);

  const availDims = nativeDims.filter((d) => !state.dims.some((sd) => sd.name === d.name) && matchesFilter(fieldFilter, d.label, d.name));
  const availMeasures = nativeMeasures.filter((m) => !state.measures.includes(m.name) && matchesFilter(fieldFilter, m.label, m.name));
  const availInline = state.inlineMeasures.filter((m) => !state.measures.includes(m.name) && matchesFilter(fieldFilter, m.label || m.name, m.name));

  const folders = [];
  for (const [dataset, dims] of importedGroups) {
    const matched = dims.filter((d) => !state.dims.some((sd) => sd.name === d.name) && matchesFilter(fieldFilter, d.label, d.name));
    if (matched.length) folders.push({ key: dataset, items: matched, rowBuilder: availableDimRow });
  }
  const matchedWindow = windowMeasures.filter((m) => !state.measures.includes(m.name) && matchesFilter(fieldFilter, m.label, m.name));
  if (matchedWindow.length) folders.push({ key: "window measures", items: matchedWindow, rowBuilder: availableMeasureRow });

  const availCount = availDims.length + availMeasures.length + availInline.length
    + folders.reduce((n, f) => n + f.items.length, 0);
  const totalShown = qDims.length + qMeas.length + availCount;

  if (fieldFilter) {
    if (availDims.length) firstFieldMatch = { kind: "dim", name: availDims[0].name };
    else if (availMeasures.length) firstFieldMatch = { kind: "measure", name: availMeasures[0].name };
    else if (availInline.length) firstFieldMatch = { kind: "measure", name: availInline[0].name };
    else if (folders.length) firstFieldMatch = { kind: folders[0].rowBuilder === availableDimRow ? "dim" : "measure", name: folders[0].items[0].name };
  }

  list.append(groupHeader("in this query", "in-query", qDims.length + qMeas.length));
  for (const { entry, dim } of qDims) list.append(selectedDimRow(dim, entry));
  for (const mea of qMeas) list.append(selectedMeasureRow(mea));

  list.append(groupHeader("available", "available", availCount));
  for (const dim of availDims) list.append(availableDimRow(dim));
  for (const mea of availMeasures) list.append(availableMeasureRow(mea));
  for (const mea of availInline) list.append(availableInlineMeasureRow(mea));
  if (folders.length) {
    const wrap = el("div", { class: "fr-folders" });
    for (const f of folders) wrap.append(fieldFolder(f.key, f.items, f.rowBuilder, !!fieldFilter));
    list.append(wrap);
  }

  if (!totalShown) list.append(el("div", { class: "empty-note" }, "no matches"));
}

function openMeasurePicker(anchor) {
  openQsPicker(anchor, "add-measure", (pop) => {
    const search = el("input", { type: "text", placeholder: "find a measure…", spellcheck: "false", autocomplete: "off" });
    const listBox = el("div", { class: "chip-list" });
    const renderRows = () => {
      const q = search.value.trim().toLowerCase();
      listBox.innerHTML = "";
      const items = [
        ...state.model.measures.filter((m) => !state.measures.includes(m.name)),
        ...state.inlineMeasures.filter((m) => !state.measures.includes(m.name)),
      ].filter((m) => matchesFilter(q, m.label || m.name, m.name));
      if (!items.length) { listBox.append(el("div", { class: "empty-note" }, "no matches")); return; }
      for (const m of items) {
        const row = el("div", { class: "field-row" }, el("span", { class: "tick" }, "◇"), el("span", { class: "lbl" }, m.label || m.name));
        row.addEventListener("click", () => { closeAllPopovers(); toggleMeasure(m.name); });
        listBox.append(row);
      }
    };
    search.addEventListener("input", renderRows);
    pop.append(el("div", { class: "field-search" }, el("span", { class: "fs-ic" }, "⌕"), search), listBox);
    renderRows();
    setTimeout(() => search.focus(), 0);
  });
}

function openDimensionPicker(anchor) {
  openQsPicker(anchor, "add-dimension", (pop) => {
    const search = el("input", { type: "text", placeholder: "find a dimension…", spellcheck: "false", autocomplete: "off" });
    const listBox = el("div", { class: "chip-list" });
    const renderRows = () => {
      const q = search.value.trim().toLowerCase();
      listBox.innerHTML = "";
      const items = state.model.dimensions.filter((d) => !state.dims.some((sd) => sd.name === d.name) && matchesFilter(q, d.label, d.name));
      if (!items.length) { listBox.append(el("div", { class: "empty-note" }, "no matches")); return; }
      for (const d of items) {
        const row = el("div", { class: "field-row" }, el("span", { class: "tick" }, "◇"), el("span", { class: "lbl" }, d.label));
        row.addEventListener("click", () => { closeAllPopovers(); toggleDim(d); });
        listBox.append(row);
      }
    };
    search.addEventListener("input", renderRows);
    pop.append(el("div", { class: "field-search" }, el("span", { class: "fs-ic" }, "⌕"), search), listBox);
    renderRows();
    setTimeout(() => search.focus(), 0);
  });
}

function openGrainMenu(anchorEl, entry) {
  openQsPicker(anchorEl, "grain:" + entry.name, (pop) => {
    for (const [g, label] of Object.entries(GRAINS)) {
      const item = el("div", { class: "model-pop-item" + (g === (entry.grain || "1mo") ? " on" : "") }, el("div", { class: "nm" }, label));
      item.addEventListener("click", () => {
        entry.grain = g;
        closeAllPopovers();
        syncSortOptions();
        renderFieldRail();
        renderQueryStrip();
        scheduleRun();
      });
      pop.append(item);
    }
  });
}

// ── query strip ────────────────────────────────────────────────

function pillMeasure(mea) {
  const rm = el("span", { class: "qs-rm" }, "✕");
  rm.addEventListener("click", (e) => { e.stopPropagation(); toggleMeasure(mea.name); });
  return el("span", { class: "qs-pill qs-measure" }, "◆ " + (mea.label || mea.name) + " ", rm);
}

function pillDim(dim, entry) {
  const rm = el("span", { class: "qs-rm" }, "✕");
  rm.addEventListener("click", (e) => { e.stopPropagation(); toggleDim(dim); });
  const kids = ["◈ " + dim.label + " "];
  if (dim.type === "time") {
    const grain = el("span", { class: "qs-grain" }, `· ${GRAINS[entry.grain || "1mo"]} ▾`);
    grain.addEventListener("click", (e) => { e.stopPropagation(); openGrainMenu(grain, entry); });
    kids.push(grain, " ");
  }
  kids.push(rm);
  return el("span", { class: "qs-pill qs-dim" }, ...kids);
}

function filterOpLabel(op) {
  const found = FILTER_OPS.find(([o]) => o === op);
  return found ? found[1] : op;
}

function filterValueLabel(flt) {
  if (flt.op === "in" || flt.op === "not_in") return flt.values.length ? flt.values.join(", ") : "…";
  return flt.value !== "" && flt.value != null ? String(flt.value) : "…";
}

function pillFilter(flt, idx) {
  const dim = dimByName(flt.field);
  const rm = el("span", { class: "qs-rm" }, "✕");
  rm.addEventListener("click", (e) => {
    e.stopPropagation();
    state.filters.splice(idx, 1);
    closeAllPopovers();
    renderQueryStrip();
    scheduleRun();
  });
  const pill = el("span", { class: "qs-pill qs-filter" },
    (dim ? dim.label : flt.field) + " ",
    el("span", { class: "qs-op" }, filterOpLabel(flt.op)),
    " " + filterValueLabel(flt) + " ",
    rm);
  pill.dataset.filterIdx = String(idx);
  pill.addEventListener("click", (e) => { if (e.target !== rm) openFilterPicker(pill, idx); });
  return pill;
}

function pillParam(p, idx) {
  const current = state.parameterValues[p.name] ?? p.default;
  const rm = el("span", { class: "qs-rm" }, "✕");
  rm.addEventListener("click", (e) => {
    e.stopPropagation();
    delete state.parameterValues[p.name];
    state.parameters.splice(idx, 1);
    closeAllPopovers();
    renderParamToggleBar();
    renderQueryStrip();
    scheduleRun();
  });
  const pill = el("span", { class: "qs-pill qs-param" }, `${p.name} = ${current} `, rm);
  pill.dataset.paramIdx = String(idx);
  pill.addEventListener("click", (e) => { if (e.target !== rm) openParamPicker(pill, idx); });
  return pill;
}

function openFilterPicker(anchorEl, idx) {
  openQsPicker(anchorEl, "filter:" + idx, (pop) => {
    const flt = state.filters[idx];
    if (!flt) { closeAllPopovers(); return; }
    const dim = dimByName(flt.field);
    normalizeCategoricalOp(flt, dim);
    const row = el("div", { class: "filter-row" });
    const dimSel = el("select", {
      onchange: (e) => { flt.field = e.target.value; resetFilterForField(flt, dimByName(flt.field)); renderQueryStrip(); scheduleRun(); refreshQsPicker(); },
    });
    for (const d of state.model.dimensions) dimSel.append(el("option", { value: d.name }, d.label));
    dimSel.value = flt.field;
    const opSel = el("select", {
      class: "op",
      onchange: (e) => { flt.op = e.target.value; flt.value = ""; flt.values = []; renderQueryStrip(); scheduleRun(); refreshQsPicker(); },
    });
    for (const [op, label] of opsForDim(dim)) opSel.append(el("option", { value: op }, label));
    opSel.value = flt.op;
    const rm = el("button", {
      class: "rm",
      onclick: () => { state.filters.splice(idx, 1); closeAllPopovers(); renderQueryStrip(); scheduleRun(); },
    }, "✕");
    row.append(el("div", { class: "top" }, dimSel, opSel, rm));
    const srcModel = dim && !dim.spine ? state.model.name : null;
    row.append(filterValueControl(flt, dim, srcModel, () => { renderQueryStrip(); scheduleRun(); }, () => refreshQsPicker()));
    pop.append(row);
  });
}

function addFilterAndEdit() {
  if (!state.model.dimensions.length) return;
  state.filters.push({ field: state.model.dimensions[0].name, op: "eq", value: "", values: [] });
  const idx = state.filters.length - 1;
  renderQueryStrip();
  const pillEl = $("#query-strip").querySelector(`[data-filter-idx="${idx}"]`);
  if (pillEl) openFilterPicker(pillEl, idx);
}

// ── visual parameters ────────────────────────────────────────
// Declared here (name, values, default) and referenced from a measure via
// param('name') in the Measure Lab. This editor doubles as the "standalone
// visual" viewer control (renderParamToggleBar) — this app has no separate
// read-only single-visual view, so previewing a value here is the closest
// analog to a viewer toggling it (dashboards get their own tile-level
// control in dashboard.js).

// Parses the comma-separated "values" text input per the parameter's
// declared type: int/float use numeric parsing (deduped, NaN dropped);
// string keeps each trimmed entry as-is (deduped) — commas inside a string
// value aren't supported by this simple editor (see specs/010-parameter-
// type-generalization/spec.md Assumptions).
const VALUES_PLACEHOLDER = { int: "1,2,3,4", float: "1.5,2,3.25", string: "east,west,north" };

function parseValuesInput(text, type) {
  const parts = text.split(",").map((s) => s.trim()).filter((s) => s !== "");
  if (type === "string") return [...new Set(parts)];
  const toNum = type === "float" ? parseFloat : (s) => parseInt(s, 10);
  return [...new Set(parts.map(toNum).filter((n) => !Number.isNaN(n)))];
}

function parseDefaultInput(text, type) {
  if (type === "string") return text;
  return type === "float" ? parseFloat(text) : parseInt(text, 10);
}

export function addParameter() {
  const n = state.parameters.length + 1;
  const p = { name: `param_${n}`, type: "int", values: [1, 2, 3, 4], default: 1 };
  state.parameters.push(p);
  state.parameterValues[p.name] = p.default;
  renderQueryStrip();
  renderParamToggleBar();
  scheduleRun();
}

function openParamAddPicker(anchor) {
  openQsPicker(anchor, "add-param", (pop) => {
    const btn = el("button", { class: "ghost" }, "+ parameter");
    btn.addEventListener("click", () => {
      addParameter();
      closeAllPopovers();
      const idx = state.parameters.length - 1;
      const pillEl = $("#query-strip").querySelector(`[data-param-idx="${idx}"]`);
      if (pillEl) openParamPicker(pillEl, idx);
    });
    pop.append(btn);
  });
}

function openParamPicker(anchorEl, idx) {
  openQsPicker(anchorEl, "param:" + idx, (pop) => {
    const p = state.parameters[idx];
    if (!p) { closeAllPopovers(); return; }
    const type = p.type || "int";
    const row = el("div", { class: "filter-row" });
    const nameInput = el("input", {
      value: p.name, placeholder: "period_list", spellcheck: "false",
      onchange: (e) => {
        const old = p.name;
        p.name = e.target.value.trim();
        if (old in state.parameterValues) {
          state.parameterValues[p.name] = state.parameterValues[old];
          delete state.parameterValues[old];
        }
        renderParamToggleBar(); renderQueryStrip(); scheduleRun(); refreshQsPicker();
      },
    });
    const typeSel = el("select", {
      title: "parameter type",
      onchange: (e) => {
        p.type = e.target.value;
        // a type switch invalidates whatever was parsed under the old type —
        // clearing (rather than reinterpreting) avoids silently keeping
        // now-mismatched-type data (research.md §7)
        p.values = [];
        p.default = undefined;
        delete state.parameterValues[p.name];
        renderParamToggleBar(); renderQueryStrip(); scheduleRun(); refreshQsPicker();
      },
    });
    for (const t of ["int", "float", "string"]) typeSel.append(el("option", { value: t }, t));
    typeSel.value = type;
    const rm = el("button", {
      class: "rm", title: "remove parameter",
      onclick: () => {
        delete state.parameterValues[p.name];
        state.parameters.splice(idx, 1);
        renderParamToggleBar(); closeAllPopovers(); renderQueryStrip(); scheduleRun();
      },
    }, "✕");
    const valuesInput = el("input", {
      value: p.values.join(","), placeholder: VALUES_PLACEHOLDER[type],
      onchange: (e) => {
        p.values = parseValuesInput(e.target.value, type);
        if (!p.values.includes(p.default)) p.default = p.values[0];
        if (!p.values.includes(state.parameterValues[p.name])) state.parameterValues[p.name] = p.default;
        renderParamToggleBar(); renderQueryStrip(); scheduleRun(); refreshQsPicker();
      },
    });
    const defaultSel = el("select", {
      onchange: (e) => { p.default = parseDefaultInput(e.target.value, type); renderParamToggleBar(); renderQueryStrip(); scheduleRun(); },
    });
    for (const v of p.values) defaultSel.append(el("option", { value: v }, String(v)));
    defaultSel.value = String(p.default);
    row.append(el("div", { class: "top" }, nameInput, typeSel, rm));
    row.append(el("div", { class: "row2" },
      el("div", {}, el("div", { class: "field-label" }, "VALUES"), valuesInput),
      el("div", {}, el("div", { class: "field-label" }, "DEFAULT"), defaultSel)));
    pop.append(row);
  });
}

export function renderParamToggleBar() {
  const bar = $("#param-toggle-bar");
  bar.innerHTML = "";
  bar.hidden = !state.parameters.length;
  if (!state.parameters.length) return;
  for (const p of state.parameters) {
    const seg = el("div", { class: "seg param-seg" }, el("span", { class: "lbl" }, p.name));
    const current = state.parameterValues[p.name] ?? p.default;
    for (const v of p.values) {
      const btn = el("button", {
        class: v === current ? "on" : "",
        onclick: () => { state.parameterValues[p.name] = v; renderParamToggleBar(); renderQueryStrip(); scheduleRun(); },
      }, String(v));
      seg.append(btn);
    }
    bar.append(seg);
  }
}

// group labels render only if they have pills — except SHOW, which always
// renders (accented when empty, per the "select at least one measure" guard
// in run()) since it's the one group a fresh query can't do without
function appendGroup(strip, label, pills, onAdd, forceShow) {
  if (!pills.length && !forceShow) return;
  const lbl = el("span", { class: "qs-label" + (strip.children.length ? " qs-sp" : "") }, label);
  strip.append(lbl);
  for (const p of pills) strip.append(p);
  const add = el("span", { class: "qs-add" + (!pills.length ? " qs-accent" : "") }, "+");
  add.addEventListener("click", () => onAdd(add));
  strip.append(add);
}

function collapseQueryStripOverflow(strip) {
  requestAnimationFrame(() => {
    const kids = [...strip.children];
    if (!kids.length) return;
    const firstTop = kids[0].offsetTop;
    const rowHeight = kids[0].offsetHeight || 20;
    const lineOf = (node) => Math.round((node.offsetTop - firstTop) / (rowHeight + 8));
    const overflow = kids.filter((k) => lineOf(k) >= 2);
    if (!overflow.length) return;
    for (const k of overflow) k.remove();
    const more = el("span", { class: "qs-more" }, `+${overflow.length} more ▾`);
    more.addEventListener("click", () => {
      openQsPicker(more, "overflow", (pop) => {
        const box = el("div", { class: "chip-list" });
        for (const k of overflow) box.append(k);
        pop.append(box);
      });
    });
    strip.append(more);
  });
}

export function renderQueryStrip() {
  const strip = $("#query-strip");
  if (!strip || !state.model) return;
  strip.innerHTML = "";

  const showMeas = state.measures.map((n) => measureByName(n)).filter(Boolean);
  appendGroup(strip, "SHOW", showMeas.map((m) => pillMeasure(m)), openMeasurePicker, true);

  const byDims = state.dims.map((entry) => ({ entry, dim: dimByName(entry.name) })).filter((x) => x.dim);
  appendGroup(strip, "BY", byDims.map(({ dim, entry }) => pillDim(dim, entry)), openDimensionPicker, true);

  appendGroup(strip, "WHERE", state.filters.map((flt, idx) => pillFilter(flt, idx)), addFilterAndEdit, true);

  if (state.parameters.length) {
    appendGroup(strip, "WITH", state.parameters.map((p, idx) => pillParam(p, idx)), openParamAddPicker, true);
  }

  collapseQueryStripOverflow(strip);
  syncAutoBtn();
  syncDisplayBtn();
  syncSaveBadge();
}

// ── chart type (AUTO · <resolved> ▾) ─────────────────────────

export function renderChartSeg() {
  for (const btn of $("#chart-seg").querySelectorAll("button")) {
    btn.classList.toggle("on", btn.dataset.t === state.chartType);
  }
}

export function syncAutoBtn() {
  const btn = $("#auto-btn");
  if (!btn || !state.model) return;
  if (state.chartType === "auto") {
    const resolved = state.dims.length || state.measures.length ? decideChart(builderCtx()) : "bar";
    btn.textContent = `AUTO · ${resolved} ▾`;
    btn.classList.remove("on");
  } else {
    btn.textContent = `${state.chartType.toUpperCase()} ▾`;
    btn.classList.add("on");
  }
}

export function renderYScaleSeg() {
  for (const btn of $("#yscale-seg").querySelectorAll("button")) {
    btn.classList.toggle("on", btn.dataset.s === state.yScale);
  }
}

// ── display popover ──────────────────────────────────────────

function displayIsNonDefault() {
  return !!state.sort.by || state.limit !== 1000 || state.yScale !== "linear" || !!state.xAxisTitle || !!state.yAxisTitle;
}

export function syncDisplayBtn() {
  const btn = $("#display-btn");
  const pop = $("#display-pop");
  if (!btn || !pop) return;
  btn.classList.toggle("accent", !pop.hidden || displayIsNonDefault());
}

export function resetDisplay() {
  state.sort = { by: "", desc: true };
  state.limit = 1000;
  state.yScale = "linear";
  state.xAxisTitle = "";
  state.yAxisTitle = "";
  syncSortOptions();
  $("#sort-dir").value = "desc";
  $("#limit").value = state.limit;
  $("#axis-title-x").value = "";
  $("#axis-title-y").value = "";
  renderYScaleSeg();
  syncDisplayBtn();
  renderBuilderViz();
  scheduleRun();
}

export function syncSortOptions() {
  const sel = $("#sort-by");
  const current = state.sort.by;
  sel.innerHTML = "";
  sel.append(el("option", { value: "" }, "auto"));
  for (const d of state.dims) sel.append(el("option", { value: d.name }, (dimByName(d.name) || { label: d.name }).label));
  for (const m of state.measures) sel.append(el("option", { value: m }, (measureByName(m) || { label: m }).label));
  sel.value = [...sel.options].some((o) => o.value === current) ? current : "";
  state.sort.by = sel.value;
}

// ── footer rows (saved visuals / dashboards) ─────────────────

let footerOpen = null; // null | "saved" | "dashboards"

export function toggleFooterRow(key) {
  footerOpen = footerOpen === key ? null : key;
  syncFooterRows();
}

export function syncFooterRows() {
  $("#footer-row-saved").classList.toggle("open", footerOpen === "saved");
  $("#footer-row-dash").classList.toggle("open", footerOpen === "dashboards");
  $("#footer-expand").hidden = !footerOpen;
  $("#saved-filter").hidden = footerOpen !== "saved";
  $("#saved-list").hidden = footerOpen !== "saved";
  $("#dash-list-filter").hidden = footerOpen !== "dashboards";
  $("#dash-list").hidden = footerOpen !== "dashboards";
  $("#new-dash").hidden = footerOpen !== "dashboards";
}

// ── saved visuals ────────────────────────────────────────────

let savedVisualsCache = [];
let savedFilter = "";

export function setSavedFilter(text) { savedFilter = text.trim().toLowerCase(); renderSavedList(); }

function renderSavedList() {
  const box = $("#saved-list");
  box.innerHTML = "";
  if ($("#footer-saved-count")) $("#footer-saved-count").textContent = String(savedVisualsCache.length);
  if (!savedVisualsCache.length) { box.append(el("div", { class: "empty-note" }, "nothing saved yet — build a query and hit SAVE")); return; }
  const visuals = savedVisualsCache.filter((v) => matchesFilter(savedFilter, v.name, v.model));
  if (!visuals.length) { box.append(el("div", { class: "empty-note" }, "no matches")); return; }
  for (const v of visuals) {
    const item = el("div", { class: "saved-item" + (v.id === state.visualId && state.view === "builder" ? " on" : "") },
      el("span", { class: "nm" }, v.name),
      el("span", { class: "tag" }, v.model),
      el("button", {
        class: "del", title: "delete",
        onclick: async (e) => {
          e.stopPropagation();
          await api(`/api/visuals/${v.id}`, { method: "DELETE" });
          if (state.visualId === v.id) { state.visualId = null; }
          refreshSaved();
        },
      }, "✕"));
    item.addEventListener("click", () => loadVisual(v));
    box.append(item);
  }
}

export async function refreshSaved() {
  savedVisualsCache = await api("/api/visuals");
  renderSavedList();
}
hooks.refreshSaved = refreshSaved;

export function currentSpec() {
  return {
    query: buildQuery(),
    chartType: state.chartType,
    xAxisTitle: state.xAxisTitle,
    yAxisTitle: state.yAxisTitle,
    yScale: state.yScale,
  };
}

// ── save badge (SAVED · <age> / UNSAVED) ─────────────────────

let lastSavedSpec = null;
let lastSavedAt = 0;

function elapsedLabel(ms) {
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m AGO`;
  return `${Math.round(m / 60)}h AGO`;
}

function markSaved() {
  lastSavedSpec = JSON.stringify(currentSpec());
  lastSavedAt = Date.now();
  syncSaveBadge();
}

export function syncSaveBadge() {
  const badge = $("#save-badge");
  if (!badge || !state.model) return;
  if (!state.visualId) {
    badge.textContent = state.measures.length ? "UNSAVED" : "";
    badge.classList.toggle("unsaved", !!state.measures.length);
    return;
  }
  const dirty = lastSavedSpec !== JSON.stringify(currentSpec());
  badge.classList.toggle("unsaved", dirty);
  badge.textContent = dirty ? "UNSAVED" : `SAVED · ${elapsedLabel(lastSavedAt)}`;
}

export async function saveVisual(asNew) {
  const name = $("#visual-name").value.trim() || "untitled_visual";
  const payload = { name, model: state.model.name, spec: currentSpec() };
  let saved;
  try {
    saved = (!asNew && state.visualId)
      ? await api(`/api/visuals/${state.visualId}`, { method: "PUT", body: payload })
      : await api("/api/visuals", { method: "POST", body: payload });
  } catch (err) {
    alert("Couldn't save: " + err.message);
    return;
  }
  state.visualId = saved.id;
  state.visualName = saved.name;
  markSaved();
  refreshSaved();
  navigate(paths.studioVisual(saved.id), { replace: true });
}

export async function duplicateVisual() {
  if (!state.visualId) { alert("save the visual first"); return; }
  const payload = { name: (state.visualName || "untitled_visual") + " copy", model: state.model.name, spec: currentSpec() };
  try {
    const saved = await api("/api/visuals", { method: "POST", body: payload });
    refreshSaved();
    navigate(paths.studioVisual(saved.id), { replace: true });
  } catch (err) {
    alert("Couldn't duplicate: " + err.message);
  }
}

export async function copyQueryJson() {
  try {
    await navigator.clipboard.writeText(JSON.stringify(buildQuery(), null, 2));
  } catch {
    alert("Couldn't copy — clipboard access is blocked");
  }
}

export async function deleteCurrentVisual() {
  if (!state.visualId) return;
  if (!confirm(`Delete '${state.visualName || "this visual"}'? This can't be undone.`)) return;
  try {
    await api(`/api/visuals/${state.visualId}`, { method: "DELETE" });
  } catch (err) {
    alert("Couldn't delete: " + err.message);
    return;
  }
  refreshSaved();
  navigate(paths.studio());
}

export async function addCurrentToDashboard(dashId) {
  if (!state.visualId) { alert("save the visual first"); return; }
  try {
    const dash = await api(`/api/dashboards/${dashId}`);
    dash.items.push({ visual_id: state.visualId, w: 1 });
    await api(`/api/dashboards/${dashId}`, {
      method: "PUT",
      body: { name: dash.name, items: dash.items, views: dash.views, active_view: dash.active_view, instant: !!dash.instant },
    });
    alert(`Added to '${dash.name}'.`);
  } catch (err) {
    alert("Couldn't add to dashboard: " + err.message);
  }
}

export function loadVisual(v) {
  const model = modelByName(v.model);
  if (!model) return vizMessage($("#chart"), `model '${v.model}' is no longer defined`, true);
  showView("builder");
  state.model = model;
  const q = v.spec.query || {};
  state.dims = (q.dimensions || []).map((d) => (typeof d === "string" ? { name: d } : { name: d.name, grain: d.grain }));
  state.measures = q.measures || [];
  state.inlineMeasures = q.inline_measures || [];
  state.parameters = q.parameters || [];
  state.parameterValues = { ...(q.parameter_values || {}) };
  state.filters = (q.filters || []).map((f) => ({ field: f.field, op: f.op, value: f.value ?? "", values: f.values || [] }));
  state.sort = q.sort ? { by: q.sort.by, desc: !!q.sort.desc } : { by: "", desc: true };
  state.limit = q.limit || 1000;
  state.chartType = v.spec.chartType || "auto";
  state.xAxisTitle = v.spec.xAxisTitle || "";
  state.yAxisTitle = v.spec.yAxisTitle || "";
  state.yScale = v.spec.yScale || "linear";
  state.visualId = v.id;
  state.visualName = v.name;
  state.showTable = false;
  resetSidebarFilters();
  syncBuilderUI();
  markSaved();
  refreshSaved();
  scheduleRun();
}

export function syncBuilderUI() {
  renderModelSwitch();
  renderFieldRail();
  renderQueryStrip();
  renderParamToggleBar();
  renderChartSeg();
  renderYScaleSeg();
  syncSortOptions();
  syncAutoBtn();
  $("#sort-dir").value = state.sort.desc ? "desc" : "asc";
  $("#limit").value = state.limit;
  $("#axis-title-x").value = state.xAxisTitle;
  $("#axis-title-y").value = state.yAxisTitle;
  $("#visual-name").value = state.visualName;
  $("#toggle-table").classList.toggle("on", state.showTable);
  syncDisplayBtn();
  syncSaveBadge();
  closeAllPopovers();
  footerOpen = null;
  syncFooterRows();
}

export function selectModel(name) {
  state.model = modelByName(name);
  state.dims = [];
  state.measures = [];
  state.inlineMeasures = [];
  state.parameters = [];
  state.parameterValues = {};
  if (hooks.closeLab) hooks.closeLab(false);
  state.filters = [];
  state.sort = { by: "", desc: true };
  state.xAxisTitle = "";
  state.yAxisTitle = "";
  state.yScale = "linear";
  resetSidebarFilters();
  state.visualId = null;
  lastSavedSpec = null;
  lastSavedAt = 0;
  // sensible starting query: time dim at month grain (if any) + first measure
  const timeDim = state.model.dimensions.find((d) => d.type === "time");
  if (timeDim) state.dims.push({ name: timeDim.name, grain: "1mo" });
  if (state.model.measures.length) state.measures.push(state.model.measures[0].name);
  state.visualName = "";
  syncBuilderUI();
  scheduleRun();
}
hooks.selectModel = selectModel;

// router entry points for /studio and /studio/visual/:id — see router.js
hooks.defaultStudio = () => {
  showView("builder");
  if (state.models.length) selectModel(state.models[0].name);
};
hooks.openVisualById = async (id) => {
  const visuals = await api("/api/visuals");
  const v = visuals.find((x) => x.id === id);
  if (!v) throw new Error(`visual ${id} not found`);
  loadVisual(v);
};

// pull fresh model definitions after an edit and keep the builder coherent
export async function refreshModels() {
  state.models = await api("/api/models");
  const cur = state.model && modelByName(state.model.name);
  if (cur) {
    state.model = cur;
    // prune selections that no longer exist in the edited model
    state.dims = state.dims.filter((d) => dimByName(d.name));
    state.measures = state.measures.filter((m) => measureByName(m));
    state.filters = state.filters.filter((f) => dimByName(f.field));
    syncBuilderUI();
    scheduleRun();
  } else if (state.models.length) {
    selectModel(state.models[0].name);
  }
}
