/* Guided bundle form: the default way to create/edit a common (shared-
   dimension) model — the bundle counterpart of modelform.js. A stepper walks
   the author through datasets → relationships between them → dimensions,
   holding a structured spec the server renders to YAML
   (POST /api/dimensions/generate). Raw YAML editing stays one click away
   (editor.js). Form state is ephemeral until SAVE (Constitution V). */
"use strict";

import { refreshModels } from "./builder.js";
import { openEditor } from "./editor.js";
import {
  colsOf, datasetCards, loadDatasets, manualPathRow, NAME_RE, note, pairRow,
  sourceSchema, textField, titleCase,
} from "./formkit.js";
import { $, api, el } from "./lib.js";
import { navigate, paths, setPath } from "./router.js";
import { hooks, showView, state } from "./state.js";

const STEPS = ["NAME & DATASETS", "RELATIONSHIPS", "DIMENSIONS", "REVIEW & SAVE"];

const form = {
  editingName: null,   // name of the existing bundle being edited (null = new)
  step: 0,
  dirty: false,
  name: "", label: "", description: "",
  datasets: [],        // {name, path, format, dimensions:[spec dicts]}
  rels: [],            // {from, to, how, pairs:[{left,right}]} — flattened DatasetJoins
};
let generated = null;  // last /api/dimensions/generate response

const setStatus = (html) => { $("#bf-status").innerHTML = html; };
const markDirty = () => { form.dirty = true; };
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
    editingName: name, step: 0, dirty: false,
    name: "", label: "", description: "", datasets: [], rels: [],
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
}
hooks.openBundleForm = openBundleForm;

// ── rendering ──────────────────────────────────────────────────────────────

function render() {
  const rail = $("#bf-steps");
  rail.innerHTML = "";
  STEPS.forEach((label, idx) => {
    const btn = el("button", { class: "mf-step" + (idx === form.step ? " on" : "") + (idx < form.step ? " done" : "") },
      el("span", { class: "num" }, String(idx + 1)), label);
    btn.addEventListener("click", () => { if (idx <= form.step || !stepError()) { form.step = idx; render(); } });
    rail.append(btn);
  });
  const main = $("#bf-main");
  main.innerHTML = "";
  [renderDatasets, renderRels, renderDims, renderReview][form.step](main);
  const err = stepError();
  $("#bf-hint").textContent = err || "";
  $("#bf-prev").disabled = form.step === 0;
  $("#bf-next").hidden = form.step === STEPS.length - 1;
  $("#bf-next").disabled = !!err;
  $("#bf-save").hidden = form.step !== STEPS.length - 1;
  $("#bf-save").disabled = true;   // review re-enables once generate says ok
}

function stepError() {
  if (form.step === 0) {
    if (!NAME_RE.test(form.name.trim())) return "common-model name must be snake_case (a-z, 0-9, _)";
    if (!form.datasets.length) return "add at least one dataset";
    const seen = new Set();
    for (const d of form.datasets) {
      if (!NAME_RE.test(d.name)) return "every dataset needs a snake_case name";
      if (seen.has(d.name)) return `two datasets are both named '${d.name}'`;
      seen.add(d.name);
    }
  }
  if (form.step === 1) {
    for (const r of form.rels) {
      if (r.from === r.to) return "a relationship cannot join a dataset to itself";
      if (!r.pairs.some((p) => p.left && p.right)) return `${r.from} ⇄ ${r.to}: relate at least one column pair`;
    }
  }
  return null;
}

const field = (label, value, set, ph) => textField(label, value, (v) => {
  set(v);
  markDirty();
  $("#bf-hint").textContent = stepError() || "";
  $("#bf-next").disabled = !!stepError();
}, ph);

// ── step 1: NAME & DATASETS ──

function renderDatasets(main) {
  main.append(el("div", { class: "sec-title" }, "1 · Name"));
  main.append(el("div", { class: "mf-row3" },
    field("NAME (snake_case)", form.name, (v) => { form.name = v; }, "my_dimensions"),
    field("LABEL", form.label, (v) => { form.label = v; }, "My Dimensions"),
    field("DESCRIPTION", form.description, (v) => { form.description = v; }, "Shared dimensions imported by fact models.")));

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "2 · Datasets in this common model"));
  main.append(note("the reference tables this common model bundles — each declares dimensions any fact model can import"));
  form.datasets.forEach((d, idx) => {
    const nameIn = el("input", { value: d.name, spellcheck: "false", class: "mf-join-name" });
    nameIn.addEventListener("input", () => {
      // renaming a dataset follows through to the relationships that touch it
      for (const r of form.rels) {
        if (r.from === d.name) r.from = nameIn.value;
        if (r.to === d.name) r.to = nameIn.value;
      }
      d.name = nameIn.value;
      markDirty();
      $("#bf-hint").textContent = stepError() || "";
      $("#bf-next").disabled = !!stepError();
    });
    const rm = el("button", { class: "rm", title: "remove dataset" }, "✕");
    rm.addEventListener("click", () => {
      form.datasets.splice(idx, 1);
      form.rels = form.rels.filter((r) => r.from !== d.name && r.to !== d.name);
      markDirty();
      render();
    });
    const cols = colsOf(d);
    main.append(el("div", { class: "mf-card" },
      el("div", { class: "mf-card-head" },
        nameIn, el("span", { class: "fmt" }, d.format),
        el("span", { class: "mf-colcount" }, cols ? `${cols.length} columns · ${d.dimensions.length} dims` : "columns not readable yet"),
        rm),
      el("div", { class: "path" }, d.path)));
  });

  main.append(el("div", { class: "sec-title", style: "margin-top:14px" }, "Add a dataset"));
  main.append(datasetCards((ds) => addDataset(ds), null));
  main.append(manualPathRow(null, (src) => addDataset({ key: src.path, ...src })));
}

async function addDataset(ds) {
  const base = (ds.key.split("/").pop() || "dataset").replace(/\.[a-z0-9]+$/i, "")
    .replace(/[^a-z0-9_]+/gi, "_").toLowerCase() || "dataset";
  let name = base;
  for (let n = 2; dsByName(name); n++) name = `${base}_${n}`;
  form.datasets.push({ name, path: ds.path, format: ds.format, dimensions: [] });
  markDirty();
  render();
  await sourceSchema(ds.path, ds.format);
  render();
}

// ── step 2: RELATIONSHIPS (joins between the bundle's datasets) ──

function renderRels(main) {
  main.append(el("div", { class: "sec-title" }, "Relationships"));
  main.append(note("how the datasets in this common model join to each other — importing any one of them "
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
    const how = el("select", {}, ...["left", "inner"].map((h) => el("option", { value: h }, h + " join")));
    how.value = r.how;
    how.addEventListener("change", () => { r.how = how.value; markDirty(); });
    const rm = el("button", { class: "rm", title: "remove relationship" }, "✕");
    rm.addEventListener("click", () => { form.rels.splice(idx, 1); markDirty(); render(); });
    card.append(el("div", { class: "mf-card-head" },
      endpoint(r.from, (v) => { r.from = v; }), el("span", { class: "mf-link" }, "⇄"),
      endpoint(r.to, (v) => { r.to = v; }), how, el("span", { class: "mf-colcount" }, ""), rm));
    card.append(el("div", { class: "field-label", style: "margin-top:8px" }, `RELATIONSHIP · ${r.from} ⇄ ${r.to}`));
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
  const add = el("button", { class: "ghost" }, "+ add relationship");
  add.addEventListener("click", () => {
    form.rels.push({ from: form.datasets[0].name, to: form.datasets[1].name, how: "left", pairs: [{ left: "", right: "" }] });
    markDirty();
    render();
  });
  main.append(add);
}

// ── step 3: DIMENSIONS (per dataset) ──

function renderDims(main) {
  main.append(el("div", { class: "sec-title" }, "Dimensions"));
  main.append(note("tick the columns each dataset offers as shared dimensions — names must be unique across "
    + "the whole common model (a fact model imports them all into one namespace)"));
  for (const d of form.datasets) {
    main.append(el("div", { class: "sec-title", style: "margin-top:12px" }, `${d.name} · ${d.path}`));
    const box = el("div", { class: "mf-dims" });
    main.append(box);
    renderDatasetDims(box, d);
  }
}

function renderDatasetDims(box, d) {
  box.innerHTML = "";
  const cols = colsOf(d) || [];
  if (!cols.length) { box.append(note("no readable columns — the source may be unreachable; add dimensions via EDIT YAML")); }
  const known = new Set(cols.map((c) => c.name));
  const phantom = d.dimensions.filter((x) => !known.has(x.column || x.name));
  const rows = el("div", { class: "mf-dim-rows" });
  for (const c of [...cols, ...phantom.map((x) => ({ name: x.column || x.name, dtype: "?" }))]) {
    const dim = d.dimensions.find((x) => (x.column || x.name) === c.name);
    const row = el("div", { class: "mf-dim-row" + (dim ? " on" : "") });
    const tick = el("button", { class: "chip" + (dim ? " on" : "") },
      el("span", { class: "tick" }, dim ? "✓" : ""), el("span", { class: "lbl" }, c.name),
      el("span", { class: "hint" }, c.dtype));
    tick.addEventListener("click", () => {
      if (dim) d.dimensions = d.dimensions.filter((x) => x !== dim);
      else d.dimensions.push({
        name: c.name, column: c.name, label: titleCase(c.name),
        type: /date|time/i.test(c.dtype) ? "time" : "categorical", description: "", spine: null, geo: null,
      });
      markDirty();
      renderDatasetDims(box, d);
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

// ── step 4: REVIEW & SAVE ──

async function renderReview(main) {
  main.append(el("div", { class: "sec-title" }, "Review"));
  const report = el("div", { class: "editor-report" }, "generating yaml…");
  const pre = el("pre", { class: "mf-yaml" }, "");
  main.append(report, note(form.editingName
    ? `saving rewrites dimensions/${form.editingName}.yaml from this form (hand-written comments are not preserved)`
    : "saving writes a new file under dimensions/ — every fact model can then import it"), pre);

  generated = await api("/api/dimensions/generate", { method: "POST", body: toSpec() }).catch((e) => ({ ok: false, error: e.message }));
  if (form.step !== 3) return;   // author moved on while we were fetching
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
  $("#bf-save").disabled = !generated.ok;
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
  $("#bf-prev").addEventListener("click", () => { if (form.step > 0) { form.step -= 1; render(); } });
  $("#bf-next").addEventListener("click", () => {
    if (stepError()) return;
    form.step += 1;
    render();
  });
  $("#bf-save").addEventListener("click", saveBundleForm);
  $("#bf-yaml").addEventListener("click", editAsYaml);
  $("#bf-back").addEventListener("click", () => {
    if (!confirmLeaveBundleForm()) return;
    form.dirty = false;
    navigate(paths.modelling());
  });
}
