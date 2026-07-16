/* Guided bundle form: the default way to create/edit a common (shared-
   dimension) model — the bundle counterpart of modelform.js, and the same
   editing architecture: a sectioned editor (Overview / Datasets / Relations /
   Dimensions / YAML) with free navigation, continuous debounced validation
   (POST /api/dimensions/generate) and an always-visible SAVE. Raw YAML
   editing stays one click away (editor.js). Form state is ephemeral until
   SAVE (Constitution V). */
"use strict";

import { refreshModels } from "./builder.js";
import { openEditor } from "./editor.js";
import {
  colsOf, columnImportPanel, datasetCards, dimFromColumn, loadDatasets,
  manualPathRow, NAME_RE, note, pairRow, sectionRail, sourceSchema, synonymsInput, textField,
} from "./formkit.js";
import { $, api, el } from "./lib.js";
import { navigate, paths, setPath } from "./router.js";
import { hooks, showView, state } from "./state.js";

const SECTIONS = [
  { id: "overview", label: "OVERVIEW" },
  { id: "datasets", label: "DATASETS" },
  { id: "relations", label: "RELATIONS" },
  { id: "dimensions", label: "DIMENSIONS" },
  { id: "yaml", label: "YAML" },
];

const form = {
  editingName: null,   // name of the existing bundle being edited (null = new)
  section: "overview",
  dirty: false,
  name: "", label: "", description: "",
  datasets: [],        // {name, path, format, dimensions:[spec dicts]}
  rels: [],            // {from, to, how, pairs:[{left,right}]} — flattened DatasetJoins
  importFor: null,     // dataset object whose column-import panel is open (transient)
};
let generated = null;  // last /api/dimensions/generate response
let genToken = 0;

const setStatus = (html) => { $("#bf-status").innerHTML = html; };
const markDirty = () => { form.dirty = true; scheduleGenerate(); };
const dsByName = (name) => form.datasets.find((d) => d.name === name);

function toSpec() {
  return {
    name: form.name.trim(), label: form.label.trim(), description: form.description.trim(),
    datasets: form.datasets.map((d) => ({
      name: d.name,
      source: { path: d.path, format: d.format },
      dimensions: d.dimensions,
      joins: form.rels.filter((r) => r.from === d.name).map((r) => {
        const done = r.pairs.filter((p) => p.left && p.right);
        return { to: r.to, how: r.how,
                 left_on: done.map((p) => p.left), right_on: done.map((p) => p.right) };
      }),
    })),
  };
}

export function confirmLeaveBundleForm() {
  if (state.view !== "bundleform" || !form.dirty) return true;
  return confirm("Leave the common-model form? In-progress edits are not saved.");
}
hooks.confirmLeaveBundleForm = confirmLeaveBundleForm;

export async function openBundleForm(name) {
  if (!confirmLeaveBundleForm()) return;
  Object.assign(form, {
    editingName: name, section: "overview", dirty: false,
    name: "", label: "", description: "", datasets: [], rels: [], importFor: null,
  });
  generated = null;
  showView("bundleform");
  $("#bf-title").textContent = name ? `edit common model · ${name}` : "new common model";
  setStatus(name ? "loading…" : "");
  render();
  await loadDatasets();
  if (name) {
    const { spec } = await api(`/api/dimensions/${name}/spec`);
    Object.assign(form, {
      name: spec.name, label: spec.label, description: spec.description,
      datasets: spec.datasets.map((d) => ({
        name: d.name, path: d.source.path, format: d.source.format, dimensions: d.dimensions,
      })),
      rels: spec.datasets.flatMap((d) => d.joins.map((j) => ({
        from: d.name, to: j.to, how: j.how,
        pairs: j.left_on.map((l, idx) => ({ left: l, right: j.right_on[idx] ?? l })),
      }))),
    });
    setStatus("");
    await Promise.all(form.datasets.map((d) => sourceSchema(d.path, d.format)));
  }
  render();
  scheduleGenerate();
}
hooks.openBundleForm = openBundleForm;

// ── section problems (inline guidance, never a navigation gate) ────────────

function sectionProblem(id) {
  if (id === "overview" && !NAME_RE.test(form.name.trim())) {
    return "common-model name must be snake_case (a-z, 0-9, _)";
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
  if (id === "datasets") return form.datasets.length ? "done" : "";
  if (id === "relations") return form.rels.length ? "done" : "";
  if (id === "dimensions") return form.datasets.some((d) => d.dimensions.length) ? "done" : "";
  return "";
}

// ── continuous validation (debounced /api/dimensions/generate) ────────────

let genTimer = null;
function scheduleGenerate() {
  clearTimeout(genTimer);
  genTimer = setTimeout(runGenerate, 500);
  renderChrome();
}

async function runGenerate() {
  if (state.view !== "bundleform") return;
  if (firstProblem()) { generated = null; renderChrome(); return; }
  const token = ++genToken;
  const res = await api("/api/dimensions/generate", { method: "POST", body: toSpec() })
    .catch((e) => ({ ok: false, error: e.message }));
  if (token !== genToken || state.view !== "bundleform") return;
  generated = res;
  renderChrome();
  if (form.section === "yaml") renderYamlInto($("#bf-main"));
}

function renderChrome() {
  sectionRail($("#bf-steps"), SECTIONS, form.section, sectionStatus, (id) => { form.section = id; render(); });
  const hint = $("#bf-hint");
  const live = $("#bf-live");
  const p = firstProblem();
  if (p) {
    hint.textContent = p.problem;
    live.innerHTML = "";
  } else {
    hint.textContent = "";
    if (!generated) live.innerHTML = '<span class="pending">validating…</span>';
    else if (generated.ok) {
      const b = generated.bundle;
      const totalDims = b.datasets.reduce((s, x) => s + x.dimensions, 0);
      const warns = b.datasets.filter((x) => x.schema_error).length;
      live.innerHTML = `<span class="ok">✓ valid</span> · ${b.datasets.length} dataset${b.datasets.length === 1 ? "" : "s"}`
        + ` · ${totalDims} shared dimension${totalDims === 1 ? "" : "s"}`
        + (warns ? ' · <span class="warn">⚠ source unreachable</span>' : "");
    } else {
      live.innerHTML = `<span class="err">✗ ${generated.error}</span>`;
    }
  }
  $("#bf-save").disabled = !generated?.ok;
}

// ── rendering ──────────────────────────────────────────────────────────────

function render() {
  renderChrome();
  const main = $("#bf-main");
  main.innerHTML = "";
  ({ overview: renderOverview, datasets: renderDatasets, relations: renderRels,
    dimensions: renderDims, yaml: renderYamlInto })[form.section](main);
}

const field = (label, value, set, ph) => textField(label, value, (v) => { set(v); markDirty(); }, ph);

// ── section: OVERVIEW ──

function renderOverview(main) {
  main.append(el("div", { class: "sec-title" }, "What a common model is"));
  main.append(note("a common dimension model declares shared dimensions (geography, calendars, org "
    + "hierarchies…) once — any fact model can import them, and they stay consistent everywhere. "
    + "Common models have no measures."));
  main.append(el("div", { class: "mf-row3" },
    field("NAME (snake_case)", form.name, (v) => { form.name = v; }, "my_dimensions"),
    field("LABEL", form.label, (v) => { form.label = v; }, "My Dimensions"),
    field("DESCRIPTION", form.description, (v) => { form.description = v; }, "Shared dimensions imported by fact models.")));
}

// ── section: DATASETS ──

function renderDatasets(main) {
  main.append(el("div", { class: "sec-title" }, "Datasets in this common model"));
  main.append(note("the reference tables this common model bundles — each declares dimensions any fact model can import"));
  form.datasets.forEach((d, idx) => {
    const nameIn = el("input", { value: d.name, spellcheck: "false", class: "mf-join-name" });
    nameIn.addEventListener("input", () => {
      // renaming a dataset follows through to the relations that touch it
      for (const r of form.rels) {
        if (r.from === d.name) r.from = nameIn.value;
        if (r.to === d.name) r.to = nameIn.value;
      }
      d.name = nameIn.value;
      markDirty();
    });
    const rm = el("button", { class: "rm", title: "remove dataset" }, "✕");
    rm.addEventListener("click", () => {
      form.datasets.splice(idx, 1);
      form.rels = form.rels.filter((r) => r.from !== d.name && r.to !== d.name);
      if (form.importFor === d) form.importFor = null;
      markDirty();
      render();
    });
    const cols = colsOf(d);
    const card = el("div", { class: "mf-card" },
      el("div", { class: "mf-card-head" },
        nameIn, el("span", { class: "fmt" }, d.format),
        el("span", { class: "mf-colcount" }, cols ? `${cols.length} columns · ${d.dimensions.length} dims` : "columns not readable yet"),
        rm),
      el("div", { class: "path" }, d.path));
    appendColumnImport(card, d, cols);
    main.append(card);
  });

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "Add a dataset"));
  main.append(datasetCards((ds) => addDataset(ds), null));
  main.append(manualPathRow(null, (src) => addDataset({ key: src.path, ...src })));
}

/* Column-import affordance for one bundle dataset: a fresh add opens the
   panel automatically (all columns selected), a ghost button reopens it. */
function appendColumnImport(card, d, cols) {
  if (!cols || !cols.length) return;
  const taken = d.dimensions.map((x) => x.column || x.name);
  const open = cols.filter((c) => !taken.includes(c.name));
  if (form.importFor === d) {
    card.append(columnImportPanel(cols, taken, {
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
  const rec = { name, path: ds.path, format: ds.format, dimensions: [] };
  form.datasets.push(rec);
  markDirty();
  render();
  await sourceSchema(ds.path, ds.format);
  form.importFor = rec;   // fresh dataset: offer its columns right away
  render();
}

// ── section: RELATIONS (between the bundle's datasets) ──

function renderRels(main) {
  main.append(el("div", { class: "sec-title" }, "Relations"));
  main.append(note("how the datasets in this common model relate to each other — importing any one of them "
    + "pulls in everything reachable through these. Column pairs don't need matching names."));
  if (form.datasets.length < 2) {
    main.append(note("nothing to relate — this common model has a single dataset"));
    return;
  }
  form.rels.forEach((r, idx) => {
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
    main.append(card);
  });
  const add = el("button", { class: "ghost" }, "+ add relation");
  add.addEventListener("click", () => {
    form.rels.push({ from: form.datasets[0].name, to: form.datasets[1].name, how: "left", pairs: [{ left: "", right: "" }] });
    markDirty();
    render();
  });
  main.append(add);
}

// ── section: DIMENSIONS (per dataset) ──

function renderDims(main) {
  main.append(el("div", { class: "sec-title" }, "Shared dimensions"));
  main.append(note("refine each dataset's dimensions — names must be unique across the whole common model "
    + "(a fact model imports them all into one namespace). Synonyms help Chat match plain-language questions."));
  for (const d of form.datasets) {
    main.append(el("div", { class: "sec-title", style: "margin-top:12px" }, `${d.name} · ${d.path}`));
    const box = el("div", { class: "mf-dims" });
    main.append(box);
    renderDatasetDims(box, d);
  }
  if (!form.datasets.length) main.append(note("add a dataset first (DATASETS section)"));
}

function renderDatasetDims(box, d) {
  box.innerHTML = "";
  const cols = colsOf(d) || [];
  if (!cols.length && !d.dimensions.length) {
    box.append(note("no readable columns — the source may be unreachable; add dimensions via EDIT YAML"));
    return;
  }
  const rows = el("div", { class: "mf-dim-rows" });
  d.dimensions.forEach((dim, idx) => {
    const colName = dim.column || dim.name;
    const dtype = cols.find((c) => c.name === colName)?.dtype || "?";
    const row = el("div", { class: "mf-dim-row on" });
    const rm = el("button", { class: "rm", title: "remove dimension" }, "✕");
    rm.addEventListener("click", () => { d.dimensions.splice(idx, 1); markDirty(); renderDatasetDims(box, d); });
    const label = el("input", { value: dim.label, placeholder: "Label", spellcheck: "false" });
    label.addEventListener("input", () => { dim.label = label.value; markDirty(); });
    const type = el("select", {}, ...["categorical", "time", "numeric"].map((t) => el("option", { value: t }, t)));
    type.value = dim.type;
    type.addEventListener("change", () => { dim.type = type.value; markDirty(); });
    row.append(
      el("span", { class: "chip on" }, el("span", { class: "tick" }, "✓"),
        el("span", { class: "lbl" }, colName), el("span", { class: "hint" }, dtype)),
      label, type,
      synonymsInput(dim.synonyms || (dim.synonyms = []), markDirty));
    if (dim.spine || dim.geo) row.append(el("span", { class: "mf-colcount" }, dim.spine ? "⧗ spine" : "◎ geo"));
    row.append(rm);
    rows.append(row);
  });
  box.append(rows);

  const declared = new Set(d.dimensions.map((x) => x.column || x.name));
  const addable = cols.filter((c) => !declared.has(c.name));
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
}

// ── section: YAML ──

function renderYamlInto(main) {
  if (form.section !== "yaml") return;
  main.innerHTML = "";
  main.append(el("div", { class: "sec-title" }, "Generated YAML"));
  const report = el("div", { class: "editor-report" });
  const pre = el("pre", { class: "mf-yaml" }, "");
  main.append(report, note(form.editingName
    ? `saving rewrites dimensions/${form.editingName}.yaml from this form (hand-written comments are not preserved)`
    : "saving writes a new file under dimensions/ — every fact model can then import it"), pre);
  const p = firstProblem();
  if (p) {
    report.innerHTML = `<span class="warn">⚠ ${p.problem}</span>`;
    return;
  }
  if (!generated) { report.textContent = "generating yaml…"; return; }
  pre.textContent = generated.yaml || "";
  if (generated.ok) {
    const b = generated.bundle;
    const totalDims = b.datasets.reduce((s, x) => s + x.dimensions, 0);
    const warns = b.datasets.filter((x) => x.schema_error).map((x) => `${x.name}: ${x.schema_error}`);
    report.innerHTML = `<span class="ok">✓ valid</span> — <b>${b.label}</b> (${b.name}) · `
      + `${b.datasets.length} dataset${b.datasets.length === 1 ? "" : "s"} · ${totalDims} shared dimensions`
      + warns.map((w) => `<br><span class="warn">⚠ ${w}</span>`).join("");
  } else {
    report.innerHTML = `<span class="err">✗ ${generated.error}</span>`;
  }
}

// ── save + wiring ──

async function saveBundleForm() {
  if (!generated?.ok) return;
  setStatus("saving…");
  try {
    const saved = form.editingName
      ? await api(`/api/dimensions/${form.editingName}/yaml`, { method: "PUT", body: { yaml: generated.yaml } })
      : await api("/api/dimensions", { method: "POST", body: { yaml: generated.yaml } });
    form.dirty = false;
    state.bundles = await api("/api/dimensions");   // importers may have re-resolved
    await refreshModels();
    navigate(paths.modelling());
    setStatus(`<span class="ok">saved ${saved.file} ✓</span>`);
  } catch (err) {
    setStatus(`<span class="err">✗ ${err.message}</span>`);
  }
}

async function editAsYaml() {
  setStatus("generating yaml…");
  const res = await api("/api/dimensions/generate", { method: "POST", body: toSpec() }).catch(() => null);
  setStatus("");
  form.dirty = false;   // the yaml editor takes over ownership of the edits
  openEditor("bundle", form.editingName, { text: res?.yaml });
  setPath(form.editingName ? paths.modellingBundleYaml(form.editingName) : paths.modellingNewBundleYaml());
}

export function attachBundleForm() {
  $("#bf-save").addEventListener("click", saveBundleForm);
  $("#bf-yaml").addEventListener("click", editAsYaml);
  $("#bf-back").addEventListener("click", () => {
    if (!confirmLeaveBundleForm()) return;
    form.dirty = false;
    navigate(paths.modelling());
  });
}
