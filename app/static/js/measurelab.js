/* Measure lab: author a measure directly on the visual.
   Type polars expression syntax with completion (source columns + expression
   methods), watch it resolve live in the chart, then keep it on the visual
   (saved with the visual's spec) or promote it to the model yaml. */
"use strict";

import { buildQuery, refreshModels, renderBuilderViz, renderMeasures, scheduleRun, syncSortOptions } from "./builder.js";
import { makeCompleter, polarsContext, polarsItems } from "./completion.js";
import { $, api, el, fmtMeasure } from "./lib.js";
import { hooks, state } from "./state.js";

const lab = { open: false, editingName: null, schema: [], schemaModel: null };
let completer = null;

function setStatus(html, isError = false) {
  const box = $("#lab-status");
  box.innerHTML = html;
  box.className = isError ? "err" : "";
}

async function loadSchema() {
  if (lab.schemaModel === state.model.name && lab.schema.length) return;
  lab.schema = [];
  lab.schemaModel = state.model.name;
  try {
    lab.schema = (await api(`/api/models/${state.model.name}/schema`)).columns;
  } catch { /* completion just won't offer columns */ }
}

export function openLab(def = null) {
  lab.open = true;
  lab.editingName = def ? def.name : null;
  $("#measure-lab").hidden = false;
  $("#lab-name").value = def ? def.name : "";
  $("#lab-label").value = (def && def.label) || "";
  $("#lab-format").value = (def && def.format) || "number";
  $("#lab-expr").value = (def && def.expr) || "";
  setStatus('type an expression — <b>pl.</b>, <b>.</b> and <b>pl.col("</b> trigger suggestions');
  loadSchema();
  $("#lab-expr").focus();
}
hooks.openLab = openLab;

export function closeLab(rerun = true) {
  lab.open = false;
  lab.editingName = null;
  $("#measure-lab").hidden = true;
  if (completer) completer.hide();
  if (rerun) scheduleRun();   // drop any live preview from the chart
}
hooks.closeLab = closeLab;

function labDef() {
  return {
    name: $("#lab-name").value.trim(),
    label: $("#lab-label").value.trim(),
    format: $("#lab-format").value,
    expr: $("#lab-expr").value.trim(),
  };
}

function nameProblem(def) {
  if (!/^[a-z_][a-z0-9_]*$/.test(def.name)) return "name must be snake_case";
  const taken =
    state.model.measures.some((m) => m.name === def.name) ||
    state.model.dimensions.some((d) => d.name === def.name) ||
    state.inlineMeasures.some((m) => m.name === def.name && m.name !== lab.editingName);
  return taken ? `'${def.name}' is already taken` : null;
}

// working query: the builder's current query + this draft measure
function draftQuery(def) {
  const q = buildQuery();
  q.inline_measures = [
    ...state.inlineMeasures.filter((m) => m.name !== lab.editingName && m.name !== def.name),
    def,
  ];
  if (!q.measures.includes(def.name)) q.measures = [...q.measures, def.name];
  q.measures = q.measures.filter((m) => m !== lab.editingName || m === def.name);
  return q;
}

let resolveTimer = null;
export function scheduleResolve() {
  clearTimeout(resolveTimer);
  resolveTimer = setTimeout(tryResolve, 450);
}

async function tryResolve() {
  if (!lab.open) return;
  const def = labDef();
  if (!def.name && !def.expr) return;
  if (!def.name) return setStatus("give the measure a snake_case name", true);
  if (!def.expr) return setStatus("…waiting for an expression");
  const problem = nameProblem(def);
  if (problem) return setStatus("✗ " + problem, true);
  setStatus("resolving…");
  try {
    const result = await api("/api/query", { method: "POST", body: draftQuery(def) });
    state.result = result;
    renderBuilderViz();   // the draft measure renders live in the chart
    let peek = "";
    if (result.rows.length === 1 && !state.dims.length) {
      peek = ` · <b>${fmtMeasure(result.rows[0][def.name], def.format)}</b>`;
    }
    setStatus(`<span class="ok">✓ resolves</span> · ${result.elapsed_ms}ms${peek}`);
  } catch (err) {
    setStatus("✗ " + err.message, true);
  }
}

async function saveToVisual() {
  const def = labDef();
  if (!def.name || !def.expr) return setStatus("needs a name and an expression", true);
  const problem = nameProblem(def);
  if (problem) return setStatus("✗ " + problem, true);
  state.inlineMeasures = state.inlineMeasures.filter((m) => m.name !== lab.editingName && m.name !== def.name);
  state.inlineMeasures.push(def);
  if (lab.editingName && lab.editingName !== def.name) {
    state.measures = state.measures.map((m) => (m === lab.editingName ? def.name : m));
  }
  if (!state.measures.includes(def.name)) state.measures.push(def.name);
  closeLab(false);
  renderMeasures();
  syncSortOptions();
  scheduleRun();
}

async function saveToModel() {
  const def = labDef();
  if (!def.name || !def.expr) return setStatus("needs a name and an expression", true);
  setStatus("saving to model…");
  try {
    await api(`/api/models/${state.model.name}/measures`, { method: "POST", body: def });
    // promoted: no longer visual-scoped
    state.inlineMeasures = state.inlineMeasures.filter((m) => m.name !== lab.editingName && m.name !== def.name);
    if (lab.editingName && lab.editingName !== def.name) {
      state.measures = state.measures.map((m) => (m === lab.editingName ? def.name : m));
    }
    if (!state.measures.includes(def.name)) state.measures.push(def.name);
    await refreshModels();   // measure now appears as a regular model measure
    closeLab(false);
  } catch (err) {
    setStatus("✗ " + err.message, true);
  }
}

// ── completion (shared engine, polars-expression context) ────

// resolve the measure-lab textarea against the model's source columns
function labResolve(upto, after, caret) {
  const ctx = polarsContext(upto, caret);
  if (!ctx) return null;
  return { items: polarsItems(ctx, lab.schema, after), start: ctx.start };
}

// ── wiring ───────────────────────────────────────────────────

export function initMeasureLab() {
  $("#lab-open").addEventListener("click", () => openLab());
  $("#lab-cancel").addEventListener("click", () => closeLab());
  $("#lab-save-visual").addEventListener("click", saveToVisual);
  $("#lab-save-model").addEventListener("click", saveToModel);
  const expr = $("#lab-expr");
  completer = makeCompleter(expr, $("#lab-suggest"), labResolve, scheduleResolve);
  expr.addEventListener("input", () => { completer.update(); scheduleResolve(); });
  expr.addEventListener("keydown", (e) => completer.onKeydown(e));
  expr.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
  for (const id of ["lab-name", "lab-label", "lab-format"]) {
    $("#" + id).addEventListener("input", scheduleResolve);
  }
}
