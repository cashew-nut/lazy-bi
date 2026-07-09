/* The query builder: sidebar controls, query execution, saved visuals. */
"use strict";

import { decideChart, renderViz, vizMessage } from "./charts/index.js";
import { GRAINS, fetchDimValues } from "./charts/common.js";
import { renderTableInto } from "./charts/table.js";
import { FILTER_OPS, filterReady, toApiFilter } from "./filters.js";
import { $, api, el } from "./lib.js";
import { hooks, modelByName, showView, state, valueCache } from "./state.js";

export const dimByName = (name) => state.model.dimensions.find((d) => d.name === name);
export const measureByName = (name) =>
  state.model.measures.find((m) => m.name === name)
  || state.inlineMeasures.find((m) => m.name === name);

function builderCtx() {
  return {
    model: state.model,
    dims: state.dims,
    chartType: state.chartType,
    result: state.result,
    container: $("#chart"),
    legendBox: $("#legend"),
    rerender: renderBuilderViz,
  };
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
    setMeta(`${result.row_count} rows · <span class="ms">${result.elapsed_ms}ms</span> · lazy scan <span class="path">${state.model.path}</span>`);
    renderBuilderViz();
  } catch (err) {
    if (token !== state.queryToken) return;
    state.result = null;
    setMeta("");
    $("#legend").innerHTML = "";
    vizMessage($("#chart"), "QUERY ERROR // " + err.message, true);
  }
}

function setMeta(html, busy = false) {
  $("#meta").innerHTML = html + (busy ? " <span style='color:var(--pink)'>▮▯▯</span>" : "");
}

export function renderBuilderViz() {
  const ctx = builderCtx();
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

// ── sidebar ──────────────────────────────────────────────────

function renderModelSelect() {
  const sel = $("#model-select");
  sel.innerHTML = "";
  for (const m of state.models) sel.append(el("option", { value: m.name }, m.label));
  sel.value = state.model.name;
  $("#model-desc").textContent = state.model.description;
}

export function renderDims() {
  const box = $("#dim-list");
  box.innerHTML = "";
  for (const dim of state.model.dimensions) {
    const active = state.dims.find((d) => d.name === dim.name);
    const chip = el("div", { class: "chip" + (active ? " on" : "") },
      el("span", { class: "tick" }, active ? "◈" : "◇"),
      el("span", { class: "lbl" }, dim.label),
      el("span", { class: "hint" }, dim.spine ? "spine" : dim.type === "time" ? "time" : ""));
    if (active && dim.type === "time") {
      const grainSel = el("select", { class: "grain", onchange: (e) => { active.grain = e.target.value; syncSortOptions(); scheduleRun(); } });
      for (const [g, label] of Object.entries(GRAINS)) grainSel.append(el("option", { value: g }, label));
      grainSel.value = active.grain || "1mo";
      grainSel.addEventListener("click", (e) => e.stopPropagation());
      chip.append(grainSel);
    }
    chip.addEventListener("click", () => {
      if (active) state.dims = state.dims.filter((d) => d.name !== dim.name);
      else state.dims.push(dim.type === "time" ? { name: dim.name, grain: "1mo" } : { name: dim.name });
      renderDims(); syncSortOptions(); scheduleRun();
    });
    box.append(chip);
  }
}

export function renderMeasures() {
  const box = $("#measure-list");
  box.innerHTML = "";
  const toggle = (name) => {
    if (state.measures.includes(name)) state.measures = state.measures.filter((m) => m !== name);
    else state.measures.push(name);
    renderMeasures(); syncSortOptions(); scheduleRun();
  };
  for (const mea of state.model.measures) {
    const active = state.measures.includes(mea.name);
    const chip = el("div", { class: "chip measure" + (active ? " on" : ""), title: mea.expr },
      el("span", { class: "tick" }, active ? "◆" : "◇"),
      el("span", { class: "lbl" }, mea.label),
      el("span", { class: "hint" }, mea.format === "number" ? "" : mea.format));
    chip.addEventListener("click", () => toggle(mea.name));
    box.append(chip);
  }
  // visual-scoped measures from the lab — saved with the visual, not the model
  for (const mea of state.inlineMeasures) {
    const active = state.measures.includes(mea.name);
    const edit = el("button", { class: "mini", title: "edit in the measure lab" }, "✎");
    edit.addEventListener("click", (e) => { e.stopPropagation(); hooks.openLab(mea); });
    const rm = el("button", { class: "mini rm", title: "remove from this visual" }, "✕");
    rm.addEventListener("click", (e) => {
      e.stopPropagation();
      state.inlineMeasures = state.inlineMeasures.filter((m) => m.name !== mea.name);
      state.measures = state.measures.filter((m) => m !== mea.name);
      renderMeasures(); syncSortOptions(); scheduleRun();
    });
    const chip = el("div", { class: "chip measure inline" + (active ? " on" : ""), title: mea.expr },
      el("span", { class: "tick" }, active ? "◆" : "◇"),
      el("span", { class: "lbl" }, mea.label || mea.name),
      el("span", { class: "hint" }, "visual"),
      edit, rm);
    chip.addEventListener("click", () => toggle(mea.name));
    box.append(chip);
  }
}

export function renderFilters() {
  const box = $("#filter-list");
  box.innerHTML = "";
  state.filters.forEach((flt, idx) => {
    const row = el("div", { class: "filter-row" });
    const dimSel = el("select", { onchange: (e) => { flt.field = e.target.value; flt.value = ""; flt.values = []; renderFilters(); scheduleRun(); } });
    for (const d of state.model.dimensions) dimSel.append(el("option", { value: d.name }, d.label));
    dimSel.value = flt.field;
    const opSel = el("select", { class: "op", onchange: (e) => { flt.op = e.target.value; flt.value = ""; flt.values = []; renderFilters(); scheduleRun(); } });
    for (const [op, label] of FILTER_OPS) opSel.append(el("option", { value: op }, label));
    opSel.value = flt.op;
    const rm = el("button", { class: "rm", onclick: () => { state.filters.splice(idx, 1); renderFilters(); scheduleRun(); } }, "✕");
    row.append(el("div", { class: "top" }, dimSel, opSel, rm));

    const dim = dimByName(flt.field);
    const multi = flt.op === "in" || flt.op === "not_in";
    const distinct = valueCache[state.model.name + ":" + flt.field];
    const haveList = Array.isArray(distinct) && distinct.length;
    const onValues = () => { renderFilters(); };
    if (multi) {
      if (!haveList) fetchDimValues(state.model.name, flt.field, onValues);
      const sel = el("select", { multiple: "multiple", onchange: (e) => { flt.values = [...e.target.selectedOptions].map((o) => o.value); scheduleRun(); } });
      for (const v of (haveList ? distinct : flt.values)) {
        const opt = el("option", { value: String(v) }, String(v));
        if (flt.values.includes(String(v))) opt.selected = true;
        sel.append(opt);
      }
      row.append(sel);
    } else if ((flt.op === "eq" || flt.op === "ne") && dim && dim.type !== "time") {
      if (!haveList) fetchDimValues(state.model.name, flt.field, onValues);
      const sel = el("select", { onchange: (e) => { flt.value = e.target.value; scheduleRun(); } });
      sel.append(el("option", { value: "" }, "— pick —"));
      for (const v of (haveList ? distinct : [])) sel.append(el("option", { value: String(v) }, String(v)));
      sel.value = flt.value || "";
      row.append(sel);
    } else {
      const input = el("input", {
        type: dim && dim.type === "time" && flt.op !== "contains" ? "date" : "text",
        value: flt.value || "", placeholder: "value…",
        onchange: (e) => { flt.value = e.target.value; scheduleRun(); },
      });
      row.append(input);
    }
    box.append(row);
  });
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

export function renderChartSeg() {
  for (const btn of $("#chart-seg").querySelectorAll("button")) {
    btn.classList.toggle("on", btn.dataset.t === state.chartType);
  }
}

// ── saved visuals ────────────────────────────────────────────

export async function refreshSaved() {
  const visuals = await api("/api/visuals");
  const box = $("#saved-list");
  box.innerHTML = "";
  if (!visuals.length) { box.append(el("div", { class: "empty-note" }, "nothing saved yet — build a query and hit SAVE")); return; }
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
hooks.refreshSaved = refreshSaved;

export function currentSpec() {
  return { query: buildQuery(), chartType: state.chartType };
}

export async function saveVisual(asNew) {
  const name = $("#visual-name").value.trim() || "untitled_visual";
  const payload = { name, model: state.model.name, spec: currentSpec() };
  const saved = (!asNew && state.visualId)
    ? await api(`/api/visuals/${state.visualId}`, { method: "PUT", body: payload })
    : await api("/api/visuals", { method: "POST", body: payload });
  state.visualId = saved.id;
  state.visualName = saved.name;
  refreshSaved();
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
  state.filters = (q.filters || []).map((f) => ({ field: f.field, op: f.op, value: f.value ?? "", values: f.values || [] }));
  state.sort = q.sort ? { by: q.sort.by, desc: !!q.sort.desc } : { by: "", desc: true };
  state.limit = q.limit || 1000;
  state.chartType = v.spec.chartType || "auto";
  state.visualId = v.id;
  state.visualName = v.name;
  state.showTable = false;
  syncBuilderUI();
  refreshSaved();
  scheduleRun();
}

export function syncBuilderUI() {
  renderModelSelect();
  renderDims();
  renderMeasures();
  renderFilters();
  renderChartSeg();
  syncSortOptions();
  $("#sort-dir").value = state.sort.desc ? "desc" : "asc";
  $("#limit").value = state.limit;
  $("#visual-name").value = state.visualName;
  $("#toggle-table").classList.toggle("on", state.showTable);
}

export function selectModel(name) {
  state.model = modelByName(name);
  state.dims = [];
  state.measures = [];
  state.inlineMeasures = [];
  if (hooks.closeLab) hooks.closeLab(false);
  state.filters = [];
  state.sort = { by: "", desc: true };
  state.visualId = null;
  // sensible starting query: time dim at month grain (if any) + first measure
  const timeDim = state.model.dimensions.find((d) => d.type === "time");
  if (timeDim) state.dims.push({ name: timeDim.name, grain: "1mo" });
  if (state.model.measures.length) state.measures.push(state.model.measures[0].name);
  state.visualName = "";
  syncBuilderUI();
  scheduleRun();
}

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
