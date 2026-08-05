/* Guided model form: the default way to create/edit a fact model, and the
   same general design as the common-model form (bundleform.js) — step one
   adds the datasets and imports any common models, step two says how they
   relate. A single sectioned editor (Overview / Datasets / Relations /
   Dimensions / Measures / YAML) with free navigation, no gated wizard steps.

   The datasets need not all relate to each other. Each connected group of
   them is a fact table in its own right: two fact tables that each relate to
   the same common model, and to nothing else, is the ordinary shape, and the
   RELATIONS section names the groups the current relations produce. The
   server never joins them — see app/semantic.py's ModelPart.

   The form holds a structured spec that the server renders to YAML (POST
   /api/models/generate); generation runs continuously (debounced) so
   validation status and SAVE are always live. Raw YAML editing stays one
   click away (editor.js) — the form is the guided front door, the text editor
   the escape hatch. Form state is ephemeral: nothing persists until SAVE
   writes the generated yaml (Constitution V). */
"use strict";

import { refreshModels } from "./builder.js";
import { DSL_FUNCTIONS, dslContext, dslItems, makeCompleter } from "./completion.js";
import { openEditor } from "./editor.js";
import { openMemoriesModal } from "./memories.js";
import {
  autoGrow, colsOf, columnImportPanel, datasetCards, dimFromColumn, loadDatasets,
  manualPathRow, matchRow, NAME_RE, note, pairRow, sectionRail, sourceSchema, spineCreatePanel,
  spineFields, synonymsInput, textAreaField, textField, uploadRow,
} from "./formkit.js";
import { $, api, el } from "./lib.js";
import { setPanelDescription, setPanelModel } from "./panelchat.js";
import { navigate, paths, setPath } from "./router.js";
import { hooks, showView, state } from "./state.js";

const SECTIONS = [
  { id: "overview", label: "OVERVIEW" },
  { id: "datasets", label: "DATASETS" },
  { id: "relations", label: "RELATIONS" },
  { id: "dimensions", label: "DIMENSIONS" },
  { id: "measures", label: "MEASURES" },
  { id: "yaml", label: "YAML" },
];
const AGGS = { sum: "sum", mean: "mean", min: "min", max: "max", count_distinct: "count_distinct" };

const form = {
  editingName: null,   // name of the existing model being edited (null = new)
  locked: false,       // built-in demo model — DELETE MODEL stays hidden
  section: "overview",
  dirty: false,
  name: "", label: "", description: "",
  datasets: [],        // {name, path, format, dimensions:[spec dicts], measures:[...]}
  rels: [],            // {from, to, how, pairs:[{left,right}]} — relations between datasets
  // {bundle, from, anchor, datasets:null|[names], how, pairs:[{left,right}],
  //  interval:{start,end,point}, match}
  // `how: "between"` uses `interval`/`match` instead of `pairs` — see importControls
  imports: [],
  // transient UI state (never part of the spec)
  importFor: null,     // dataset object whose column-import panel is open
  spineFor: null,      // dataset object whose new-spine-dimension panel is open
};
let generated = null;       // last /api/models/generate response
let genToken = 0;           // stale-response guard for the debounced generate
let seedBundle = null;      // common model to pre-import into the next new model

const setStatus = (html) => { $("#mf-status").innerHTML = html; };
const dsByName = (name) => form.datasets.find((d) => d.name === name);

/* The create-chooser sets this before navigating to /modelling/model/new so a
   fresh fact model can start from a common dimension model. */
export function setModelSeed(bundleName) { seedBundle = bundleName; }

// ── the dataset graph: which datasets form one fact table ─────────────────
// Mirrors app/semantic.py's _components — the relations partition the model's
// datasets into connected groups, and each group is scanned on its own.

function componentOf(name) {
  const seen = new Set([name]);
  const frontier = [name];
  while (frontier.length) {
    const node = frontier.pop();
    for (const r of form.rels) {
      for (const [a, b] of [[r.from, r.to], [r.to, r.from]]) {
        if (a === node && b && !seen.has(b) && dsByName(b)) { seen.add(b); frontier.push(b); }
      }
    }
  }
  return form.datasets.filter((d) => seen.has(d.name));
}

/* Every fact table the current relations produce, each as its member datasets
   in declaration order — the answer to "is this one model or three?". */
function components() {
  const placed = new Set();
  const out = [];
  for (const d of form.datasets) {
    if (placed.has(d.name)) continue;
    const group = componentOf(d.name);
    group.forEach((x) => placed.add(x.name));
    out.push(group);
  }
  return out;
}

// columns a dataset can address: the whole fact table's post-relation scan
// when the server has resolved one, else whatever its own component's sources
// report on their own
function columnsFor(name) {
  const part = (generated?.parts || []).find((p) => p.datasets.includes(name));
  const resolved = part && generated.part_columns?.[part.name];
  if (resolved) return resolved;
  const cols = [];
  for (const d of componentOf(name)) {
    for (const c of colsOf(d) || []) if (!cols.some((x) => x.name === c.name)) cols.push(c);
  }
  return cols;
}

// dimension names already spoken for in a dataset's fact table — native ones
// declared by any member, plus everything its common-model imports bring in.
// Scoped to the fact table, not the model: two *separate* fact tables reusing
// a name is what conforms them, not a clash.
function takenIn(name) {
  const group = componentOf(name).map((d) => d.name);
  const taken = new Map();   // dimension name -> where it comes from
  for (const d of form.datasets) {
    if (!group.includes(d.name)) continue;
    for (const dim of d.dimensions) taken.set(dim.name, d.name);
  }
  for (const imp of form.imports) {
    if (!group.includes(imp.from)) continue;
    const b = state.bundles.find((x) => x.name === imp.bundle);
    for (const ds of b?.datasets || []) {
      if (imp.datasets && !imp.datasets.includes(ds.name)) continue;
      for (const dim of ds.dimensions) if (!taken.has(dim)) taken.set(dim, imp.bundle);
    }
  }
  return taken;
}

function toSpec() {
  const pairsOf = (rows) => {
    const done = rows.filter((p) => p.left && p.right);
    return { left_on: done.map((p) => p.left), right_on: done.map((p) => p.right) };
  };
  return {
    name: form.name.trim(), label: form.label.trim(), description: form.description.trim(),
    datasets: form.datasets.map((d) => ({
      name: d.name,
      source: { path: d.path, format: d.format },
      joins: form.rels.filter((r) => r.from === d.name).map((r) => ({
        to: r.to, how: r.how, ...pairsOf(r.pairs),
      })),
      dimensions: d.dimensions,
      measures: d.measures
        .filter((m) => m.name.trim() && m.expr.trim())
        .map((m) => ({
          name: m.name, label: m.label, expr: m.expr, format: m.format, description: m.description,
          ...(hasFrame(m) ? { frame: m.frame, frame_emits: m.frame_emits || [] } : {}),
          ...(m.synonyms && m.synonyms.length ? { synonyms: m.synonyms } : {}),
        })),
    })),
    dimension_imports: form.imports.map((i) => ({
      bundle: i.bundle, from_dataset: i.from, anchor_dataset: i.anchor, datasets: i.datasets,
      how: i.how || "left", match: i.match || "overlap",
      ...(i.how === "between" ? intervalOf(i.interval) : pairsOf(i.pairs)),
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
    name: "", label: "", description: "", datasets: [], rels: [], imports: [],
    importFor: null, spineFor: null,
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
  form.locked = false;
  $("#mf-delete").hidden = true;   // unknown until the spec fetch below resolves (existing model only)
  setPanelModel(name || null, name);
  setStatus(name ? "loading…" : "");
  render();
  if (!state.bundles.length) state.bundles = await api("/api/dimensions").catch(() => []);
  await loadDatasets();
  if (name) {
    const { spec, locked } = await api(`/api/models/${name}/spec`);
    form.locked = locked;
    $("#mf-delete").hidden = locked;
    Object.assign(form, {
      name: spec.name, label: spec.label, description: spec.description,
      datasets: spec.datasets.map((d) => ({
        name: d.name, path: d.source.path, format: d.source.format,
        dimensions: d.dimensions, measures: d.measures,
      })),
      rels: spec.datasets.flatMap((d) => d.joins.map((j) => ({
        from: d.name, to: j.to, how: j.how, pairs: toPairs(j),
      }))),
      imports: spec.dimension_imports.map(importFromSpec),
    });
    setPanelModel(name, form.label || name);
    setPanelDescription(form.description);
    setStatus("");
    await Promise.all(form.datasets.map((d) => sourceSchema(d.path, d.format)));
    await Promise.all(form.imports.map((i) => anchorSchema(i)));
  } else if (seedBundle) {
    // "start from a common model": pre-wire the import so the new model opens
    // with the shared dimensions already on board, waiting only for the
    // dataset they relate to
    const b = state.bundles.find((x) => x.name === seedBundle);
    if (b && b.datasets.length) {
      form.imports.push(newImport(b, b.datasets[0]));
      await anchorSchema(form.imports[0]);
    }
  }
  seedBundle = null;
  render();
  scheduleGenerate();
}
hooks.openModelForm = openModelForm;

const toPairs = (j) => j.left_on.map((l, idx) => ({ left: l, right: j.right_on[idx] ?? l }));

/* An interval (`how: between`) import's keys: a [start, end] pair on the
   relating dataset and one date column on the imported side. Emitted only once
   all three are chosen — a partial one renders as empty keys, which /generate
   reports as a problem rather than silently writing a broken join. */
const intervalOf = (iv = {}) => (iv.start && iv.end && iv.point
  ? { left_on: [iv.start, iv.end], right_on: [iv.point] }
  : { left_on: [], right_on: [] });

const toInterval = (j) => (j.how === "between"
  ? { start: j.left_on[0] || "", end: j.left_on[1] || "", point: j.right_on[0] || "" }
  : { start: "", end: "", point: "" });

/* Form-state shape for one dimension_imports entry. */
const importFromSpec = (i) => ({
  bundle: i.bundle, from: i.from_dataset, anchor: i.anchor_dataset, datasets: i.datasets,
  how: i.how || "left",
  pairs: i.how === "between" ? [{ left: "", right: "" }] : toPairs(i),
  interval: toInterval(i), match: i.match || "overlap",
});

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
  if (id === "datasets") {
    if (!form.datasets.length) return "add at least one dataset";
    const seen = new Set();
    for (const d of form.datasets) {
      if (!NAME_RE.test(d.name)) return "every dataset needs a snake_case name";
      if (seen.has(d.name)) return `two datasets are both named '${d.name}'`;
      seen.add(d.name);
    }
  }
  if (id === "relations") {
    for (const r of form.rels) {
      if (r.from === r.to) return "a relation cannot connect a dataset to itself";
      if (!r.pairs.some((p) => p.left && p.right)) return `${r.from} ⇄ ${r.to}: relate at least one column pair`;
    }
    for (const i of form.imports) {
      if (!i.from) return `import '${i.bundle}': pick the dataset that relates to it`;
      if (i.how === "between") {
        const iv = i.interval || {};
        if (!(iv.start && iv.end && iv.point)) {
          return `import '${i.bundle}': a date-range relation needs a start, an end and their date column`;
        }
      } else if (!i.pairs.some((p) => p.left && p.right)) {
        return `import '${i.bundle}': relate at least one column pair`;
      }
    }
    // a dataset related to nothing and measuring nothing is a fact table with
    // nothing in it — the server refuses it, so say so before SAVE does
    if (components().length > 1) {
      for (const group of components()) {
        if (!group.some((d) => d.measures.some((m) => m.name.trim() && m.expr.trim()))) {
          return `'${group[0].name}' relates to nothing else here and has no measures — `
            + "relate it to another dataset, or give it one";
        }
      }
    }
  }
  if (id === "measures") {
    for (const d of form.datasets) {
      for (const m of d.measures) {
        if ((m.name.trim() && !m.expr.trim()) || (!m.name.trim() && m.expr.trim())) {
          return "a measure needs both a name and an expression (blank rows are ignored)";
        }
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

const allDimensions = () => form.datasets.flatMap((d) => d.dimensions);
const allMeasures = () => form.datasets.flatMap((d) => d.measures);

function sectionStatus(id) {
  if (sectionProblem(id)) return "err";
  if (id === "overview") return form.name ? "done" : "";
  if (id === "datasets") return form.datasets.length ? "done" : "";
  if (id === "relations") return form.rels.length || form.imports.length ? "done" : "";
  if (id === "dimensions") return allDimensions().length ? "done" : "";
  if (id === "measures") return allMeasures().some((m) => m.name.trim() && m.expr.trim()) ? "done" : "";
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
    const parts = generated?.parts?.length || 0;
    live.innerHTML = !generated ? '<span class="pending">validating…</span>'
      : generated.ok
        ? `<span class="ok">✓ valid</span> · ${n(parts, "fact table")}`
          + ` · ${n(generated.model.dimensions, parts > 1 ? "shared dimension" : "dimension")}`
          + ` · ${n(generated.model.measures, "measure")}`
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
  ({ overview: renderOverview, datasets: renderDatasets, relations: renderRels,
    dimensions: renderDimSection, measures: renderMeasureSection, yaml: renderYamlInto })[form.section](main);
}

// textField wrapper: every keystroke refreshes the dirty flag + live status
const field = (label, value, set, ph) => textField(label, value, (v) => { set(v); markDirty(); }, ph);

// ── section: OVERVIEW ──

function renderOverview(main) {
  main.append(el("div", { class: "sec-title" }, "What this model is"));
  main.append(note("a fact model measures one or more datasets (orders, shipments, spend…) — Studio and "
    + "Chat query it through the dimensions and measures you declare here. Add the datasets under "
    + "DATASETS, then say how they relate under RELATIONS; datasets you don't relate to each other stay "
    + "separate fact tables, which is how two of them can share a common model's dimensions without "
    + "ever being joined together."));
  main.append(el("div", { class: "mf-row3" },
    field("NAME (snake_case)", form.name, (v) => { form.name = v; }, "my_model"),
    field("LABEL", form.label, (v) => { form.label = v; }, "My Model")));
  main.append(textAreaField("DESCRIPTION", form.description, (v) => {
    form.description = v;
    setPanelDescription(v);
    markDirty();
  }, "What this model covers — shown to Chat as context when answering questions about it."));
  const bundleNames = [...new Set(form.imports.map((i) => i.bundle))];
  if (bundleNames.length && !form.editingName) {
    main.append(el("div", { class: "mf-picked", style: "margin-top:12px" },
      el("span", { class: "ok" }, "✓"),
      ` started from common model${bundleNames.length === 1 ? "" : "s"} `
      + bundleNames.map((n) => `'${n}'`).join(", ")
      + " — add the dataset it describes under DATASETS, then relate the two under RELATIONS"));
  }
}

// ── section: DATASETS (the model's own tables + the common models it imports) ──

function renderDatasets(main) {
  main.append(el("div", { class: "sec-title" }, "Datasets in this model"));
  main.append(note("every table this model reads. They don't have to be related to each other — "
    + "relate them (or not) under RELATIONS."));
  form.datasets.forEach((d, idx) => {
    const nameIn = el("input", { value: d.name, spellcheck: "false", class: "mf-join-name" });
    nameIn.addEventListener("input", () => {
      // renaming a dataset follows through to everything that points at it
      for (const r of form.rels) {
        if (r.from === d.name) r.from = nameIn.value;
        if (r.to === d.name) r.to = nameIn.value;
      }
      for (const i of form.imports) if (i.from === d.name) i.from = nameIn.value;
      d.name = nameIn.value;
      markDirty();
    });
    const rm = el("button", { class: "rm", title: "remove dataset" }, "✕");
    rm.addEventListener("click", () => {
      form.datasets.splice(idx, 1);
      form.rels = form.rels.filter((r) => r.from !== d.name && r.to !== d.name);
      for (const i of form.imports) if (i.from === d.name) i.from = "";
      if (form.importFor === d) form.importFor = null;
      markDirty();
      render();
    });
    const cols = colsOf(d);
    const card = el("div", { class: "mf-card" },
      el("div", { class: "mf-card-head" },
        nameIn, el("span", { class: "fmt" }, d.format),
        el("span", { class: "mf-colcount" }, cols
          ? `${cols.length} columns · ${d.dimensions.length} dims · ${d.measures.length} measures`
          : "columns not readable yet"),
        rm),
      el("div", { class: "path" }, d.path));
    appendColumnImport(card, d, cols);
    main.append(card);
  });

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "Add a dataset"));
  main.append(datasetCards((ds) => addDataset(ds), null));
  main.append(manualPathRow(null, (src) => addDataset({ key: src.path, ...src })));
  main.append(uploadRow((src) => addDataset({ key: src.path, ...src })));

  renderBundlePicker(main);
}

/* The "import any common models" half of step one: pick which shared
   dimension sets this model draws on (and which of their datasets). How each
   one relates to this model is step two. */
function renderBundlePicker(main) {
  main.append(el("div", { class: "sec-title", style: "margin-top:18px" }, "Common models"));
  main.append(note("shared dimensions declared once in a common dimension model — geography, calendars, "
    + "hierarchies. Import one here; relate it to one of your datasets under RELATIONS. Imported "
    + "dimensions are read-only: they're managed in the common model, so every importer stays consistent."));
  if (!state.bundles.length) {
    main.append(note("none yet — create a common dimension model from the Modelling workspace first"));
    return;
  }
  for (const b of state.bundles) {
    // a bundle can be imported more than once — e.g. reference data related on
    // a key, and a date table in the same bundle related on a range; and, in a
    // model with several fact tables, once per fact table
    const imps = form.imports.filter((i) => i.bundle === b.name);
    const card = el("div", { class: "mf-card" + (imps.length ? " on" : "") });
    const add = el("button", { class: "btn " + (imps.length ? "plain" : "") },
      imps.length ? "+ IMPORT AGAIN" : "IMPORT");
    add.addEventListener("click", async () => {
      const next = newImport(b, b.datasets[0]);
      form.imports.push(next);
      markDirty();
      render();
      await anchorSchema(next);
      render();
    });
    card.append(el("div", { class: "mf-card-head" },
      el("span", { class: "nm" }, b.label),
      el("span", { class: "mf-colcount" }, b.datasets.map((d) => d.name).join(", ")), add));
    imps.forEach((imp, idx) => card.append(...importDatasetsRow(b, imp, idx)));
    main.append(card);
  }
}

/* Per-import row in the DATASETS section: which of the bundle's datasets come
   in, and where the relation itself is set up. */
function importDatasetsRow(b, imp, idx) {
  const out = [];
  const head = el("div", { class: "mf-anchor-row" + (idx > 0 ? " mf-rel-sep" : "") },
    el("span", { class: "field-label" }, `IMPORT ${idx + 1}`),
    el("span", { class: "mf-colcount" },
      imp.from ? `relates to '${imp.from}'` : "not related to a dataset yet — see RELATIONS"));
  const rm = el("button", { class: "rm", title: "drop this import" }, "✕");
  rm.addEventListener("click", () => {
    form.imports = form.imports.filter((i) => i !== imp);
    markDirty();
    render();
  });
  head.append(rm);
  out.push(head);

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
  out.push(...importedDimChips(b, imp));
  return out;
}

// the dimensions this import contributes, read-only by design
function importedDimChips(b, imp) {
  const dims = (b.datasets || [])
    .filter((d) => imp.datasets === null || imp.datasets.includes(d.name))
    .flatMap((d) => d.dimensions);
  if (!dims.length) return [];
  const locked = el("div", { class: "mf-locked-dims" },
    el("span", { class: "field-label" }, "IMPORTED DIMENSIONS · read-only"),
    ...dims.map((n) => el("span", { class: "chip taken" },
      el("span", { class: "tick" }, "◈"), el("span", { class: "lbl" }, n))));
  const edit = el("button", { class: "mini-btn" }, "manage in common model ►");
  edit.addEventListener("click", () => navigate(paths.modellingBundle(b.name)));
  locked.append(edit);
  return [locked];
}

/* Column-import affordance for one dataset: a fresh add opens the panel
   automatically (all columns selected), a ghost button reopens it. */
function appendColumnImport(card, d, cols) {
  if (!cols || !cols.length) return;
  const taken = takenIn(d.name);
  const open = cols.filter((c) => !taken.has(c.name));
  if (form.importFor === d) {
    card.append(columnImportPanel(cols, [...taken.keys()], {
      onapply: (chosen) => {
        d.dimensions.push(...chosen.map(dimFromColumn));
        form.importFor = null;
        markDirty();
        render();
      },
      ondismiss: () => { form.importFor = null; render(); },
    }));
  } else if (open.length) {
    const btn = el("button", { class: "ghost mf-import-open" },
      `+ import columns as dimensions (${open.length} available)`);
    btn.addEventListener("click", () => { form.importFor = d; render(); });
    card.append(btn);
  } else {
    card.append(el("div", { class: "mf-colcount", style: "margin-top:8px" }, "all columns are already dimensions"));
  }
}

async function addDataset(ds) {
  const base = (ds.key.split("/").pop() || "dataset").replace(/\.[a-z0-9]+$/i, "")
    .replace(/[^a-z0-9_]+/gi, "_").toLowerCase() || "dataset";
  let name = base;
  for (let n = 2; dsByName(name); n++) name = `${base}_${n}`;
  const rec = { name, path: ds.path, format: ds.format, dimensions: [], measures: [] };
  form.datasets.push(rec);
  // the first dataset is the one an already-seeded common model was waiting for
  for (const imp of form.imports) if (!imp.from) imp.from = name;
  markDirty();
  render();
  await sourceSchema(ds.path, ds.format);
  form.importFor = rec;   // fresh dataset: offer its columns right away
  render();
}

// ── section: RELATIONS ──

function renderRels(main) {
  main.append(el("div", { class: "sec-title" }, "Relations"));
  main.append(note("how this model's datasets relate — to each other, and to the common models you "
    + "imported. Column pairs don't need matching names, and nothing has to be related to everything: "
    + "what you leave unrelated stays a fact table of its own."));
  renderFactTableSummary(main);

  main.append(el("div", { class: "sec-title", style: "margin-top:16px" }, "Between this model's datasets"));
  if (form.datasets.length < 2) {
    main.append(note("nothing to relate — this model has a single dataset"));
  } else {
    form.rels.forEach((r, idx) => main.append(datasetRelCard(r, idx)));
    const add = el("button", { class: "ghost" }, "+ relate two datasets");
    add.addEventListener("click", () => {
      form.rels.push({
        from: form.datasets[0].name, to: form.datasets[1].name, how: "left",
        pairs: [{ left: "", right: "" }],
      });
      markDirty();
      render();
    });
    main.append(add);
  }

  main.append(el("div", { class: "sec-title", style: "margin-top:18px" }, "To the common models you imported"));
  if (!form.imports.length) {
    main.append(note("none imported — pick one under DATASETS"));
    return;
  }
  for (const imp of form.imports) {
    const b = state.bundles.find((x) => x.name === imp.bundle);
    if (!b) continue;
    main.append(el("div", { class: "mf-card" }, ...importControls(b, imp)));
  }
}

/* The heart of the redesign, said plainly: what the current relations add up
   to. One group is an ordinary fact model; several means the measures are
   answered separately and merged on the dimensions they share. */
function renderFactTableSummary(main) {
  const groups = components();
  if (!form.datasets.length) return;
  const box = el("div", { class: "mf-picked" });
  if (groups.length === 1) {
    box.append(el("span", { class: "ok" }, "✓"),
      ` one fact table: ${groups[0].map((d) => d.name).join(" + ")}`);
    main.append(box);
    return;
  }
  box.append(el("span", { class: "ok" }, "◈"),
    ` ${groups.length} separate fact tables · `
    + groups.map((g) => g.map((d) => d.name).join(" + ")).join("  |  "));
  main.append(box, note("these are never joined to each other — each answers the query on its own and "
    + "the results merge on the dimensions they share, so no measure inflates. They can only be grouped "
    + "by dimensions all of them offer, which is what importing the same common model into each buys "
    + "you (a name they all declare natively works too)."));
  const shared = generated?.shared_dimensions;
  if (shared) {
    const chips = el("div", { class: "mf-locked-dims" },
      el("span", { class: "field-label" }, "GROUPABLE ACROSS ALL OF THEM"));
    if (!shared.length) {
      chips.append(el("span", { class: "mf-colcount" },
        "nothing yet — only grand totals will line up until they share a dimension"));
    }
    for (const n of shared) {
      chips.append(el("span", { class: "chip on" },
        el("span", { class: "tick" }, "✓"), el("span", { class: "lbl" }, n)));
    }
    main.append(chips);
  }
}

function datasetRelCard(r, idx) {
  const card = el("div", { class: "mf-card" });
  const endpoint = (val, set) => {
    const sel = el("select", {}, ...form.datasets.map((d) => el("option", { value: d.name }, d.name)));
    sel.value = val;
    sel.addEventListener("change", () => { set(sel.value); markDirty(); render(); });
    return sel;
  };
  const how = el("select", {},
    el("option", { value: "left" }, "keep all rows"),
    el("option", { value: "inner" }, "matching rows only"));
  how.value = r.how;
  how.addEventListener("change", () => { r.how = how.value; markDirty(); });
  const rm = el("button", { class: "rm", title: "remove relation" }, "✕");
  rm.addEventListener("click", () => { form.rels.splice(idx, 1); markDirty(); render(); });
  card.append(el("div", { class: "mf-card-head" },
    endpoint(r.from, (v) => { r.from = v; }), el("span", { class: "mf-link" }, "⇄"),
    endpoint(r.to, (v) => { r.to = v; }), how, el("span", { class: "mf-colcount" }, ""), rm));
  card.append(el("div", { class: "field-label", style: "margin-top:8px" }, `RELATION · ${r.from} ⇄ ${r.to}`));
  r.pairs.forEach((p, pi) => card.append(pairRow(p, colsOf(dsByName(r.from)), colsOf(dsByName(r.to)), {
    leftPh: `${r.from} column`, rightPh: `${r.to} column`,
    onchange: () => { markDirty(); render(); }, oninput: markDirty,
    onremove: () => { r.pairs.splice(pi, 1); markDirty(); render(); },
  })));
  const addPair = el("button", { class: "ghost" }, "+ relate another column pair");
  addPair.addEventListener("click", () => { r.pairs.push({ left: "", right: "" }); markDirty(); render(); });
  card.append(addPair);
  return card;
}

// default relation guess for a freshly-imported bundle: its anchor's first
// declared dimension, mirrored on the model side when a column name matches
function guessPair(anchorDs, fromName) {
  const right = anchorDs.dimensions[0] || "";
  const mine = fromName ? columnsFor(fromName) : [];
  const left = mine.some((c) => c.name === right) ? right : "";
  return { left, right };
}

const newImport = (b, anchorDs) => {
  const from = form.datasets.length ? form.datasets[0].name : "";
  return {
    bundle: b.name, from, anchor: anchorDs.name, datasets: null, how: "left",
    pairs: [guessPair(anchorDs, from)], interval: { start: "", end: "", point: "" }, match: "overlap",
  };
};

/* Column picker for the three keys of an interval import. Degrades to a plain
   text input when the schema behind it is unreachable, like pairRow does. */
function columnSelect(value, cols, placeholder, set) {
  if (!cols || !cols.length) {
    const input = el("input", { value, placeholder, spellcheck: "false" });
    input.addEventListener("input", () => { set(input.value); markDirty(); });
    return input;
  }
  const sel = el("select", {}, el("option", { value: "" }, `— ${placeholder} —`));
  if (value && !cols.some((c) => c.name === value)) sel.append(el("option", { value }, value));
  for (const c of cols) sel.append(el("option", { value: c.name }, `${c.name} · ${c.dtype}`));
  sel.value = value;
  sel.addEventListener("change", () => { set(sel.value); render(); });
  return sel;
}

function intervalControls(b, imp, anchorDs) {
  const iv = imp.interval;
  const mine = imp.from ? columnsFor(imp.from) : [];
  const theirs = anchorDs && colsOf(anchorDs);
  const out = [el("div", { class: "field-label", style: "margin-top:8px" },
    `DATE RANGE · ${imp.from || "?"} rows ⊇ ${b.name}.${imp.anchor}`)];
  out.push(el("div", { class: "mf-pair" },
    columnSelect(iv.start, mine, "start column", (v) => { iv.start = v; markDirty(); }),
    el("span", { class: "mf-link" }, "→"),
    columnSelect(iv.end, mine, "end column", (v) => { iv.end = v; markDirty(); }),
    el("span", { class: "mf-link" }, "⊇"),
    columnSelect(iv.point, theirs, "their date column", (v) => { iv.point = v; markDirty(); })));
  out.push(note("a row of that dataset is counted in every period it was open for — an empty end means "
    + "still open. Group by the imported date, or by its month/quarter/year columns, for point-in-time "
    + "totals. The imported table is narrowed to one row per period at whatever grain the query asks "
    + "for, so totals stay correct as the grain changes."));
  out.push(matchRow(imp, (v) => { imp.match = v; markDirty(); render(); }));

  if (!(iv.start && iv.end && iv.point)) {
    out.push(el("div", { class: "mf-warn" }, "⚠ pick all three columns to complete this relation"));
  }
  return out;
}

const JOIN_MODES = [
  ["left", "matching columns", "each row of the dataset picks up the shared row with the same key"],
  ["between", "a date range", "each row of the dataset is counted in every period the imported table "
    + "holds between its start and end columns — point-in-time aggregation, and the way to use a "
    + "calendar table that relates to nothing else"],
];

function importControls(b, imp) {
  const out = [];
  const fromSel = el("select", {}, el("option", { value: "" }, "— pick a dataset —"),
    ...form.datasets.map((d) => el("option", { value: d.name }, d.name)));
  fromSel.value = imp.from || "";
  fromSel.addEventListener("change", () => {
    imp.from = fromSel.value;
    imp.pairs = [guessPair(bundleDataset(b.name, imp.anchor), imp.from)];
    markDirty();
    render();
  });
  const anchorSel = el("select", {}, ...b.datasets.map((d) => el("option", { value: d.name }, d.name)));
  anchorSel.value = imp.anchor;
  anchorSel.addEventListener("change", async () => {
    imp.anchor = anchorSel.value;
    imp.pairs = [guessPair(bundleDataset(b.name, imp.anchor), imp.from)];
    imp.interval = { ...imp.interval, point: "" };
    markDirty();
    await anchorSchema(imp);
    render();
  });
  const rm = el("button", { class: "rm", title: "drop this import" }, "✕");
  rm.addEventListener("click", () => {
    form.imports = form.imports.filter((i) => i !== imp);
    markDirty();
    render();
  });
  out.push(el("div", { class: "mf-card-head" },
    el("span", { class: "nm" }, b.label), fromSel, el("span", { class: "mf-link" }, "⇄"), anchorSel, rm));
  out.push(el("div", { class: "mf-anchor-row" },
    el("span", { class: "field-label" }, "MY DATASET ⇄ THEIR ANCHOR DATASET"),
    el("span", { class: "mf-colcount" },
      "which of this model's datasets relates to the common model, and to which of its tables")));

  const modeSel = el("select", {}, ...JOIN_MODES.map(([v, lbl]) => el("option", { value: v }, lbl)));
  modeSel.value = imp.how === "between" ? "between" : "left";
  modeSel.addEventListener("change", () => { imp.how = modeSel.value; markDirty(); render(); });
  out.push(el("div", { class: "mf-anchor-row" },
    el("span", { class: "field-label" }, "RELATE ON"), modeSel,
    el("span", { class: "mf-colcount" }, JOIN_MODES.find(([v]) => v === modeSel.value)[2])));

  const anchorDs = bundleDataset(b.name, imp.anchor);
  if (!imp.from) {
    out.push(el("div", { class: "mf-warn" }, "⚠ pick the dataset this common model relates to"));
    return out;
  }
  if (imp.how === "between") {
    out.push(...intervalControls(b, imp, anchorDs));
  } else {
    out.push(el("div", { class: "field-label", style: "margin-top:8px" },
      `RELATION · ${imp.from} ⇄ ${b.name}.${imp.anchor}`));
    imp.pairs.forEach((p, pi) => out.push(pairRow(p, columnsFor(imp.from), anchorDs && colsOf(anchorDs), {
      leftPh: `${imp.from} column`, rightPh: "their column",
      onchange: () => { markDirty(); render(); }, oninput: markDirty,
      onremove: () => { imp.pairs.splice(pi, 1); markDirty(); render(); },
    })));
    const addPair = el("button", { class: "ghost" }, "+ relate another column pair");
    addPair.addEventListener("click", () => { imp.pairs.push({ left: "", right: "" }); markDirty(); render(); });
    out.push(addPair);
  }
  return out;
}

// ── section: DIMENSIONS (per dataset, like the common-model form) ──

function renderDimSection(main) {
  main.append(el("div", { class: "sec-title" }, "This model's dimensions"));
  if (generated && !generated.ok) main.append(el("div", { class: "mf-warn" }, "⚠ " + generated.error));
  else if (generated?.schema_error) main.append(el("div", { class: "mf-warn" }, "⚠ " + generated.schema_error));
  main.append(note("refine each dataset's dimensions — label, type and synonyms (synonyms help Chat "
    + "match plain-language questions). Datasets related to each other read one joined frame, so a "
    + "dimension may name any column its fact table brings in."));
  if (!form.datasets.length) {
    main.append(note("add a dataset first (DATASETS section)"));
    return;
  }
  for (const d of form.datasets) {
    main.append(el("div", { class: "sec-title", style: "margin-top:12px" }, `${d.name} · ${d.path}`));
    const box = el("div", { class: "mf-dims" });
    main.append(box);
    renderDatasetDims(box, d);
  }
}

function renderDatasetDims(box, d) {
  box.innerHTML = "";
  const cols = columnsFor(d.name);
  const own = colsOf(d) || [];
  if (!cols.length && !d.dimensions.length) {
    box.append(note("no readable columns — the source may be unreachable; add dimensions via EDIT YAML"));
    return;
  }
  const rows = el("div", { class: "mf-dim-rows" });
  const known = new Set(cols.map((c) => c.name));
  d.dimensions.forEach((dim, idx) => {
    const colName = dim.column || dim.name;
    const dtype = cols.find((c) => c.name === colName)?.dtype || "?";
    const row = el("div", { class: "mf-dim-row on" + (dim.spine ? " spine" : "") });
    const rm = el("button", { class: "rm", title: "remove dimension" }, "✕");
    rm.addEventListener("click", () => { d.dimensions.splice(idx, 1); markDirty(); render(); });
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
        el("span", { class: "hint" }, dim.spine ? "generated timeline"
          : known.has(colName) ? dtype : "column not in scan")),
      label, type, grainSelect(dim),
      synonymsInput(dim.synonyms || (dim.synonyms = []), markDirty));
    if (dim.geo) row.append(el("span", { class: "mf-colcount" }, "◎ geo"));
    row.append(rm);
    if (dim.spine) row.append(spineFields(dim, cols, markDirty));
    rows.append(row);
  });
  box.append(rows);

  // this dataset's own columns that nothing in its fact table has claimed yet
  const taken = takenIn(d.name);
  const addable = own.filter((c) => !taken.has(c.name));
  if (addable.length) {
    const grid = el("div", { class: "mf-import-grid", style: "margin-top:6px" });
    for (const c of addable) {
      const chip = el("button", { class: "chip" },
        el("span", { class: "tick" }, "+"), el("span", { class: "lbl" }, c.name), el("span", { class: "hint" }, c.dtype));
      chip.addEventListener("click", () => { d.dimensions.push(dimFromColumn(c)); markDirty(); renderDatasetDims(box, d); });
      grid.append(chip);
    }
    box.append(grid);
  }

  const imported = [...taken].filter(([, from]) => from !== d.name && !dsByName(from));
  if (imported.length) {
    const locked = el("div", { class: "mf-locked-dims" },
      el("span", { class: "field-label" }, "IMPORTED HERE · read-only"));
    for (const [n, owner] of imported) {
      locked.append(el("span", { class: "chip taken", title: `managed in common model '${owner}'` },
        el("span", { class: "tick" }, "◈"), el("span", { class: "lbl" }, n),
        el("span", { class: "hint" }, owner)));
    }
    box.append(locked);
  }

  if (cols.length) {
    if (form.spineFor === d) {
      box.append(spineCreatePanel(cols, {
        onapply: (dim) => { d.dimensions.push(dim); form.spineFor = null; markDirty(); renderDatasetDims(box, d); },
        ondismiss: () => { form.spineFor = null; renderDatasetDims(box, d); },
      }));
    } else {
      const btn = el("button", { class: "ghost", style: "margin-top:6px" },
        "+ create time-spine dimension (for point-in-time \"active\" measures)");
      btn.addEventListener("click", () => { form.spineFor = d; renderDatasetDims(box, d); });
      box.append(btn);
    }
  }
}

/* A column's `grain:` — the bucket it stays constant across. It only matters
   for a date table imported with `how: between`. Blank (the default) means the
   table's own row grain. */
const GRAINS = [["", "— per row —"], ["1d", "a day"], ["1w", "a week"],
                ["1mo", "a month"], ["1q", "a quarter"], ["1y", "a year"]];

function grainSelect(dim) {
  const sel = el("select", {
    class: "grain",
    title: "date tables only: the period this column is constant across, so an interval "
      + "(how: between) import can narrow to one row per period",
  }, ...GRAINS.map(([v, lbl]) => el("option", { value: v }, lbl)));
  sel.value = dim.grain || "";
  sel.addEventListener("change", () => { dim.grain = sel.value || null; markDirty(); });
  return sel;
}

// ── section: MEASURES (per dataset — a measure belongs to one fact table) ──

const FRAME_TEMPLATE =
  `frame = (\n    lf.group_by(dims)\n    .agg(pl.col("...").sum())\n)`;

const blankMeasure = () => ({ name: "", label: "", expr: "", format: "number", description: "", synonyms: [] });
const blankFramedMeasure = () =>
  ({ name: "", label: "", expr: "", format: "number", description: "", synonyms: [], frame: FRAME_TEMPLATE, frame_emits: [] });

// the one place that decides whether a measure counts as "framed" — blank
// frame text (e.g. cleared by the author) reverts it to a plain measure
// rather than saving/rendering it as an empty, invisible frame
const hasFrame = (m) => !!(m.frame && m.frame.trim());

// combined completion pool for a measure's expr: the columns of the fact table
// that owns it, plus its sibling measure names — a bare identifier is one or
// the other depending on whether the expr turns out to be a window measure
// (running_total()/lag()), which the client can't know until it parses
function exprColumns(owner) {
  const cols = columnsFor(owner.name);
  const names = new Set(cols.map((c) => c.name));
  const siblings = componentOf(owner.name)
    .flatMap((d) => d.measures.map((m) => m.name))
    .filter((n) => n && !names.has(n));
  return [...cols, ...siblings.map((n) => ({ name: n, dtype: "measure" }))];
}

// ── live per-row validation (POST /api/measures/check) ──

const checkTimers = new WeakMap();
function scheduleCheck(m, owner, statusEl) {
  clearTimeout(checkTimers.get(m));
  checkTimers.set(m, setTimeout(() => runCheck(m, owner, statusEl), 400));
}

async function runCheck(m, owner, statusEl) {
  const framed = hasFrame(m);
  const hasBody = framed ? true : m.expr.trim();
  if (!m.name.trim() || !hasBody) { statusEl.innerHTML = ""; return; }
  statusEl.innerHTML = '<span class="pending">checking…</span>';
  const body = {
    expr: m.expr || "",
    frame: framed ? m.frame : null,
    frame_emits: framed ? (m.frame_emits || []) : [],
    columns: columnsFor(owner.name).map((c) => c.name),
    measure_names: componentOf(owner.name)
      .flatMap((d) => d.measures.map((x) => x.name))
      .filter((n) => n && n !== m.name),
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

// dimensions the fact table has declared so far, offered as frame_emits
// candidates (frame_emits names dimension(s) the frame recomputes itself —
// e.g. a per-entity milestone date — see subscriptions.yaml's
// median_tenure_days)
function frameEmitsPicker(m, owner, redraw) {
  const wrap = el("div", { class: "mf-subset" });
  const dims = componentOf(owner.name).flatMap((d) => d.dimensions);
  if (!dims.length) {
    wrap.append(note("declare a dimension above to offer it here, or type its name once the frame computes it"));
    return wrap;
  }
  for (const d of dims) {
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
function exprEditor(m, owner, statusEl, { rows = 1, cls = "mf-expr" } = {}) {
  const wrap = el("div", { class: "mf-expr-wrap" });
  // a framed measure's expr aggregates the frame with polars syntax, not the DSL
  const ph = hasFrame(m) ? 'pl.col("...").median()' : "mean(unit_price)";
  const ta = el("textarea", { class: cls, rows: String(rows), spellcheck: "false", placeholder: ph });
  ta.value = m.expr;
  const suggest = el("div", { class: "mf-suggest" });
  suggest.hidden = true;
  const completer = makeCompleter(ta, suggest, (upto, after, caret) => {
    const ctx = dslContext(upto, caret);
    return ctx ? { items: dslItems(ctx, exprColumns(owner), after), start: ctx.start } : null;
  }, () => scheduleCheck(m, owner, statusEl));
  ta.addEventListener("input", () => { m.expr = ta.value; markDirty(); completer.update(); scheduleCheck(m, owner, statusEl); });
  ta.addEventListener("keydown", (e) => completer.onKeydown(e));
  ta.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
  autoGrow(ta);
  wrap.append(ta, suggest);
  return wrap;
}

/* Frame editor (complex measures): python-ish escape hatch; only the
   col("...") trigger completes inside it. */
function frameEditor(m, owner, statusEl) {
  const wrap = el("div", { class: "mf-expr-wrap" });
  const ta = el("textarea", { class: "mf-frame", rows: "7", spellcheck: "false" });
  ta.value = m.frame || "";
  const suggest = el("div", { class: "mf-suggest" });
  suggest.hidden = true;
  const completer = makeCompleter(ta, suggest, (upto, after, caret) => {
    const ctx = dslContext(upto, caret);
    return ctx && ctx.kind === "col" ? { items: dslItems(ctx, exprColumns(owner), after), start: ctx.start } : null;
  });
  ta.addEventListener("input", () => { m.frame = ta.value; markDirty(); completer.update(); scheduleCheck(m, owner, statusEl); });
  ta.addEventListener("keydown", (e) => completer.onKeydown(e));
  ta.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
  wrap.append(ta, suggest);
  return wrap;
}

function measureCard(m, owner, idx, box) {
  const isFramed = hasFrame(m);
  const card = el("div", { class: "mf-measure-card" + (isFramed ? " framed" : "") });
  const status = el("div", { class: "mf-measure-status" });

  const name = el("input", { value: m.name, placeholder: "measure_name", spellcheck: "false" });
  name.addEventListener("input", () => { m.name = name.value; markDirty(); scheduleCheck(m, owner, status); });
  const label = el("input", { value: m.label, placeholder: "Label", spellcheck: "false" });
  label.addEventListener("input", () => { m.label = label.value; markDirty(); });
  const fmt = el("select", {}, ...["number", "currency", "percent"].map((f) => el("option", { value: f }, f)));
  fmt.value = m.format;
  fmt.addEventListener("change", () => { m.format = fmt.value; markDirty(); });
  const expand = el("button", { class: "btn plain", title: "open the full measure editor" }, "⤢ EXPAND");
  expand.addEventListener("click", () => openMeasureModal(m, owner, box));
  const rm = el("button", { class: "rm", title: "remove measure" }, "✕");
  rm.addEventListener("click", () => { owner.measures.splice(idx, 1); markDirty(); renderMeasures(box, owner); });

  const head = el("div", { class: "mf-measure" }, name, label, fmt,
    ...(isFramed ? [el("span", { class: "mf-badge" }, "⚡ COMPLEX")] : []), expand, rm);
  card.append(head);

  if (isFramed) {
    card.append(el("div", { class: "field-label", style: "margin-top:6px" }, "FRAME · derived step ahead of the aggregation (open the full editor for guidance)"));
    card.append(frameEditor(m, owner, status));
    card.append(el("div", { class: "field-label", style: "margin-top:8px" }, "EXPR · aggregates the frame's own output columns"));
    card.append(exprEditor(m, owner, status));
  } else {
    card.append(exprEditor(m, owner, status));
  }

  const synRow = el("div", { class: "mf-syn-row" },
    el("span", { class: "field-label" }, "SYNONYMS"),
    synonymsInput(m.synonyms || (m.synonyms = []), markDirty));
  card.append(synRow, status);
  scheduleCheck(m, owner, status);
  return card;
}

function renderMeasureSection(main) {
  main.append(el("div", { class: "sec-title" }, "Measures"));
  main.append(note("safe DSL expressions (e.g. sum(revenue), mean(price)) — every measure reduces to one "
    + "value per group. A measure belongs to the dataset it's declared on, which is what scopes it to "
    + "one fact table; names have to be unique across the whole model. Use ⤢ EXPAND for the full editor "
    + "with a function reference; complex measures add a derived-frame step ahead of their aggregation."));
  if (!form.datasets.length) {
    main.append(note("add a dataset first (DATASETS section)"));
    return;
  }
  for (const d of form.datasets) {
    main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, `${d.name} · ${d.path}`));
    const box = el("div");
    main.append(box);
    renderMeasures(box, d);
  }
}

function renderMeasures(box, owner) {
  box.innerHTML = "";
  owner.measures.forEach((m, idx) => box.append(measureCard(m, owner, idx, box)));
  const add = el("button", { class: "ghost" }, "+ add measure");
  add.addEventListener("click", () => { owner.measures.push(blankMeasure()); markDirty(); renderMeasures(box, owner); });
  const addFramed = el("button", { class: "ghost" }, "+ add complex measure (frame)");
  addFramed.addEventListener("click", () => {
    const m = blankFramedMeasure();
    owner.measures.push(m);
    markDirty();
    renderMeasures(box, owner);
    openMeasureModal(m, owner, box);   // complex measures deserve the full editor
  });
  box.append(el("div", { class: "mf-quick-slot" }), el("div", { class: "mf-measure-actions" }, add, addFramed));
  renderQuickAdd(box, owner);
}

function renderQuickAdd(box, owner) {
  const slot = box.querySelector(".mf-quick-slot");
  const cols = columnsFor(owner.name).filter((c) => /int|float|decimal/i.test(c.dtype));
  if (!slot || !cols.length) return;
  const colSel = el("select", {}, ...cols.map((c) => el("option", { value: c.name }, c.name)));
  const aggSel = el("select", {}, ...Object.keys(AGGS).map((a) => el("option", { value: a }, a)));
  const add = el("button", { class: "btn plain" }, "+ QUICK ADD");
  add.addEventListener("click", () => {
    const c = colSel.value, a = aggSel.value;
    owner.measures.push({
      name: `${c}_${a}`, label: "", format: "number", description: "", synonyms: [],
      expr: `${AGGS[a]}(${c})`,
    });
    markDirty();
    renderMeasures(box, owner);
  });
  slot.className = "mf-quick";
  slot.innerHTML = "";
  slot.append(el("span", { class: "field-label" }, "QUICK ADD"), colSel, aggSel, add);
}

// ── expanded measure editor (modal) ──

let modalMeasure = null;   // measure being edited in the modal
let modalOwner = null;     // dataset that declares it
let modalListBox = null;   // measures container to re-render on close

function closeMeasureModal() {
  $("#measure-modal").hidden = true;
  $("#measure-modal").innerHTML = "";
  modalMeasure = null;
  modalOwner = null;
  modalListBox = null;
}

// close + mirror the modal's edits back into the measure list
function dismissMeasureModal() {
  const box = modalListBox;
  const owner = modalOwner;
  closeMeasureModal();
  if (box && box.isConnected) renderMeasures(box, owner);
  renderChrome();
}

function openMeasureModal(m, owner, listBox) {
  modalMeasure = m;
  modalOwner = owner;
  modalListBox = listBox;
  drawMeasureModal();
  $("#measure-modal").hidden = false;
}

function drawMeasureModal() {
  const m = modalMeasure;
  const owner = modalOwner;
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
  name.addEventListener("input", () => { m.name = name.value; markDirty(); scheduleCheck(m, owner, status); });
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
      frameEditor(m, owner, status),
      el("div", { class: "field-label", style: "margin-top:10px" }, "FRAME_EMITS · dimensions the frame computes itself"),
      frameEmitsPicker(m, owner, drawMeasureModal),
      el("div", { class: "field-label", style: "margin-top:10px" }, "EXPR · aggregates the frame's own output columns"),
      exprEditor(m, owner, status, { rows: 2, cls: "mf-expr mm-expr" }));
  } else {
    editorCol.append(
      el("div", { class: "field-label" }, "EXPRESSION"),
      exprEditor(m, owner, status, { rows: 4, cls: "mf-expr mm-expr" }));
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
      el("span", { class: "editor-file" }, `measure editor · ${owner.name}`),
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
  scheduleCheck(m, owner, status);
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
    const parts = generated.parts || [];
    report.innerHTML = `<span class="ok">✓ valid</span> — <b>${generated.model.label}</b> (${generated.model.name}) · `
      + `${parts.length} fact table${parts.length === 1 ? "" : "s"} · `
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

async function deleteModelForm() {
  if (!form.editingName || form.locked) return;
  if (!confirm(`Delete model '${form.label || form.editingName}'? Saved visuals pointing at it will stop working.`)) return;
  try {
    await api(`/api/models/${form.editingName}`, { method: "DELETE" });
  } catch (err) {
    setStatus(`<span class="err">✗ ${err.message}</span>`);
    return;
  }
  form.dirty = false;
  await refreshModels();
  navigate(paths.modelling());
}

// hand the current form state to the raw YAML editor — the escape hatch for
// anything the form does not surface (geo pairs, exotic expressions)
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
  $("#mf-delete").addEventListener("click", deleteModelForm);
  $("#mf-yaml").addEventListener("click", editAsYaml);
  $("#mf-build").addEventListener("click", () => {
    if (form.editingName) navigate(paths.studioModel(form.editingName));
  });
  $("#mf-memory").addEventListener("click", () => {
    if (form.editingName) {
      openMemoriesModal({
        name: form.editingName, label: form.label,
        dimensions: allDimensions(), measures: allMeasures(),
      });
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
