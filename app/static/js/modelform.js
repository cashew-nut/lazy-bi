/* Guided model form: the default way to create/edit a fact model. A stepper
   walks the author through source dataset → joined datasets → common-model
   imports → dimensions & measures, holding a structured spec that the server
   renders to YAML (POST /api/models/generate) for review and save. Raw YAML
   editing stays one click away (editor.js) — the form is the guided front
   door, the text editor the escape hatch. Form state is ephemeral: nothing
   persists until SAVE writes the generated yaml (Constitution V). */
"use strict";

import { refreshModels } from "./builder.js";
import { openEditor } from "./editor.js";
import { $, api, el, fmtBytes } from "./lib.js";
import { hooks, showView, state } from "./state.js";

const NAME_RE = /^[a-z_][a-z0-9_]*$/;
const STEPS = ["SOURCE", "JOINS", "COMMON MODELS", "DIMENSIONS & MEASURES", "REVIEW & SAVE"];
const AGGS = { sum: "sum()", mean: "mean()", min: "min()", max: "max()", n_unique: "n_unique()" };

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
let datasets = null;        // /api/datasets payload (fetched once per open)
let generated = null;       // last /api/models/generate response
const schemaCache = {};     // "format|path" -> [{name,dtype}] | null (unreachable)

const setStatus = (html) => { $("#mf-status").innerHTML = html; };

async function sourceSchema(path, format) {
  const key = `${format}|${path}`;
  if (key in schemaCache) return schemaCache[key];
  try {
    const res = await api(`/api/datasets/schema?path=${encodeURIComponent(path)}&format=${encodeURIComponent(format)}`);
    schemaCache[key] = res.columns;
  } catch {
    schemaCache[key] = null;   // unreachable — pairs fall back to text inputs
  }
  return schemaCache[key];
}

const colsOf = (src) => (src && schemaCache[`${src.format}|${src.path}`]) || null;

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
    measures: form.measures.filter((m) => m.name.trim() && m.expr.trim()),
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
    measures: [{ name: "rows", label: "Row Count", expr: "pl.len()", format: "number", description: "" }],
  });
  generated = null;
  showView("modelform");
  $("#mf-title").textContent = name ? `edit model · ${name}` : "new model";
  setStatus(name ? "loading…" : "");
  render();
  if (!state.bundles.length) state.bundles = await api("/api/dimensions").catch(() => []);
  if (!datasets) datasets = await api("/api/datasets").catch(() => null);
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

const note = (text) => el("div", { class: "empty-note mf-note" }, text);
const textField = (label, value, oninput, ph = "") => {
  const input = el("input", { value, placeholder: ph, spellcheck: "false" });
  input.addEventListener("input", () => { oninput(input.value); markDirty(); $("#mf-hint").textContent = stepError() || ""; $("#mf-next").disabled = !!stepError(); });
  return el("div", { class: "mf-field" }, el("div", { class: "field-label" }, label), input);
};

// a LEFT↔RIGHT relationship pair row; either side degrades to a text input
// when its schema is unreachable. The two names do not have to match.
function pairRow(pair, leftCols, rightCols, onchange, onremove) {
  const side = (val, cols, set, ph) => {
    if (!cols || !cols.length) {
      const input = el("input", { value: val, placeholder: ph, spellcheck: "false" });
      input.addEventListener("input", () => { set(input.value); markDirty(); });
      return input;
    }
    const sel = el("select", {}, el("option", { value: "" }, `— ${ph} —`));
    if (val && !cols.some((c) => c.name === val)) sel.append(el("option", { value: val }, val));
    for (const c of cols) sel.append(el("option", { value: c.name }, `${c.name} · ${c.dtype}`));
    sel.value = val;
    sel.addEventListener("change", () => { set(sel.value); markDirty(); onchange(); });
    return sel;
  };
  const rm = el("button", { class: "rm", title: "remove pair" }, "✕");
  rm.addEventListener("click", onremove);
  return el("div", { class: "mf-pair" },
    side(pair.left, leftCols, (v) => { pair.left = v; }, "this model's column"),
    el("span", { class: "mf-link" }, "⇄"),
    side(pair.right, rightCols, (v) => { pair.right = v; }, "their column"),
    rm);
}

function datasetCards(onpick, current) {
  const box = el("div", { class: "mf-ds-grid" });
  if (!datasets) { box.append(note("bucket not reachable — enter a path manually below")); return box; }
  for (const ds of datasets.datasets) {
    const on = current && current.path === ds.path;
    const card = el("div", { class: "mk-card clickable" + (on ? " sel" : "") },
      el("div", { class: "mk-top" }, el("span", { class: "nm" }, ds.key || "(root)"), el("span", { class: "fmt" }, ds.format)),
      el("div", { class: "path" }, ds.path),
      el("div", { class: "mk-sub" }, `${ds.object_count} obj · ${fmtBytes(ds.bytes)}`
        + (ds.models.length ? ` · read by ${[...new Set(ds.models.map((m) => m.name))].join(", ")}` : " · unmapped")
        + (ds.format_ambiguous ? " · ⚠ mixed types" : "")));
    card.addEventListener("click", () => onpick({ key: ds.key, path: ds.path, format: ds.format }));
    // grouped globs are drillable to one exact object (FR-006)
    if (ds.format !== "delta" && ds.objects.length > 1) {
      const drill = el("div", { class: "import-datasets" });
      for (const o of ds.objects) {
        const chip = el("div", { class: "col-chip", title: `use just ${o.key}` },
          el("span", {}, o.key.split("/").pop()), el("span", { class: "dt" }, o.format));
        chip.addEventListener("click", (e) => {
          e.stopPropagation();
          onpick({ key: o.key, path: `s3://${datasets.bucket}/${o.key}`, format: o.format });
        });
        drill.append(chip);
      }
      card.append(drill);
    }
    box.append(card);
  }
  return box;
}

// ── step 1: SOURCE ──

function renderSource(main) {
  main.append(el("div", { class: "sec-title" }, "1 · Name"));
  main.append(el("div", { class: "mf-row3" },
    textField("NAME (snake_case)", form.name, (v) => { form.name = v; }, "my_model"),
    textField("LABEL", form.label, (v) => { form.label = v; }, "My Model"),
    textField("DESCRIPTION", form.description, (v) => { form.description = v; }, "What this model covers.")));

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "2 · Source dataset"));
  main.append(note("the glob / dataset this model scans — pick one from the bucket:"));
  main.append(datasetCards(async (ds) => {
    form.source = { path: ds.path, format: ds.format };
    markDirty();
    render();
    await sourceSchema(ds.path, ds.format);
    render();
  }, form.source));

  const path = el("input", { value: form.source?.path || "", placeholder: "s3://bucket/prefix/*.parquet", spellcheck: "false" });
  const fmt = el("select", {}, ...["parquet", "csv", "delta"].map((f) => el("option", { value: f }, f)));
  fmt.value = form.source?.format || "parquet";
  const load = el("button", { class: "btn plain" }, "USE PATH");
  load.addEventListener("click", async () => {
    if (!path.value.trim()) return;
    form.source = { path: path.value.trim(), format: fmt.value };
    markDirty();
    await sourceSchema(form.source.path, form.source.format);
    render();
  });
  main.append(el("div", { class: "mf-manual" }, el("div", { class: "field-label" }, "OR TYPE A PATH"),
    el("div", { class: "mf-manual-row" }, path, fmt, load)));

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
    j.pairs.forEach((p, pi) => card.append(pairRow(p, colsOf(form.source), colsOf(j),
      () => render(), () => { j.pairs.splice(pi, 1); markDirty(); render(); })));
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
  imp.pairs.forEach((p, pi) => out.push(pairRow(p, modelColumns(), anchorDs && colsOf(anchorDs),
    () => render(), () => { imp.pairs.splice(pi, 1); markDirty(); render(); })));
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
  main.append(note("polars aggregation expressions — every measure must reduce to one value per group"));
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
        name: c.name, column: c.name,
        label: c.name.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase()),
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

function renderMeasures(box) {
  box.innerHTML = "";
  form.measures.forEach((m, idx) => {
    const name = el("input", { value: m.name, placeholder: "measure_name", spellcheck: "false" });
    name.addEventListener("input", () => { m.name = name.value; markDirty(); });
    const label = el("input", { value: m.label, placeholder: "Label", spellcheck: "false" });
    label.addEventListener("input", () => { m.label = label.value; markDirty(); });
    const fmt = el("select", {}, ...["number", "currency", "percent"].map((f) => el("option", { value: f }, f)));
    fmt.value = m.format;
    fmt.addEventListener("change", () => { m.format = fmt.value; markDirty(); });
    const expr = el("input", { class: "mf-expr", value: m.expr, placeholder: 'pl.col("unit_price").mean()', spellcheck: "false" });
    expr.addEventListener("input", () => { m.expr = expr.value; markDirty(); });
    const rm = el("button", { class: "rm", title: "remove measure" }, "✕");
    rm.addEventListener("click", () => { form.measures.splice(idx, 1); markDirty(); renderMeasures(box); });
    box.append(el("div", { class: "mf-measure" }, name, label, fmt, expr, rm));
  });
  const add = el("button", { class: "ghost" }, "+ add measure");
  add.addEventListener("click", () => { form.measures.push({ name: "", label: "", expr: "", format: "number", description: "" }); markDirty(); renderMeasures(box); });
  box.append(el("div", { class: "mf-quick-slot" }), add);
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
      expr: `pl.col("${c}").${AGGS[a]}`,
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
