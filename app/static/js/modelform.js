/* Guided model form: the default way to create/edit a fact model. A single
   sectioned editor (Overview / Data / Common models / Dimensions / Measures /
   YAML) with free navigation — no gated wizard steps. The form holds a
   structured spec that the server renders to YAML (POST /api/models/generate);
   generation runs continuously (debounced) so validation status and SAVE are
   always live. Raw YAML editing stays one click away (editor.js) — the form
   is the guided front door, the text editor the escape hatch. Form state is
   ephemeral: nothing persists until SAVE writes the generated yaml
   (Constitution V). */
"use strict";

import { refreshModels } from "./builder.js";
import { DSL_FUNCTIONS, dslContext, dslItems, makeCompleter } from "./completion.js";
import { openEditor } from "./editor.js";
import { openMemoriesModal } from "./memories.js";
import {
  autoGrow, colsOf, columnImportPanel, datasetCards, dimFromColumn, loadDatasets,
  manualPathRow, NAME_RE, note, pairRow, sectionRail, sourceSchema, spineCreatePanel, spineFields,
  synonymsInput, textAreaField, textField,
} from "./formkit.js";
import { $, api, el } from "./lib.js";
import { setPanelDescription, setPanelModel } from "./panelchat.js";
import { navigate, paths, setPath } from "./router.js";
import { hooks, showView, state } from "./state.js";

const SECTIONS = [
  { id: "overview", label: "OVERVIEW" },
  { id: "data", label: "DATA" },
  { id: "commons", label: "COMMON MODELS" },
  { id: "dimensions", label: "DIMENSIONS" },
  { id: "measures", label: "MEASURES" },
  { id: "yaml", label: "YAML" },
];
const AGGS = { sum: "sum", mean: "mean", min: "min", max: "max", count_distinct: "count_distinct" };

const form = {
  editingName: null,   // name of the existing model being edited (null = new)
  section: "overview",
  dirty: false,
  name: "", label: "", description: "",
  source: null,        // {path, format}
  relations: [],       // {name, path, format, how, pairs:[{left,right}]} — yaml `joins`
  imports: [],         // {bundle, anchor, datasets:null|[names], pairs:[{left,right}]}
  dimensions: [],      // spec dimension dicts (column/type/label/spine/geo/synonyms preserved)
  measures: [],        // {name, label, expr, format, description, synonyms, frame?, frame_emits?}
  // transient UI state (never part of the spec)
  pickingSource: false,     // source picker grid expanded while a source is already set
  importFor: null,          // "source" | relation object — whose column-import panel is open
  addingSpine: false,       // the "new time-spine dimension" panel is open
};
let generated = null;       // last /api/models/generate response
let genToken = 0;           // stale-response guard for the debounced generate
let seedBundle = null;      // common model to pre-import into the next new model

const setStatus = (html) => { $("#mf-status").innerHTML = html; };

/* The create-chooser sets this before navigating to /modelling/model/new so a
   fresh fact model can start from a common dimension model. */
export function setModelSeed(bundleName) { seedBundle = bundleName; }

// model-side columns offered as the LEFT half of a relation: the source's
// own columns plus everything the declared relations pull in
function modelColumns() {
  const cols = [...(colsOf(form.source) || [])];
  for (const r of form.relations) for (const c of colsOf(r) || []) {
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
    joins: form.relations.map((r) => ({ name: r.name, path: r.path, format: r.format, how: r.how, ...pairsOf(r.pairs) })),
    dimension_imports: form.imports.map((i) => ({
      bundle: i.bundle, anchor_dataset: i.anchor, datasets: i.datasets, ...pairsOf(i.pairs),
    })),
    dimensions: form.dimensions,
    measures: form.measures
      .filter((m) => m.name.trim() && m.expr.trim())
      .map((m) => ({
        name: m.name, label: m.label, expr: m.expr, format: m.format, description: m.description,
        ...(hasFrame(m) ? { frame: m.frame, frame_emits: m.frame_emits || [] } : {}),
        ...(m.synonyms && m.synonyms.length ? { synonyms: m.synonyms } : {}),
      })),
  };
}

export function confirmLeaveModelForm() {
  if (state.view !== "modelform" || !form.dirty) return true;
  return confirm("Leave the model form? In-progress edits are not saved.");
}
hooks.confirmLeaveModelForm = confirmLeaveModelForm;

export async function openModelForm(name) {
  if (!confirmLeaveModelForm()) return;
  Object.assign(form, {
    editingName: name, section: "overview", dirty: false,
    name: "", label: "", description: "", source: null, relations: [], imports: [],
    dimensions: [],
    measures: [{ name: "rows", label: "Row Count", expr: "count()", format: "number", description: "", synonyms: [] }],
    pickingSource: false, importFor: null, addingSpine: false,
  });
  generated = null;
  closeMeasureModal();
  showView("modelform");
  $("#mf-title").textContent = name ? `edit model · ${name}` : "new model";
  // build (open in Studio), memory curation, and the chat panel only make
  // sense for a model that's actually saved and registered — a fresh,
  // unsaved model has none of the three (chat needs a live model to query)
  $("#mf-build").hidden = !name;
  $("#mf-memory").hidden = !name;
  setPanelModel(name || null, name);
  setStatus(name ? "loading…" : "");
  render();
  if (!state.bundles.length) state.bundles = await api("/api/dimensions").catch(() => []);
  await loadDatasets();
  if (name) {
    const { spec } = await api(`/api/models/${name}/spec`);
    Object.assign(form, {
      name: spec.name, label: spec.label, description: spec.description,
      source: spec.source,
      relations: spec.joins.map((j) => ({ ...j, pairs: toPairs(j) })),
      imports: spec.dimension_imports.map((i) => ({
        bundle: i.bundle, anchor: i.anchor_dataset, datasets: i.datasets, pairs: toPairs(i),
      })),
      dimensions: spec.dimensions, measures: spec.measures,
    });
    setPanelModel(name, form.label || name);
    setPanelDescription(form.description);
    setStatus("");
    if (form.source) await sourceSchema(form.source.path, form.source.format);
    await Promise.all(form.relations.map((r) => sourceSchema(r.path, r.format)));
    await Promise.all(form.imports.map((i) => anchorSchema(i)));
  } else if (seedBundle) {
    // "start from a common model": pre-wire the import so the new model opens
    // with the shared dimensions already on board
    const b = state.bundles.find((x) => x.name === seedBundle);
    if (b && b.datasets.length) {
      const anchor = b.datasets[0];
      form.imports.push({ bundle: b.name, anchor: anchor.name, datasets: null, pairs: [guessPair(anchor)] });
      await anchorSchema(form.imports[0]);
    }
  }
  seedBundle = null;
  render();
  scheduleGenerate();
}
hooks.openModelForm = openModelForm;

const toPairs = (j) => j.left_on.map((l, idx) => ({ left: l, right: j.right_on[idx] ?? l }));

const bundleDataset = (bundleName, dsName) =>
  (state.bundles.find((b) => b.name === bundleName)?.datasets || []).find((d) => d.name === dsName);

const anchorSchema = (imp) => {
  const ds = bundleDataset(imp.bundle, imp.anchor);
  return ds ? sourceSchema(ds.path, ds.format) : Promise.resolve(null);
};

const markDirty = () => { form.dirty = true; scheduleGenerate(); };

// ── section problems (inline guidance, never a navigation gate) ────────────

function sectionProblem(id) {
  if (id === "overview" && !NAME_RE.test(form.name.trim())) {
    return "model name must be snake_case (a-z, 0-9, _)";
  }
  if (id === "data") {
    if (!form.source) return "pick a source dataset (or enter a path)";
    for (const r of form.relations) {
      if (!NAME_RE.test(r.name)) return "every related dataset needs a snake_case name";
      if (!r.pairs.some((p) => p.left && p.right)) return `'${r.name}': relate at least one column pair`;
    }
  }
  if (id === "commons") {
    for (const i of form.imports) {
      if (!i.pairs.some((p) => p.left && p.right)) return `import '${i.bundle}': relate at least one column pair`;
    }
  }
  if (id === "measures") {
    for (const m of form.measures) {
      if ((m.name.trim() && !m.expr.trim()) || (!m.name.trim() && m.expr.trim())) {
        return "a measure needs both a name and an expression (blank rows are ignored)";
      }
    }
  }
  return null;
}

const firstProblem = () => {
  for (const s of SECTIONS) {
    const p = sectionProblem(s.id);
    if (p) return { section: s.id, problem: p };
  }
  return null;
};

function sectionStatus(id) {
  if (sectionProblem(id)) return "err";
  if (id === "overview") return form.name ? "done" : "";
  if (id === "data") return form.source ? "done" : "";
  if (id === "commons") return form.imports.length ? "done" : "";
  if (id === "dimensions") return form.dimensions.length ? "done" : "";
  if (id === "measures") return form.measures.some((m) => m.name.trim() && m.expr.trim()) ? "done" : "";
  return "";
}

// ── continuous validation (debounced /api/models/generate) ────────────────

let genTimer = null;
function scheduleGenerate() {
  clearTimeout(genTimer);
  genTimer = setTimeout(runGenerate, 500);
  renderChrome();
}

async function runGenerate() {
  if (state.view !== "modelform") return;
  if (firstProblem()) { generated = null; renderChrome(); return; }
  const token = ++genToken;
  const res = await api("/api/models/generate", { method: "POST", body: toSpec() })
    .catch((e) => ({ ok: false, error: e.message }));
  if (token !== genToken || state.view !== "modelform") return;
  generated = res;
  renderChrome();
  if (form.section === "yaml") renderYamlInto($("#mf-main"));
}

// footer + rail refresh without rebuilding the main pane (so typing never
// loses focus)
function renderChrome() {
  sectionRail($("#mf-steps"), SECTIONS, form.section, sectionStatus, (id) => { form.section = id; render(); });
  const hint = $("#mf-hint");
  const live = $("#mf-live");
  const p = firstProblem();
  if (p) {
    hint.textContent = `${p.problem}`;
    live.innerHTML = "";
  } else {
    hint.textContent = "";
    const n = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;
    live.innerHTML = !generated ? '<span class="pending">validating…</span>'
      : generated.ok
        ? `<span class="ok">✓ valid</span> · ${n(generated.model.dimensions, "dimension")} · ${n(generated.model.measures, "measure")}`
          + (generated.schema_error ? ' · <span class="warn">⚠ source unreachable</span>' : "")
        : `<span class="err">✗ ${generated.error}</span>`;
  }
  $("#mf-save").disabled = !generated?.ok;
}

// ── rendering ──────────────────────────────────────────────────────────────

function render() {
  renderChrome();
  const main = $("#mf-main");
  main.innerHTML = "";
  ({ overview: renderOverview, data: renderData, commons: renderImports,
    dimensions: renderDimSection, measures: renderMeasureSection, yaml: renderYamlInto })[form.section](main);
}

// textField wrapper: every keystroke refreshes the dirty flag + live status
const field = (label, value, set, ph) => textField(label, value, (v) => { set(v); markDirty(); }, ph);

const PAIR_PH = { leftPh: "this model's column", rightPh: "their column" };
const modelPair = (pair, leftCols, rightCols, onremove) =>
  pairRow(pair, leftCols, rightCols, {
    ...PAIR_PH,
    onchange: () => { markDirty(); render(); },
    oninput: markDirty,
    onremove,
  });

// ── section: OVERVIEW ──

function renderOverview(main) {
  main.append(el("div", { class: "sec-title" }, "What this model is"));
  main.append(note("a fact model measures one dataset (orders, shipments, spend…) — Studio and Chat query it "
    + "through the dimensions and measures you declare here."));
  main.append(el("div", { class: "mf-row3" },
    field("NAME (snake_case)", form.name, (v) => { form.name = v; }, "my_model"),
    field("LABEL", form.label, (v) => { form.label = v; }, "My Model")));
  main.append(textAreaField("DESCRIPTION", form.description, (v) => {
    form.description = v;
    setPanelDescription(v);
    markDirty();
  }, "What this model covers — shown to Chat as context when answering questions about it."));
  if (form.imports.length && !form.editingName) {
    main.append(el("div", { class: "mf-picked", style: "margin-top:12px" },
      el("span", { class: "ok" }, "✓"),
      ` started from common model${form.imports.length === 1 ? "" : "s"} `
      + form.imports.map((i) => `'${i.bundle}'`).join(", ")
      + " — its shared dimensions are imported and ready to relate under COMMON MODELS"));
  }
}

// ── section: DATA (source + related datasets) ──

function renderData(main) {
  main.append(el("div", { class: "sec-title" }, "Source dataset"));
  if (!form.source || form.pickingSource) {
    main.append(note("the glob / dataset this model scans — pick one from the bucket:"));
    main.append(datasetCards(async (ds) => {
      form.source = { path: ds.path, format: ds.format };
      form.pickingSource = false;
      markDirty();
      render();
      await sourceSchema(ds.path, ds.format);
      form.importFor = "source";   // fresh source: offer its columns right away
      render();
    }, form.source));
    main.append(manualPathRow(form.source, async (src) => {
      form.source = src;
      form.pickingSource = false;
      markDirty();
      await sourceSchema(src.path, src.format);
      form.importFor = "source";
      render();
    }));
  }
  if (form.source && !form.pickingSource) {
    const cols = colsOf(form.source);
    const change = el("button", { class: "mini-btn" }, "change");
    change.addEventListener("click", () => { form.pickingSource = true; render(); });
    main.append(el("div", { class: "mf-card" },
      el("div", { class: "mf-card-head" },
        el("span", { class: "nm" }, "✓ source"),
        el("span", { class: "fmt" }, form.source.format),
        el("span", { class: "mf-colcount" }, cols ? `${cols.length} columns` : "columns not readable yet"),
        change),
      el("div", { class: "path" }, form.source.path)));
    appendColumnImport(main, "source", cols);
  }

  main.append(el("div", { class: "sec-title", style: "margin-top:18px" }, "Related datasets"));
  main.append(note("lookup tables related to the source — their columns become usable in dimensions and "
    + "measures. Relate each one by column pairs; the two sides don't need the same name."));
  form.relations.forEach((r, idx) => {
    const card = el("div", { class: "mf-card" });
    const rm = el("button", { class: "rm", title: "remove related dataset" }, "✕");
    rm.addEventListener("click", () => {
      form.relations.splice(idx, 1);
      if (form.importFor === r) form.importFor = null;
      markDirty();
      render();
    });
    const nameIn = el("input", { value: r.name, spellcheck: "false", class: "mf-join-name" });
    nameIn.addEventListener("input", () => { r.name = nameIn.value; markDirty(); });
    const how = el("select", {},
      el("option", { value: "left" }, "keep all source rows"),
      el("option", { value: "inner" }, "matching rows only"));
    how.value = r.how;
    how.addEventListener("change", () => { r.how = how.value; markDirty(); });
    card.append(el("div", { class: "mf-card-head" }, nameIn, el("span", { class: "fmt" }, r.format), how, rm),
      el("div", { class: "path" }, r.path),
      el("div", { class: "field-label", style: "margin-top:8px" }, "RELATION · this model ⇄ " + r.name));
    r.pairs.forEach((p, pi) => card.append(modelPair(p, colsOf(form.source), colsOf(r),
      () => { r.pairs.splice(pi, 1); markDirty(); render(); })));
    const addPair = el("button", { class: "ghost" }, "+ relate another column pair");
    addPair.addEventListener("click", () => { r.pairs.push({ left: "", right: "" }); markDirty(); render(); });
    card.append(addPair);
    appendColumnImport(card, r, colsOf(r));
    main.append(card);
  });

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "Add a related dataset"));
  main.append(datasetCards(async (ds) => {
    const base = (ds.key.split("/").pop() || "lookup").replace(/[^a-z0-9_]+/gi, "_").toLowerCase() || "lookup";
    const rel = { name: base, path: ds.path, format: ds.format, how: "left", pairs: [{ left: "", right: "" }] };
    form.relations.push(rel);
    markDirty();
    render();
    await sourceSchema(ds.path, ds.format);
    form.importFor = rel;   // fresh dataset: offer its columns right away
    render();
  }, null));
}

/* Column-import affordance for a dataset (the source, or one related
   dataset): a fresh pick opens the panel automatically (all columns
   selected), and a ghost button reopens it any time. */
function appendColumnImport(box, target, cols) {
  if (!cols || !cols.length) return;
  const taken = new Set(form.dimensions.map((d) => d.column || d.name));
  for (const dim of Object.keys(importedDimOwners())) taken.add(dim);
  const open = cols.filter((c) => !taken.has(c.name));
  if (form.importFor === target) {
    box.append(columnImportPanel(cols, [...taken], {
      onapply: (chosen) => {
        form.dimensions.push(...chosen.map(dimFromColumn));
        form.importFor = null;
        markDirty();
        render();
      },
      ondismiss: () => { form.importFor = null; render(); },
    }));
  } else if (open.length) {
    const btn = el("button", { class: "ghost mf-import-open" },
      `+ import columns as dimensions (${open.length} available)`);
    btn.addEventListener("click", () => { form.importFor = target; render(); });
    box.append(btn);
  } else {
    box.append(el("div", { class: "mf-colcount", style: "margin-top:8px" }, "all columns are already dimensions"));
  }
}

// ── section: COMMON MODELS (dimension_imports) ──

function renderImports(main) {
  main.append(el("div", { class: "sec-title" }, "Common models"));
  main.append(note("import shared dimensions declared once in a common dimension model. Anchor the import on "
    + "one of its datasets and relate it to this model by column pairs. Imported dimensions are read-only "
    + "here — they're managed in the common model, so every importer stays consistent."));
  if (!state.bundles.length) {
    main.append(note("none yet — create a common dimension model from the Modelling workspace first"));
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

// default relation guess for a freshly-imported bundle: its anchor's first
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
    el("span", { class: "mf-colcount" }, "the dataset this model relates to")));

  const anchorDs = bundleDataset(b.name, imp.anchor);
  out.push(el("div", { class: "field-label", style: "margin-top:8px" }, `RELATION · this model ⇄ ${b.name}.${imp.anchor}`));
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

  // the dimensions this import contributes, read-only by design
  const dims = (b.datasets || [])
    .filter((d) => imp.datasets === null || imp.datasets.includes(d.name))
    .flatMap((d) => d.dimensions);
  if (dims.length) {
    const locked = el("div", { class: "mf-locked-dims" },
      el("span", { class: "field-label" }, "IMPORTED DIMENSIONS · read-only"),
      ...dims.map((n) => el("span", { class: "chip taken" }, el("span", { class: "tick" }, "◈"), el("span", { class: "lbl" }, n))));
    const edit = el("button", { class: "mini-btn" }, "manage in common model ►");
    edit.addEventListener("click", () => navigate(paths.modellingBundle(b.name)));
    locked.append(edit);
    out.push(locked);
  }
  return out;
}

// ── section: DIMENSIONS ──

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

function renderDimSection(main) {
  main.append(el("div", { class: "sec-title" }, "This model's dimensions"));
  if (generated && !generated.ok) main.append(el("div", { class: "mf-warn" }, "⚠ " + generated.error));
  else if (generated?.schema_error) main.append(el("div", { class: "mf-warn" }, "⚠ " + generated.schema_error));

  const cols = generated?.columns || modelColumns();
  if (!form.dimensions.length) {
    main.append(note("none yet — import columns from the DATA section, or add them from the list below"));
  } else {
    main.append(note("refine each dimension's label, type and synonyms (synonyms help Chat match plain-language questions):"));
  }
  const rows = el("div", { class: "mf-dim-rows" });
  const known = new Set(cols.map((c) => c.name));
  form.dimensions.forEach((dim, idx) => {
    const colName = dim.column || dim.name;
    const dtype = cols.find((c) => c.name === colName)?.dtype || "?";
    const row = el("div", { class: "mf-dim-row on" + (dim.spine ? " spine" : "") });
    const rm = el("button", { class: "rm", title: "remove dimension" }, "✕");
    rm.addEventListener("click", () => { form.dimensions.splice(idx, 1); markDirty(); render(); });
    const label = el("input", { value: dim.label, placeholder: "Label", spellcheck: "false" });
    label.addEventListener("input", () => { dim.label = label.value; markDirty(); });
    const type = el("select", {}, ...["categorical", "time", "numeric"].map((t) => el("option", { value: t }, t)));
    type.value = dim.type;
    type.disabled = !!dim.spine;   // the server requires type: time on a spine dimension
    type.title = dim.spine ? "a time-spine dimension is always type: time" : "";
    type.addEventListener("change", () => { dim.type = type.value; markDirty(); });
    row.append(
      el("span", { class: "chip on" }, el("span", { class: "tick" }, "✓"),
        el("span", { class: "lbl" }, colName),
        el("span", { class: "hint" }, dim.spine ? "generated timeline" : known.has(colName) ? dtype : "column not in scan")),
      label, type,
      synonymsInput(dim.synonyms || (dim.synonyms = []), markDirty));
    if (dim.geo) row.append(el("span", { class: "mf-colcount" }, "◎ geo"));
    row.append(rm);
    if (dim.spine) row.append(spineFields(dim, cols, markDirty));
    rows.append(row);
  });
  main.append(rows);

  // columns still available to add (imported names live in the bundle instead)
  const owners = importedDimOwners();
  const declared = new Set(form.dimensions.map((d) => d.column || d.name));
  const addable = cols.filter((c) => !declared.has(c.name) && !owners[c.name]);
  if (addable.length) {
    main.append(el("div", { class: "sec-title", style: "margin-top:16px" }, "Add from columns"));
    const grid = el("div", { class: "mf-import-grid" });
    for (const c of addable) {
      const chip = el("button", { class: "chip" },
        el("span", { class: "tick" }, "+"), el("span", { class: "lbl" }, c.name), el("span", { class: "hint" }, c.dtype));
      chip.addEventListener("click", () => { form.dimensions.push(dimFromColumn(c)); markDirty(); render(); });
      grid.append(chip);
    }
    main.append(grid);
  } else if (!cols.length) {
    main.append(note("no readable columns — set a reachable source in DATA first, or add dimensions via EDIT YAML"));
  }

  if (cols.length) {
    main.append(el("div", { class: "sec-title", style: "margin-top:16px" }, "Time-spine dimension"));
    if (form.addingSpine) {
      main.append(spineCreatePanel(cols, {
        onapply: (dim) => { form.dimensions.push(dim); form.addingSpine = false; markDirty(); render(); },
        ondismiss: () => { form.addingSpine = false; render(); },
      }));
    } else {
      const btn = el("button", { class: "ghost" }, "+ create time-spine dimension (for point-in-time \"active\" measures)");
      btn.addEventListener("click", () => { form.addingSpine = true; render(); });
      main.append(btn);
    }
  }

  const importedNames = Object.keys(owners);
  if (importedNames.length) {
    main.append(el("div", { class: "sec-title", style: "margin-top:16px" }, "Imported from common models · read-only"));
    const box = el("div", { class: "mf-locked-dims" });
    for (const n of importedNames) {
      box.append(el("span", { class: "chip taken", title: `managed in common model '${owners[n]}'` },
        el("span", { class: "tick" }, "◈"), el("span", { class: "lbl" }, n), el("span", { class: "hint" }, owners[n])));
    }
    main.append(box, note("these dimensions are managed in their common model so every importer stays "
      + "consistent — open the common model from COMMON MODELS to edit them"));
  }
}

// ── section: MEASURES ──

const FRAME_TEMPLATE =
  `frame = (\n    lf.group_by(dims)\n    .agg(pl.col("...").sum())\n)`;

const blankMeasure = () => ({ name: "", label: "", expr: "", format: "number", description: "", synonyms: [] });
const blankFramedMeasure = () =>
  ({ name: "", label: "", expr: "", format: "number", description: "", synonyms: [], frame: FRAME_TEMPLATE, frame_emits: [] });

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
function frameEmitsPicker(m, redraw) {
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
      redraw();
    });
    wrap.append(chip);
  }
  return wrap;
}

/* Expression editor with intellisense: an auto-growing textarea wired to the
   shared completion engine. Used inline in measure cards and (larger) in the
   expanded editor modal. */
function exprEditor(m, statusEl, { rows = 1, cls = "mf-expr" } = {}) {
  const wrap = el("div", { class: "mf-expr-wrap" });
  // a framed measure's expr aggregates the frame with polars syntax, not the DSL
  const ph = hasFrame(m) ? 'pl.col("...").median()' : "mean(unit_price)";
  const ta = el("textarea", { class: cls, rows: String(rows), spellcheck: "false", placeholder: ph });
  ta.value = m.expr;
  const suggest = el("div", { class: "mf-suggest" });
  suggest.hidden = true;
  const completer = makeCompleter(ta, suggest, (upto, after, caret) => {
    const ctx = dslContext(upto, caret);
    return ctx ? { items: dslItems(ctx, exprColumns(), after), start: ctx.start } : null;
  }, () => scheduleCheck(m, statusEl));
  ta.addEventListener("input", () => { m.expr = ta.value; markDirty(); completer.update(); scheduleCheck(m, statusEl); });
  ta.addEventListener("keydown", (e) => completer.onKeydown(e));
  ta.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
  autoGrow(ta);
  wrap.append(ta, suggest);
  return wrap;
}

/* Frame editor (complex measures): python-ish escape hatch; only the
   col("...") trigger completes inside it. */
function frameEditor(m, statusEl) {
  const wrap = el("div", { class: "mf-expr-wrap" });
  const ta = el("textarea", { class: "mf-frame", rows: "7", spellcheck: "false" });
  ta.value = m.frame || "";
  const suggest = el("div", { class: "mf-suggest" });
  suggest.hidden = true;
  const completer = makeCompleter(ta, suggest, (upto, after, caret) => {
    const ctx = dslContext(upto, caret);
    return ctx && ctx.kind === "col" ? { items: dslItems(ctx, exprColumns(), after), start: ctx.start } : null;
  });
  ta.addEventListener("input", () => { m.frame = ta.value; markDirty(); completer.update(); scheduleCheck(m, statusEl); });
  ta.addEventListener("keydown", (e) => completer.onKeydown(e));
  ta.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
  wrap.append(ta, suggest);
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
  const expand = el("button", { class: "btn plain", title: "open the full measure editor" }, "⤢ EXPAND");
  expand.addEventListener("click", () => openMeasureModal(m, box));
  const rm = el("button", { class: "rm", title: "remove measure" }, "✕");
  rm.addEventListener("click", () => { form.measures.splice(idx, 1); markDirty(); renderMeasures(box); });

  const head = el("div", { class: "mf-measure" }, name, label, fmt,
    ...(isFramed ? [el("span", { class: "mf-badge" }, "⚡ COMPLEX")] : []), expand, rm);
  card.append(head);

  if (isFramed) {
    card.append(el("div", { class: "field-label", style: "margin-top:6px" }, "FRAME · derived step ahead of the aggregation (open the full editor for guidance)"));
    card.append(frameEditor(m, status));
    card.append(el("div", { class: "field-label", style: "margin-top:8px" }, "EXPR · aggregates the frame's own output columns"));
    card.append(exprEditor(m, status));
  } else {
    card.append(exprEditor(m, status));
  }

  const synRow = el("div", { class: "mf-syn-row" },
    el("span", { class: "field-label" }, "SYNONYMS"),
    synonymsInput(m.synonyms || (m.synonyms = []), markDirty));
  card.append(synRow, status);
  scheduleCheck(m, status);
  return card;
}

function renderMeasureSection(main) {
  main.append(el("div", { class: "sec-title" }, "Measures"));
  main.append(note("safe DSL expressions (e.g. sum(revenue), mean(price)) — every measure reduces to one value "
    + "per group. Use ⤢ EXPAND for the full editor with a function reference; complex measures add a "
    + "derived-frame step (group-by/window logic) ahead of their aggregation."));
  const measBox = el("div");
  main.append(measBox);
  renderMeasures(measBox);
  renderQuickAdd(measBox);
}

function renderMeasures(box) {
  box.innerHTML = "";
  form.measures.forEach((m, idx) => box.append(measureCard(m, idx, box)));
  const add = el("button", { class: "ghost" }, "+ add measure");
  add.addEventListener("click", () => { form.measures.push(blankMeasure()); markDirty(); renderMeasures(box); renderQuickAdd(box); });
  const addFramed = el("button", { class: "ghost" }, "+ add complex measure (frame)");
  addFramed.addEventListener("click", () => {
    const m = blankFramedMeasure();
    form.measures.push(m);
    markDirty();
    renderMeasures(box);
    renderQuickAdd(box);
    openMeasureModal(m, box);   // complex measures deserve the full editor
  });
  box.append(el("div", { class: "mf-quick-slot" }), el("div", { class: "mf-measure-actions" }, add, addFramed));
}

function renderQuickAdd(measBox) {
  const slot = measBox.querySelector(".mf-quick-slot");
  const cols = (generated?.columns || modelColumns()).filter((c) => /int|float|decimal/i.test(c.dtype));
  if (!slot || !cols.length) return;
  const colSel = el("select", {}, ...cols.map((c) => el("option", { value: c.name }, c.name)));
  const aggSel = el("select", {}, ...Object.keys(AGGS).map((a) => el("option", { value: a }, a)));
  const add = el("button", { class: "btn plain" }, "+ QUICK ADD");
  add.addEventListener("click", () => {
    const c = colSel.value, a = aggSel.value;
    form.measures.push({
      name: `${c}_${a}`, label: "", format: "number", description: "", synonyms: [],
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

// ── expanded measure editor (modal) ──

let modalMeasure = null;   // measure being edited in the modal
let modalListBox = null;   // measures container to re-render on close

function closeMeasureModal() {
  $("#measure-modal").hidden = true;
  $("#measure-modal").innerHTML = "";
  modalMeasure = null;
  modalListBox = null;
}

// close + mirror the modal's edits back into the measure list
function dismissMeasureModal() {
  const box = modalListBox;
  closeMeasureModal();
  if (box && box.isConnected) { renderMeasures(box); renderQuickAdd(box); }
  renderChrome();
}

function openMeasureModal(m, listBox) {
  modalMeasure = m;
  modalListBox = listBox;
  drawMeasureModal();
  $("#measure-modal").hidden = false;
}

function drawMeasureModal() {
  const m = modalMeasure;
  const overlay = $("#measure-modal");
  overlay.innerHTML = "";
  const isFramed = hasFrame(m);
  const status = el("div", { class: "mf-measure-status" });

  const done = el("button", { class: "btn" }, "✓ DONE");
  done.addEventListener("click", dismissMeasureModal);
  const toggle = el("button", { class: "btn alt" }, isFramed ? "✕ DROP FRAME" : "⚡ MAKE COMPLEX");
  toggle.addEventListener("click", () => {
    if (isFramed) { m.frame = null; m.frame_emits = []; } else { m.frame = m.frame || FRAME_TEMPLATE; m.frame_emits = m.frame_emits || []; }
    markDirty();
    drawMeasureModal();
  });

  const name = el("input", { value: m.name, placeholder: "measure_name", spellcheck: "false" });
  name.addEventListener("input", () => { m.name = name.value; markDirty(); scheduleCheck(m, status); });
  const label = el("input", { value: m.label, placeholder: "Label", spellcheck: "false" });
  label.addEventListener("input", () => { m.label = label.value; markDirty(); });
  const fmt = el("select", {}, ...["number", "currency", "percent"].map((f) => el("option", { value: f }, f)));
  fmt.value = m.format;
  fmt.addEventListener("change", () => { m.format = fmt.value; markDirty(); });
  const desc = el("input", { value: m.description || "", placeholder: "Description — what this measure means", spellcheck: "false" });
  desc.addEventListener("input", () => { m.description = desc.value; markDirty(); });

  const body = el("div", { class: "mm-body" });
  const editorCol = el("div", { class: "mm-editor" });

  if (isFramed) {
    editorCol.append(
      el("div", { class: "field-label" }, "FRAME · builds a derived LazyFrame ahead of the aggregation"),
      note("lf (filtered scan), dims (the query's other grouping columns) and pl are in scope; assign the "
        + "result to `frame`. Saving a framed measure requires the admin role."),
      frameEditor(m, status),
      el("div", { class: "field-label", style: "margin-top:10px" }, "FRAME_EMITS · dimensions the frame computes itself"),
      frameEmitsPicker(m, drawMeasureModal),
      el("div", { class: "field-label", style: "margin-top:10px" }, "EXPR · aggregates the frame's own output columns"),
      exprEditor(m, status, { rows: 2, cls: "mf-expr mm-expr" }));
  } else {
    editorCol.append(
      el("div", { class: "field-label" }, "EXPRESSION"),
      exprEditor(m, status, { rows: 4, cls: "mf-expr mm-expr" }));
  }
  editorCol.append(status,
    el("div", { class: "field-label", style: "margin-top:10px" }, "SYNONYMS · plain-language names Chat can match"),
    synonymsInput(m.synonyms || (m.synonyms = []), markDirty));

  // clickable DSL reference: inserts at the caret of the last-focused editor
  const ref = el("div", { class: "mm-ref" }, el("div", { class: "sec-title" }, "Function reference"));
  for (const [insert, hint, off] of DSL_FUNCTIONS) {
    const row = el("button", { class: "mm-fn", title: "insert at cursor" },
      el("span", { class: "fn" }, insert), el("span", { class: "hint" }, hint));
    row.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const ta = overlay.querySelector("textarea.mm-expr") || overlay.querySelector("textarea");
      if (!ta) return;
      const start = ta.selectionStart ?? ta.value.length;
      ta.value = ta.value.slice(0, start) + insert + ta.value.slice(ta.selectionEnd ?? start);
      ta.setSelectionRange(start + insert.length + off, start + insert.length + off);
      ta.dispatchEvent(new Event("input", { bubbles: true }));
      ta.focus();
    });
    ref.append(row);
  }
  body.append(editorCol, ref);

  const card = el("div", { class: "mm-card" },
    el("div", { class: "chart-head" },
      el("span", { class: "editor-file" }, "measure editor"),
      ...(isFramed ? [el("span", { class: "mf-badge" }, "⚡ COMPLEX")] : []),
      el("span", { style: "flex:1" }),
      toggle, done),
    el("div", { class: "mm-fields" },
      el("div", { class: "mf-field" }, el("div", { class: "field-label" }, "NAME (snake_case)"), name),
      el("div", { class: "mf-field" }, el("div", { class: "field-label" }, "LABEL"), label),
      el("div", { class: "mf-field" }, el("div", { class: "field-label" }, "FORMAT"), fmt),
      el("div", { class: "mf-field grow" }, el("div", { class: "field-label" }, "DESCRIPTION"), desc)),
    body);
  overlay.append(card);
  overlay.onclick = (e) => { if (e.target === overlay) done.click(); };
  scheduleCheck(m, status);
}

// ── section: YAML ──

function renderYamlInto(main) {
  if (form.section !== "yaml") return;
  main.innerHTML = "";
  main.append(el("div", { class: "sec-title" }, "Generated YAML"));
  const report = el("div", { class: "editor-report" });
  const pre = el("pre", { class: "mf-yaml" }, "");
  main.append(report, note(form.editingName
    ? `saving rewrites models/${form.editingName}.yaml from this form (hand-written comments are not preserved)`
    : "saving writes a new file under models/ and hot-reloads the semantic layer"), pre);
  const p = firstProblem();
  if (p) {
    report.innerHTML = `<span class="warn">⚠ ${p.problem}</span>`;
    return;
  }
  if (!generated) { report.textContent = "generating yaml…"; return; }
  pre.textContent = generated.yaml || "";
  if (generated.ok) {
    report.innerHTML = `<span class="ok">✓ valid</span> — <b>${generated.model.label}</b> (${generated.model.name}) · `
      + `${generated.model.dimensions} dimensions · ${generated.model.measures} measures`
      + (generated.schema_error ? `<br><span class="warn">⚠ ${generated.schema_error}</span>` : "");
  } else {
    report.innerHTML = `<span class="err">✗ ${generated.error}</span>`;
  }
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
    navigate(paths.modelling());
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
  closeMeasureModal();
  openEditor("model", form.editingName, { text: res?.yaml });
  setPath(form.editingName ? paths.modellingModelYaml(form.editingName) : paths.modellingNewModelYaml());
}

export function attachModelForm() {
  $("#mf-save").addEventListener("click", saveModelForm);
  $("#mf-yaml").addEventListener("click", editAsYaml);
  $("#mf-build").addEventListener("click", () => {
    if (form.editingName) navigate(paths.studioModel(form.editingName));
  });
  $("#mf-memory").addEventListener("click", () => {
    if (form.editingName) {
      openMemoriesModal({ name: form.editingName, label: form.label, dimensions: form.dimensions, measures: form.measures });
    }
  });
  $("#mf-back").addEventListener("click", () => {
    if (!confirmLeaveModelForm()) return;
    form.dirty = false;
    closeMeasureModal();
    navigate(paths.modelling());
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#measure-modal").hidden) dismissMeasureModal();
  });
}
