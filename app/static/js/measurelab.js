/* Measure lab: author a measure directly on the visual.
   Type a SQL aggregate with completion (source columns + aggregate
   functions) — or describe it and let ASK AI write it — watch it resolve live
   in the chart, then keep it on the visual (saved with the visual's spec) or
   promote it to the model yaml.

   A measure that needs a step in between (a per-order total before it can be
   averaged, a per-customer first purchase) carries a `from:` block: the rows
   its aggregate reads instead of the raw scan. The lab edits that block too,
   since asking for one is exactly the kind of measure worth asking for. */
"use strict";

import {
  buildQuery, refreshModels, renderBuilderViz, renderFieldRail, renderQueryStrip,
  scheduleRun, syncSortOptions,
} from "./builder.js";
import { dslContext, dslItems, makeCompleter } from "./completion.js";
import { $, api, el, fmtMeasure } from "./lib.js";
import { askBar, askForMeasure, measureAiAvailable, measureOrThrow, outcomeNote } from "./measureai.js";
import { hooks, state } from "./state.js";

const lab = { open: false, editingName: null, schema: [], schemaModel: null, emits: [],
              description: "", synonyms: [], asks: [] };
let completer = null;
let fromCompleter = null;
let ai = null;

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

// param()'s one legal position is a LAG/LEAD offset — this is a client-side
// heuristic for UX only (disabling "save to model" early); the server's
// sqlgrammar.referenced_parameter_names guard is authoritative.
const PARAM_REF = /\bparam\s*\(/;

function refreshParamInsertOptions() {
  const sel = $("#lab-param-insert");
  sel.innerHTML = "";
  sel.append(el("option", { value: "" }, "+ param"));
  for (const p of state.parameters) sel.append(el("option", { value: p.name }, `${p.name} (${p.type || "int"})`));
}

function updateSaveModelGuard(def) {
  const referencesParam = PARAM_REF.test(def.expr);
  const btn = $("#lab-save-model");
  btn.disabled = referencesParam;
  btn.title = referencesParam
    ? "parameterized measures can only be saved to a visual, not to the shared model"
    : "";
}

export function openLab(def = null) {
  lab.open = true;
  lab.editingName = def ? def.name : null;
  $("#measure-lab").hidden = false;
  $("#lab-name").value = def ? def.name : "";
  $("#lab-label").value = (def && def.label) || "";
  $("#lab-format").value = (def && def.format) || "number";
  $("#lab-expr").value = (def && def.expr) || "";
  setStep((def && def.from) || "", (def && def.emits) || []);
  setMeta((def && def.description) || "", (def && def.synonyms) || []);
  $("#lab-ai").hidden = !measureAiAvailable();
  if (ai) { ai.setStatus(AI_HINT); ai.syncThinking(); }
  lab.asks = [];
  setStatus('type a SQL aggregate — e.g. <b>SUM(unit_price * quantity)</b>; '
    + 'a bare name offers columns, sibling measures and functions');
  loadSchema();
  refreshParamInsertOptions();
  updateSaveModelGuard(labDef());
  renderLabHistory(def ? def.name : null);
  $("#lab-expr").focus();
}
hooks.openLab = openLab;

export function closeLab(rerun = true) {
  lab.open = false;
  lab.editingName = null;
  lab.asks = [];
  $("#measure-lab").hidden = true;
  if (completer) completer.hide();
  if (fromCompleter) fromCompleter.hide();
  if (rerun) scheduleRun();   // drop any live preview from the chart
}
hooks.closeLab = closeLab;

// The step (`from:`) is part of the measure wherever it goes — the query API,
// the visual's saved spec and POST /models/{name}/measures all take the same
// {name,label,format,expr,from,emits} shape — so it is built here once, and
// omitted entirely when there is no block rather than saved as an empty one.
function labDef() {
  const def = {
    name: $("#lab-name").value.trim(),
    label: $("#lab-label").value.trim(),
    format: $("#lab-format").value,
    expr: $("#lab-expr").value.trim(),
  };
  const step = stepText();
  if (step) {
    def.from = step;
    def.emits = [...lab.emits];
  }
  // carried, not dropped: a description is what Chat reads to decide whether a
  // measure answers a question, and both survive the round trip to the model
  if (lab.description) def.description = lab.description;
  if (lab.synonyms.length) def.synonyms = [...lab.synonyms];
  return def;
}

// The prose a measure carries — written by ASK AI, or already on the measure
// being edited. Shown rather than held invisibly, since it is saved with it.
function setMeta(description, synonyms) {
  lab.description = description || "";
  lab.synonyms = [...(synonyms || [])];
  const box = $("#lab-meta");
  const bits = [];
  if (lab.description) bits.push(lab.description);
  if (lab.synonyms.length) bits.push(`also called: ${lab.synonyms.join(", ")}`);
  box.textContent = bits.join(" · ");
  box.hidden = !bits.length;
}

const stepText = () => ($("#lab-from-wrap").hidden ? "" : $("#lab-from").value.trim());

// What "+ ADD STEP" starts from — the same skeleton modelform.js seeds a
// complex measure with, so the two authoring surfaces teach one shape.
const FROM_TEMPLATE = "SELECT {dims}, ...\nFROM {model}\nGROUP BY {dims}, ...";

// Show/hide the step editor as one operation, so "has a from: block" is a
// single fact about the lab rather than three fields that can disagree.
function setStep(text, emits = []) {
  lab.emits = [...emits];
  $("#lab-from").value = text || "";
  $("#lab-from-wrap").hidden = !text;
  // the two are one control in two states: the step is either being edited
  // (with DROP STEP to leave) or offered (with ADD STEP to start one)
  $("#lab-add-step-row").hidden = !!text;
  renderEmits();
}

/* EMITS: the dimensions this block computes itself rather than inheriting
   from the raw rows — a per-entity milestone date, a cohort month. The engine
   withholds an emitted dimension from {dims} while the block runs and groups
   on the block's own output afterwards, which is what lets a timeline bucket
   what you derived. It is only ever a *declared* dimension's name (anything
   else is refused at save time), so it is picked here rather than typed.

   A generated timeline is left out: it is not a column of the fact table, so
   no block can produce one and emitting it can only fail. Offered as a select
   plus chips rather than the model form's chip-per-dimension, because the lab
   sees a resolved model — every imported bundle dimension included — and that
   is a longer list than one row can hold. */
const emitCandidates = () => (state.model.dimensions || []).filter((d) => !d.spine);

function renderEmits() {
  const box = $("#lab-emits");
  box.replaceChildren(el("span", { class: "lab-hint emit-lead" },
    "EMITS · dimensions this block computes itself"));
  // what is emitted is drawn from lab.emits, not from the candidate list: a
  // measure written elsewhere may emit something this picker wouldn't offer,
  // and it must still be visible (and removable) rather than silently kept
  for (const name of lab.emits) {
    const chip = el("button", { class: "chip on", title: "stop emitting this dimension" },
      el("span", { class: "tick" }, "✓"), el("span", { class: "lbl" }, name));
    chip.addEventListener("click", () => toggleEmit(name));
    box.append(chip);
  }
  const rest = emitCandidates().filter((d) => !lab.emits.includes(d.name));
  if (!rest.length) {
    if (!lab.emits.length) box.append(el("span", { class: "lab-hint" }, "— this table declares none"));
    return;
  }
  const add = el("select", { class: "lab-emit-add",
                             title: "the block outputs this dimension itself" },
    el("option", { value: "" }, "+ emit"),
    ...rest.map((d) => el("option", { value: d.name }, d.name)));
  add.addEventListener("change", () => { if (add.value) toggleEmit(add.value); });
  box.append(add);
}

function toggleEmit(name) {
  lab.emits = lab.emits.includes(name)
    ? lab.emits.filter((n) => n !== name)
    : [...lab.emits, name];
  renderEmits();
  scheduleResolve();     // emitting changes the query the measure has to run
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
  updateSaveModelGuard(def);
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
  renderFieldRail();
  renderQueryStrip();
  syncSortOptions();
  scheduleRun();
}

// Saving a measure to the model is an authoring action; identity comes from
// the signed-in session (spec 011) and the server records it in the
// measure's provenance history — no credentials are collected here anymore.
async function saveToModel() {
  const def = labDef();
  if (!def.name || !def.expr) return setStatus("needs a name and an expression", true);
  if (PARAM_REF.test(def.expr)) {
    return setStatus("✗ parameterized measures can only be saved to a visual", true);
  }
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

// Compact provenance strip: when the lab's name matches a saved model
// measure, show who wrote each version — pre-auth rows (no verified
// account) are flagged as legacy/self-declared.
async function renderLabHistory(name) {
  const box = $("#lab-history");
  box.innerHTML = "";
  box.hidden = true;
  if (!name || !state.model.measures.some((m) => m.name === name)) return;
  try {
    const rows = await api(`/api/models/${state.model.name}/measures/${name}/history`);
    if (!rows.length) return;
    box.hidden = false;
    box.append(el("span", { class: "lab-hint" }, "model history: "));
    for (const h of rows.slice(0, 4)) {
      const who = h.verified ? h.author : `${h.author} (legacy, self-declared)`;
      box.append(el("span", { class: "hist-entry" },
        `v${h.version} ${h.action} · ${who} · ${String(h.created_at).slice(0, 10)}`));
    }
  } catch { /* the strip is informational only */ }
}

// ── ASK AI (POST /api/measures/write/stream) ─────────────────

const AI_HINT = 'describe the measure — "average order value", "revenue from '
  + 'repeat customers only", "3-month moving average of revenue"';

/* The server builds the whole catalog it shows the model (columns, dimensions
   with real values, every existing formula) from the model name and the query
   below — the client sends what it alone knows: which visual this is, what it
   is currently showing, and which measure is being edited. What comes back has
   already been compiled and run server-side; it still lands in these fields
   for the author to read and change, and saving stays their click. */
async function askMeasure(text, ui) {
  const editing = lab.editingName
    ? state.inlineMeasures.find((m) => m.name === lab.editingName)
      || state.model.measures.find((m) => m.name === lab.editingName)
    : null;
  const payload = await askForMeasure({
    instruction: text,
    model: state.model.name,
    scope: "visual",
    query: buildQuery(),
    editing: editing || null,
    history: lab.asks,
    thinking: ui.thinking(),
  }, { onStatus: (msg) => ui.setStatus(msg) });

  const measure = measureOrThrow(payload);   // throws a decline/failure as-is
  $("#lab-name").value = measure.name || "";
  $("#lab-label").value = measure.label || "";
  $("#lab-format").value = measure.format || "number";
  $("#lab-expr").value = measure.expr || "";
  setStep(measure.from || "", measure.emits || []);
  setMeta(measure.description || "", measure.synonyms || []);
  lab.asks = [...lab.asks, { instruction: text, summary: measure.name }].slice(-6);
  ui.clear();
  ui.setStatus(outcomeNote(payload) || AI_HINT);
  updateSaveModelGuard(labDef());
  renderLabHistory(measure.name);
  tryResolve();       // and show it in the chart, the same as a typed one
}

// ── completion (shared engine, SQL-expression context) ────

// combined completion pool for a bare identifier: source columns plus
// sibling measure names (model measures + this visual's other inline
// measures) — a bare name is one or the other depending on whether the
// expr turns out to be a window measure (something OVER w), which isn't
// known until it's parsed, so both are offered together (mirrors
// modelform.js's exprColumns()). The measure currently being edited is
// excluded so it's never suggested as its own sibling.
function exprPool() {
  const names = new Set(lab.schema.map((c) => c.name));
  const measureNames = [
    ...state.model.measures.map((m) => m.name),
    ...state.inlineMeasures.map((m) => m.name),
  ].filter((n, i, arr) => n && n !== lab.editingName && !names.has(n) && arr.indexOf(n) === i);
  return [...lab.schema, ...measureNames.map((n) => ({ name: n, dtype: "measure" }))];
}

// resolve the measure-lab textarea against source columns, sibling
// measures, and this visual's declared parameters (for param('name'))
function labResolve(upto, after, caret) {
  const ctx = dslContext(upto, caret);
  if (!ctx) return null;
  return { items: dslItems(ctx, exprPool(), after, state.parameters), start: ctx.start };
}

// ── wiring ───────────────────────────────────────────────────

export function initMeasureLab() {
  $("#lab-open").addEventListener("click", () => openLab());
  $("#lab-cancel").addEventListener("click", () => closeLab());
  ai = askBar({ placeholder: "what should this measure compute?", onAsk: askMeasure, hint: AI_HINT });
  $("#lab-ai").append(ai.bar);
  $("#lab-drop-step").addEventListener("click", () => {
    setStep("", []);          // back to a plain aggregate over the fact scan
    scheduleResolve();
  });
  $("#lab-add-step").addEventListener("click", () => {
    // a hand-written complex measure, rather than only one ASK AI wrote:
    // the skeleton, the emits picker and the completer, then the caret in it
    setStep(FROM_TEMPLATE, []);
    $("#lab-from").focus();
  });
  const from = $("#lab-from");
  fromCompleter = makeCompleter(from, $("#lab-from-suggest"), labResolve, scheduleResolve);
  from.addEventListener("input", () => { fromCompleter.update(); scheduleResolve(); });
  from.addEventListener("keydown", (e) => fromCompleter.onKeydown(e));
  from.addEventListener("blur", () => setTimeout(() => fromCompleter.hide(), 150));
  $("#lab-save-visual").addEventListener("click", saveToVisual);
  $("#lab-save-model").addEventListener("click", saveToModel);
  $("#lab-param-insert").addEventListener("change", (e) => {
    const name = e.target.value;
    e.target.value = "";
    if (!name) return;
    const box = $("#lab-expr");
    const insert = `param('${name}')`;
    const start = box.selectionStart ?? box.value.length;
    const end = box.selectionEnd ?? box.value.length;
    box.value = box.value.slice(0, start) + insert + box.value.slice(end);
    box.focus();
    box.setSelectionRange(start + insert.length, start + insert.length);
    scheduleResolve();
  });
  const expr = $("#lab-expr");
  completer = makeCompleter(expr, $("#lab-suggest"), labResolve, scheduleResolve);
  expr.addEventListener("input", () => { completer.update(); scheduleResolve(); });
  expr.addEventListener("keydown", (e) => completer.onKeydown(e));
  expr.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
  for (const id of ["lab-name", "lab-label", "lab-format"]) {
    $("#" + id).addEventListener("input", scheduleResolve);
  }
}
