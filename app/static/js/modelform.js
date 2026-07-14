/* Guided model form: the default way to create/edit a fact model. A stepper
   walks the author through source dataset → joined datasets → common-model
   imports → dimensions & measures, holding a structured spec that the server
   renders to YAML (POST /api/models/generate) for review and save. Raw YAML
   editing stays one click away (editor.js) — the form is the guided front
   door, the text editor the escape hatch. Form state is ephemeral: nothing
   persists until SAVE writes the generated yaml (Constitution V). */
"use strict";

import { refreshModels } from "./builder.js";
import { dslContext, dslItems, makeCompleter } from "./completion.js";
import { openEditor } from "./editor.js";
import {
  colsOf, datasetCards, loadDatasets, manualPathRow, NAME_RE, note, pairRow,
  sourceSchema, textField, titleCase,
} from "./formkit.js";
import { $, api, el } from "./lib.js";
import { hooks, showView, state } from "./state.js";

const STEPS = ["SOURCE", "JOINS", "COMMON MODELS", "DIMENSIONS & MEASURES", "REVIEW & SAVE"];
const AGGS = { sum: "sum", mean: "mean", min: "min", max: "max", count_distinct: "count_distinct" };

const form = {
  editingName: null,   // name of the existing model being edited (null = new)
  step: 0,
  dirty: false,
  name: "", label: "", description: "",
  source: null,        // {path, format}
  joins: [],           // {name, path, format, how, pairs:[{left,right}]}
  imports: [],         // {bundle, anchor, datasets:null|[names], pairs:[{left,right}]}
  dimensions: [],      // spec dimension dicts (column/type/label/spine/geo preserved)
  measures: [],        // {name, label, expr, format, description}
};
let generated = null;       // last /api/models/generate response

const setStatus = (html) => { $("#mf-status").innerHTML = html; };

// model-side columns offered as the LEFT half of a relationship: the source's
// own columns plus everything the declared joins pull in
function modelColumns() {
  const cols = [...(colsOf(form.source) || [])];
  for (const j of form.joins) for (const c of colsOf(j) || []) {
    if (!cols.some((x) => x.name === c.name)) cols.push(c);
  }
  return cols;
}

function toSpec() {
  const pairsOf = (rows) => {
    const done = rows.filter((p) => p.left && p.right);
    return { left_on: done.map((p) => p.left), right_on: done.map((p) => p.right) };
  };
  return {
    name: form.name.trim(), label: form.label.trim(), description: form.description.trim(),
    source: form.source || { path: "", format: "parquet" },
    joins: form.joins.map((j) => ({ name: j.name, path: j.path, format: j.format, how: j.how, ...pairsOf(j.pairs) })),
    dimension_imports: form.imports.map((i) => ({
      bundle: i.bundle, anchor_dataset: i.anchor, datasets: i.datasets, ...pairsOf(i.pairs),
    })),
    dimensions: form.dimensions,
    measures: form.measures
      .filter((m) => m.name.trim() && m.expr.trim())
      .map((m) => ({
        name: m.name, label: m.label, expr: m.expr, format: m.format, description: m.description,
        ...(hasFrame(m) ? { frame: m.frame, frame_emits: m.frame_emits || [] } : {}),
        // no dedicated UI for this yet (author it via the raw yaml editor) —
        // carried through untouched so opening+saving a model via the form
        // never silently strips synonyms, same regression class this form
        // already had for frame/frame_emits before they got a proper row
        ...(m.synonyms && m.synonyms.length ? { synonyms: m.synonyms } : {}),
      })),
  };
}

export function confirmLeaveModelForm() {
  if (state.view !== "modelform" || !form.dirty) return true;
  return confirm("Leave the model form? In-progress edits are not saved.");
}

export async function openModelForm(name) {
  if (!confirmLeaveModelForm()) return;
  Object.assign(form, {
    editingName: name, step: 0, dirty: false,
    name: "", label: "", description: "", source: null, joins: [], imports: [],
    dimensions: [],
    measures: [{ name: "rows", label: "Row Count", expr: "count()", format: "number", description: "" }],
  });
  generated = null;
  showView("modelform");
  $("#mf-title").textContent = name ? `edit model · ${name}` : "new model";
  setStatus(name ? "loading…" : "");
  render();
  if (!state.bundles.length) state.bundles = await api("/api/dimensions").catch(() => []);
  await loadDatasets();
  if (name) {
    const { spec } = await api(`/api/models/${name}/spec`);
    Object.assign(form, {
      name: spec.name, label: spec.label, description: spec.description,
      source: spec.source,
      joins: spec.joins.map((j) => ({ ...j, pairs: toPairs(j) })),
      imports: spec.dimension_imports.map((i) => ({
        bundle: i.bundle, anchor: i.anchor_dataset, datasets: i.datasets, pairs: toPairs(i),
      })),
      dimensions: spec.dimensions, measures: spec.measures,
    });
    setStatus("");
    if (form.source) await sourceSchema(form.source.path, form.source.format);
    await Promise.all(form.joins.map((j) => sourceSchema(j.path, j.format)));
    await Promise.all(form.imports.map((i) => anchorSchema(i)));
  }
  render();
}

const toPairs = (j) => j.left_on.map((l, idx) => ({ left: l, right: j.right_on[idx] ?? l }));

const bundleDataset = (bundleName, dsName) =>
  (state.bundles.find((b) => b.name === bundleName)?.datasets || []).find((d) => d.name === dsName);

const anchorSchema = (imp) => {
  const ds = bundleDataset(imp.bundle, imp.anchor);
  return ds ? sourceSchema(ds.path, ds.format) : Promise.resolve(null);
};

const markDirty = () => { form.dirty = true; };

// ── rendering ──────────────────────────────────────────────────────────────

function render() {
  renderRail();
  const main = $("#mf-main");
  main.innerHTML = "";
  [renderSource, renderJoins, renderImports, renderShape, renderReview][form.step](main);
  const err = stepError();
  $("#mf-hint").textContent = err || "";
  $("#mf-prev").disabled = form.step === 0;
  $("#mf-next").hidden = form.step === STEPS.length - 1;
  $("#mf-next").disabled = !!err;
  $("#mf-save").hidden = form.step !== STEPS.length - 1;
  $("#mf-save").disabled = true;   // review re-enables once generate says ok
}

function renderRail() {
  const rail = $("#mf-steps");
  rail.innerHTML = "";
  STEPS.forEach((label, idx) => {
    const btn = el("button", { class: "mf-step" + (idx === form.step ? " on" : "") + (idx < form.step ? " done" : "") },
      el("span", { class: "num" }, String(idx + 1)), label);
    btn.addEventListener("click", () => { if (idx <= form.step || !stepError()) { form.step = idx; render(); } });
    rail.append(btn);
  });
}

function stepError() {
  if (form.step === 0) {
    if (!NAME_RE.test(form.name.trim())) return "model name must be snake_case (a-z, 0-9, _)";
    if (!form.source) return "pick a dataset (or enter a path) for the model's source";
  }
  if (form.step === 1) {
    for (const j of form.joins) {
      if (!NAME_RE.test(j.name)) return "every join needs a snake_case name";
      if (!j.pairs.some((p) => p.left && p.right)) return `join '${j.name}': relate at least one column pair`;
    }
  }
  if (form.step === 2) {
    for (const i of form.imports) {
      if (!i.pairs.some((p) => p.left && p.right)) return `import '${i.bundle}': relate at least one column pair`;
    }
  }
  return null;
}

// textField wrapper: every keystroke also refreshes the dirty flag + step gate
const field = (label, value, set, ph) => textField(label, value, (v) => {
  set(v);
  markDirty();
  $("#mf-hint").textContent = stepError() || "";
  $("#mf-next").disabled = !!stepError();
}, ph);

const PAIR_PH = { leftPh: "this model's column", rightPh: "their column" };
const modelPair = (pair, leftCols, rightCols, onremove) =>
  pairRow(pair, leftCols, rightCols, {
    ...PAIR_PH,
    onchange: () => { markDirty(); render(); },
    oninput: markDirty,
    onremove,
  });

// ── step 1: SOURCE ──

function renderSource(main) {
  main.append(el("div", { class: "sec-title" }, "1 · Name"));
  main.append(el("div", { class: "mf-row3" },
    field("NAME (snake_case)", form.name, (v) => { form.name = v; }, "my_model"),
    field("LABEL", form.label, (v) => { form.label = v; }, "My Model"),
    field("DESCRIPTION", form.description, (v) => { form.description = v; }, "What this model covers.")));

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "2 · Source dataset"));
  main.append(note("the glob / dataset this model scans — pick one from the bucket:"));
  main.append(datasetCards(async (ds) => {
    form.source = { path: ds.path, format: ds.format };
    markDirty();
    render();
    await sourceSchema(ds.path, ds.format);
    render();
  }, form.source));

  main.append(manualPathRow(form.source, async (src) => {
    form.source = src;
    markDirty();
    await sourceSchema(src.path, src.format);
    render();
  }));

  if (form.source) {
    const cols = colsOf(form.source);
    main.append(el("div", { class: "mf-picked" },
      el("span", { class: "ok" }, "✓ source"), ` ${form.source.path} (${form.source.format})`,
      el("span", { class: "mf-colcount" }, cols ? ` · ${cols.length} columns` : " · columns not readable yet")));
  }
}

// ── step 2: JOINS (extra datasets related to the source) ──

function renderJoins(main) {
  main.append(el("div", { class: "sec-title" }, "Joined datasets"));
  main.append(note("optional lookup tables joined into the scan — their columns become usable in dimensions "
    + "and measures. Relate each one to the model by column pairs; the two sides don't need the same name."));
  form.joins.forEach((j, idx) => {
    const card = el("div", { class: "mf-card" });
    const rm = el("button", { class: "rm", title: "remove join" }, "✕");
    rm.addEventListener("click", () => { form.joins.splice(idx, 1); markDirty(); render(); });
    const nameIn = el("input", { value: j.name, spellcheck: "false", class: "mf-join-name" });
    nameIn.addEventListener("input", () => { j.name = nameIn.value; markDirty(); });
    const how = el("select", {}, ...["left", "inner"].map((h) => el("option", { value: h }, h + " join")));
    how.value = j.how;
    how.addEventListener("change", () => { j.how = how.value; markDirty(); });
    card.append(el("div", { class: "mf-card-head" }, nameIn, el("span", { class: "fmt" }, j.format), how, rm),
      el("div", { class: "path" }, j.path),
      el("div", { class: "field-label", style: "margin-top:8px" }, "RELATIONSHIP · this model ⇄ " + j.name));
    j.pairs.forEach((p, pi) => card.append(modelPair(p, colsOf(form.source), colsOf(j),
      () => { j.pairs.splice(pi, 1); markDirty(); render(); })));
    const addPair = el("button", { class: "ghost" }, "+ relate another column pair");
    addPair.addEventListener("click", () => { j.pairs.push({ left: "", right: "" }); markDirty(); render(); });
    card.append(addPair);
    main.append(card);
  });

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "Add a joined dataset"));
  main.append(datasetCards(async (ds) => {
    const base = (ds.key.split("/").pop() || "lookup").replace(/[^a-z0-9_]+/gi, "_").toLowerCase() || "lookup";
    form.joins.push({ name: base, path: ds.path, format: ds.format, how: "left", pairs: [{ left: "", right: "" }] });
    markDirty();
    render();
    await sourceSchema(ds.path, ds.format);
    render();
  }, null));
}

// ── step 3: COMMON MODELS (dimension_imports) ──

function renderImports(main) {
  main.append(el("div", { class: "sec-title" }, "Common models"));
  main.append(note("import shared dimensions declared once in a common model. Anchor the import on one of its "
    + "datasets and relate it to this model by column pairs — matching names not required."));
  if (!state.bundles.length) {
    main.append(note("none yet — create a common model from the Modelling workspace first"));
    return;
  }
  for (const b of state.bundles) {
    const imp = form.imports.find((i) => i.bundle === b.name);
    const card = el("div", { class: "mf-card" + (imp ? " on" : "") });
    const toggle = el("button", { class: "btn " + (imp ? "" : "plain") }, imp ? "✓ IMPORTED" : "IMPORT");
    toggle.addEventListener("click", async () => {
      if (imp) form.imports = form.imports.filter((i) => i !== imp);
      else {
        const anchor = b.datasets[0];
        const next = { bundle: b.name, anchor: anchor.name, datasets: null, pairs: [guessPair(anchor)] };
        form.imports.push(next);
        markDirty();
        render();
        await anchorSchema(next);
      }
      markDirty();
      render();
    });
    card.append(el("div", { class: "mf-card-head" },
      el("span", { class: "nm" }, b.label),
      el("span", { class: "mf-colcount" }, b.datasets.map((d) => d.name).join(", ")), toggle));
    if (imp) card.append(...importControls(b, imp));
    main.append(card);
  }
}

// default relationship guess for a freshly-imported bundle: its anchor's first
// declared dimension, mirrored on the model side when a column name matches
function guessPair(anchorDs) {
  const right = anchorDs.dimensions[0] || "";
  const left = modelColumns().some((c) => c.name === right) ? right : "";
  return { left, right };
}

function importControls(b, imp) {
  const out = [];
  const anchorSel = el("select", {}, ...b.datasets.map((d) => el("option", { value: d.name }, d.name)));
  anchorSel.value = imp.anchor;
  anchorSel.addEventListener("change", async () => {
    imp.anchor = anchorSel.value;
    imp.pairs = [guessPair(bundleDataset(b.name, imp.anchor))];
    markDirty();
    await anchorSchema(imp);
    render();
  });
  out.push(el("div", { class: "mf-anchor-row" },
    el("span", { class: "field-label" }, "ANCHOR DATASET"), anchorSel,
    el("span", { class: "mf-colcount" }, "the dataset this model joins onto")));

  const anchorDs = bundleDataset(b.name, imp.anchor);
  out.push(el("div", { class: "field-label", style: "margin-top:8px" }, `RELATIONSHIP · this model ⇄ ${b.name}.${imp.anchor}`));
  imp.pairs.forEach((p, pi) => out.push(modelPair(p, modelColumns(), anchorDs && colsOf(anchorDs),
    () => { imp.pairs.splice(pi, 1); markDirty(); render(); })));
  const addPair = el("button", { class: "ghost" }, "+ relate another column pair");
  addPair.addEventListener("click", () => { imp.pairs.push({ left: "", right: "" }); markDirty(); render(); });
  out.push(addPair);

  if (b.datasets.length > 1) {
    const subset = el("div", { class: "mf-subset" }, el("span", { class: "field-label" }, "DATASETS"));
    for (const d of b.datasets) {
      const on = imp.datasets === null || imp.datasets.includes(d.name);
      const attrs = { class: "chip" + (on ? " on" : "") };
      if (d.name === imp.anchor) attrs.disabled = "";
      const chip = el("button", attrs,
        el("span", { class: "tick" }, on ? "✓" : ""), el("span", { class: "lbl" }, d.name));
      if (d.name !== imp.anchor) chip.addEventListener("click", () => {
        const all = b.datasets.map((x) => x.name);
        let names = imp.datasets === null ? [...all] : [...imp.datasets];
        names = on ? names.filter((n) => n !== d.name) : [...names, d.name];
        imp.datasets = names.length === all.length ? null : names;
        markDirty();
        render();
      });
      subset.append(chip);
    }
    out.push(subset);
  }
  return out;
}

// ── step 4: DIMENSIONS & MEASURES ──

async function renderShape(main) {
  main.append(el("div", { class: "sec-title" }, "Dimensions"));
  const dimBox = el("div", { class: "mf-dims" }, note("reading source columns…"));
  main.append(dimBox);

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "Measures"));
  main.append(note("safe DSL expressions (e.g. sum(revenue), mean(price)) — every measure must reduce to one "
    + "value per group. A measure can also be marked complex to add a derived-frame step (group-by/window logic "
    + "over multiple rows) ahead of its aggregation."));
  const measBox = el("div");
  main.append(measBox);
  renderMeasures(measBox);

  generated = await api("/api/models/generate", { method: "POST", body: toSpec() }).catch((e) => ({ ok: false, error: e.message }));
  if (form.step !== 3) return;   // author moved on while we were fetching
  renderDims(dimBox);
  renderQuickAdd(measBox);
}

// imported dimension names -> owning bundle (already available; not declared here)
function importedDimOwners() {
  const owners = {};
  for (const imp of form.imports) {
    const b = state.bundles.find((x) => x.name === imp.bundle);
    for (const d of b?.datasets || []) {
      if (imp.datasets && !imp.datasets.includes(d.name)) continue;
      for (const dim of d.dimensions) owners[dim] = imp.bundle;
    }
  }
  return owners;
}

function renderDims(box) {
  box.innerHTML = "";
  const cols = generated?.columns || modelColumns();
  if (generated && !generated.ok) box.append(el("div", { class: "mf-warn" }, "⚠ " + generated.error));
  else if (generated?.schema_error) box.append(el("div", { class: "mf-warn" }, "⚠ " + generated.schema_error));
  if (!cols.length) { box.append(note("no readable columns — set a reachable source first, or add dimensions via EDIT YAML")); return; }
  box.append(note("tick the columns this model exposes as dimensions, then set their type and label:"));
  const owners = importedDimOwners();
  const known = new Set(cols.map((c) => c.name));
  const rows = el("div", { class: "mf-dim-rows" });
  // declared dimensions whose column no longer appears in the scan stay editable
  const phantom = form.dimensions.filter((d) => !known.has(d.column || d.name));
  for (const c of [...cols.map((x) => ({ ...x })), ...phantom.map((d) => ({ name: d.column || d.name, dtype: "?" }))]) {
    const dim = form.dimensions.find((d) => (d.column || d.name) === c.name);
    if (owners[c.name] && !dim) {
      rows.append(el("div", { class: "mf-dim-row imported" },
        el("span", { class: "tick" }, "◈"), el("span", { class: "mf-dim-col" }, c.name),
        el("span", { class: "mf-colcount" }, `imported via '${owners[c.name]}' — already a dimension`)));
      continue;
    }
    const row = el("div", { class: "mf-dim-row" + (dim ? " on" : "") });
    const tick = el("button", { class: "chip" + (dim ? " on" : "") },
      el("span", { class: "tick" }, dim ? "✓" : ""), el("span", { class: "lbl" }, c.name),
      el("span", { class: "hint" }, c.dtype));
    tick.addEventListener("click", () => {
      if (dim) form.dimensions = form.dimensions.filter((d) => d !== dim);
      else form.dimensions.push({
        name: c.name, column: c.name, label: titleCase(c.name),
        type: /date|time/i.test(c.dtype) ? "time" : "categorical", description: "", spine: null, geo: null,
      });
      markDirty();
      renderDims(box);
    });
    row.append(tick);
    if (dim) {
      const label = el("input", { value: dim.label, placeholder: "Label", spellcheck: "false" });
      label.addEventListener("input", () => { dim.label = label.value; markDirty(); });
      const type = el("select", {}, ...["categorical", "time", "numeric"].map((t) => el("option", { value: t }, t)));
      type.value = dim.type;
      type.addEventListener("change", () => { dim.type = type.value; markDirty(); });
      row.append(label, type);
      if (dim.spine || dim.geo) row.append(el("span", { class: "mf-colcount" }, dim.spine ? "⧗ spine" : "◎ geo"));
    }
    rows.append(row);
  }
  box.append(rows);
}

const FRAME_TEMPLATE =
  `frame = (\n    lf.group_by(dims)\n    .agg(pl.col("...").sum())\n)`;

const blankMeasure = () => ({ name: "", label: "", expr: "", format: "number", description: "" });
const blankFramedMeasure = () =>
  ({ name: "", label: "", expr: "", format: "number", description: "", frame: FRAME_TEMPLATE, frame_emits: [] });

// the one place that decides whether a measure counts as "framed" — blank
// frame text (e.g. cleared by the author) reverts it to a plain measure
// rather than saving/rendering it as an empty, invisible frame
const hasFrame = (m) => !!(m.frame && m.frame.trim());

// combined completion pool for a plain measure's expr: source columns (from
// the last successful /models/generate) plus sibling measure names — a bare
// identifier is one or the other depending on whether the expr turns out to
// be a window measure (running_total()/lag()), which the client can't know
// until it parses, so both are offered
function exprColumns() {
  const cols = generated?.columns || modelColumns();
  const names = new Set(cols.map((c) => c.name));
  const measureNames = form.measures.map((m) => m.name).filter((n) => n && !names.has(n));
  return [...cols, ...measureNames.map((n) => ({ name: n, dtype: "measure" }))];
}

// ── live per-row validation (POST /api/measures/check) ──

const checkTimers = new WeakMap();
function scheduleCheck(m, statusEl) {
  clearTimeout(checkTimers.get(m));
  checkTimers.set(m, setTimeout(() => runCheck(m, statusEl), 400));
}

async function runCheck(m, statusEl) {
  const framed = hasFrame(m);
  const hasBody = framed ? true : m.expr.trim();
  if (!m.name.trim() || !hasBody) { statusEl.innerHTML = ""; return; }
  statusEl.innerHTML = '<span class="pending">checking…</span>';
  const body = {
    expr: m.expr || "",
    frame: framed ? m.frame : null,
    frame_emits: framed ? (m.frame_emits || []) : [],
    columns: (generated?.columns || modelColumns()).map((c) => c.name),
    measure_names: form.measures.map((x) => x.name).filter((n) => n && n !== m.name),
  };
  let res;
  try {
    res = await api("/api/measures/check", { method: "POST", body });
  } catch (err) {
    statusEl.innerHTML = `<span class="err">✗ ${err.message}</span>`;
    return;
  }
  statusEl.innerHTML = res.ok
    ? `<span class="ok">✓ ${res.window ? "valid — window measure" : "valid"}</span>`
    : `<span class="err">✗ ${res.error}</span>`;
}

// dimensions the model has declared so far, offered as frame_emits candidates
// (frame_emits names dimension(s) the frame recomputes itself — e.g. a
// per-entity milestone date — see clinical_ops_recruitment.yaml)
function frameEmitsPicker(m, box) {
  const wrap = el("div", { class: "mf-subset" });
  if (!form.dimensions.length) {
    wrap.append(note("declare a dimension above to offer it here, or type its name once the frame computes it"));
    return wrap;
  }
  for (const d of form.dimensions) {
    const on = (m.frame_emits || []).includes(d.name);
    const chip = el("button", { class: "chip" + (on ? " on" : "") },
      el("span", { class: "tick" }, on ? "✓" : ""), el("span", { class: "lbl" }, d.name));
    chip.addEventListener("click", () => {
      m.frame_emits = on ? (m.frame_emits || []).filter((x) => x !== d.name) : [...(m.frame_emits || []), d.name];
      markDirty();
      renderMeasures(box);
    });
    wrap.append(chip);
  }
  return wrap;
}

function measureCard(m, idx, box) {
  const isFramed = hasFrame(m);
  const card = el("div", { class: "mf-measure-card" + (isFramed ? " framed" : "") });
  const status = el("div", { class: "mf-measure-status" });

  const name = el("input", { value: m.name, placeholder: "measure_name", spellcheck: "false" });
  name.addEventListener("input", () => { m.name = name.value; markDirty(); scheduleCheck(m, status); });
  const label = el("input", { value: m.label, placeholder: "Label", spellcheck: "false" });
  label.addEventListener("input", () => { m.label = label.value; markDirty(); });
  const fmt = el("select", {}, ...["number", "currency", "percent"].map((f) => el("option", { value: f }, f)));
  fmt.value = m.format;
  fmt.addEventListener("change", () => { m.format = fmt.value; markDirty(); });
  const toggle = el("button", {
    class: "btn plain", title: isFramed
      ? "drop the frame step and go back to a plain expression"
      : "add a derived-frame step ahead of this measure's aggregation",
  }, isFramed ? "✕ FRAME" : "+ FRAME");
  toggle.addEventListener("click", () => {
    if (isFramed) { m.frame = null; m.frame_emits = []; } else { m.frame = FRAME_TEMPLATE; m.frame_emits = []; }
    markDirty();
    renderMeasures(box);
  });
  const rm = el("button", { class: "rm", title: "remove measure" }, "✕");
  rm.addEventListener("click", () => { form.measures.splice(idx, 1); markDirty(); renderMeasures(box); });

  const head = el("div", { class: "mf-measure" }, name, label, fmt,
    ...(isFramed ? [el("span", { class: "mf-badge" }, "⚡ COMPLEX")] : []), toggle, rm);
  card.append(head);

  if (isFramed) {
    card.append(note("frame: builds a derived LazyFrame ahead of the aggregation below — lf (filtered scan), "
      + "dims (the query's other grouping columns) and pl are in scope; assign the result to `frame`. This is the "
      + "same authenticated-only escape hatch as the measure lab's \"complex measure\" — plain expression measures "
      + "can't reach it."));
    const frameWrap = el("div", { class: "mf-expr-wrap" });
    const frameTa = el("textarea", { class: "mf-frame", rows: "7", spellcheck: "false" });
    frameTa.value = m.frame || "";
    const frameSuggest = el("div", { class: "mf-suggest" });
    frameSuggest.hidden = true;
    // only the col("...") trigger applies inside frame python (no bare-name/
    // function completion — that vocabulary belongs to the DSL, not this
    // eval-based escape hatch)
    const frameCompleter = makeCompleter(frameTa, frameSuggest, (upto, after, caret) => {
      const ctx = dslContext(upto, caret);
      return ctx && ctx.kind === "col" ? { items: dslItems(ctx, exprColumns(), after), start: ctx.start } : null;
    });
    frameTa.addEventListener("input", () => {
      m.frame = frameTa.value; markDirty(); frameCompleter.update(); scheduleCheck(m, status);
    });
    frameTa.addEventListener("keydown", (e) => frameCompleter.onKeydown(e));
    frameTa.addEventListener("blur", () => setTimeout(() => frameCompleter.hide(), 150));
    frameWrap.append(frameTa, frameSuggest);
    card.append(frameWrap);

    card.append(el("div", { class: "field-label", style: "margin-top:8px" }, "FRAME_EMITS · dimensions the frame computes itself"));
    card.append(frameEmitsPicker(m, box));

    card.append(el("div", { class: "field-label", style: "margin-top:8px" }, "EXPR · aggregates the frame's own output columns"));
    const expr = el("input", { class: "mf-expr", value: m.expr, placeholder: 'pl.col("...").median()', spellcheck: "false" });
    expr.addEventListener("input", () => { m.expr = expr.value; markDirty(); scheduleCheck(m, status); });
    card.append(expr);
  } else {
    const exprWrap = el("div", { class: "mf-expr-wrap" });
    const expr = el("input", { class: "mf-expr", value: m.expr, placeholder: "mean(unit_price)", spellcheck: "false" });
    const suggest = el("div", { class: "mf-suggest" });
    suggest.hidden = true;
    const completer = makeCompleter(expr, suggest, (upto, after, caret) => {
      const ctx = dslContext(upto, caret);
      return ctx ? { items: dslItems(ctx, exprColumns(), after), start: ctx.start } : null;
    }, () => scheduleCheck(m, status));
    expr.addEventListener("input", () => { m.expr = expr.value; markDirty(); completer.update(); scheduleCheck(m, status); });
    expr.addEventListener("keydown", (e) => completer.onKeydown(e));
    expr.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
    exprWrap.append(expr, suggest);
    card.append(exprWrap);
  }

  card.append(status);
  scheduleCheck(m, status);
  return card;
}

function renderMeasures(box) {
  box.innerHTML = "";
  form.measures.forEach((m, idx) => box.append(measureCard(m, idx, box)));
  const add = el("button", { class: "ghost" }, "+ add measure");
  add.addEventListener("click", () => { form.measures.push(blankMeasure()); markDirty(); renderMeasures(box); });
  const addFramed = el("button", { class: "ghost" }, "+ add complex measure (frame)");
  addFramed.addEventListener("click", () => { form.measures.push(blankFramedMeasure()); markDirty(); renderMeasures(box); });
  box.append(el("div", { class: "mf-quick-slot" }), el("div", { class: "mf-measure-actions" }, add, addFramed));
}

function renderQuickAdd(measBox) {
  const slot = measBox.querySelector(".mf-quick-slot");
  const cols = (generated?.columns || []).filter((c) => /int|float|decimal/i.test(c.dtype));
  if (!slot || !cols.length) return;
  const colSel = el("select", {}, ...cols.map((c) => el("option", { value: c.name }, c.name)));
  const aggSel = el("select", {}, ...Object.keys(AGGS).map((a) => el("option", { value: a }, a)));
  const add = el("button", { class: "btn plain" }, "+ QUICK ADD");
  add.addEventListener("click", () => {
    const c = colSel.value, a = aggSel.value;
    form.measures.push({
      name: `${c}_${a}`, label: "", format: "number", description: "",
      expr: `${AGGS[a]}(${c})`,
    });
    markDirty();
    renderMeasures(measBox);
    renderQuickAdd(measBox);
  });
  slot.className = "mf-quick";
  slot.innerHTML = "";
  slot.append(el("span", { class: "field-label" }, "QUICK ADD"), colSel, aggSel, add);
}

// ── step 5: REVIEW & SAVE ──

async function renderReview(main) {
  main.append(el("div", { class: "sec-title" }, "Review"));
  const report = el("div", { class: "editor-report" }, "generating yaml…");
  const pre = el("pre", { class: "mf-yaml" }, "");
  main.append(report, note(form.editingName
    ? `saving rewrites models/${form.editingName}.yaml from this form (hand-written comments are not preserved)`
    : "saving writes a new file under models/ and hot-reloads the semantic layer"), pre);

  generated = await api("/api/models/generate", { method: "POST", body: toSpec() }).catch((e) => ({ ok: false, error: e.message }));
  if (form.step !== 4) return;
  pre.textContent = generated.yaml || "";
  if (generated.ok) {
    report.innerHTML = `<span class="ok">✓ valid</span> — <b>${generated.model.label}</b> (${generated.model.name}) · `
      + `${generated.model.dimensions} dimensions · ${generated.model.measures} measures`
      + (generated.schema_error ? `<br><span class="warn">⚠ ${generated.schema_error}</span>` : "");
  } else {
    report.innerHTML = `<span class="err">✗ ${generated.error}</span>`;
  }
  $("#mf-save").disabled = !generated.ok;
}

// ── save + wiring ──

async function saveModelForm() {
  if (!generated?.ok) return;
  setStatus("saving…");
  try {
    const saved = form.editingName
      ? await api(`/api/models/${form.editingName}/yaml`, { method: "PUT", body: { yaml: generated.yaml } })
      : await api("/api/models", { method: "POST", body: { yaml: generated.yaml } });
    form.dirty = false;
    await refreshModels();
    showView("modelling");
    if (hooks.loadModelling) hooks.loadModelling();
    setStatus(`<span class="ok">saved ${saved.file} ✓</span>`);
  } catch (err) {
    setStatus(`<span class="err">✗ ${err.message}</span>`);
  }
}

// hand the current form state to the raw YAML editor — the escape hatch for
// anything the form does not surface (spines, geo, exotic expressions)
async function editAsYaml() {
  setStatus("generating yaml…");
  const res = await api("/api/models/generate", { method: "POST", body: toSpec() }).catch(() => null);
  setStatus("");
  form.dirty = false;   // the yaml editor takes over ownership of the edits
  openEditor("model", form.editingName, { text: res?.yaml });
}

export function attachModelForm() {
  $("#mf-prev").addEventListener("click", () => { if (form.step > 0) { form.step -= 1; render(); } });
  $("#mf-next").addEventListener("click", () => {
    if (stepError()) return;
    form.step += 1;
    render();
  });
  $("#mf-save").addEventListener("click", saveModelForm);
  $("#mf-yaml").addEventListener("click", editAsYaml);
  $("#mf-back").addEventListener("click", () => {
    if (!confirmLeaveModelForm()) return;
    form.dirty = false;
    showView("modelling");
    if (hooks.loadModelling) hooks.loadModelling();
  });
}
