/* Semantic editor: a yaml textarea with live validation and a source-column
   palette, shared by two kinds of artifact —
     kind "model"  → fact models (models/*.yaml), validated via /api/models
     kind "bundle" → common dimensional models (dimensions/*.yaml), via /api/dimensions
   Fact models additionally get a "Common Dimensions" import panel (dimlab.js). */
"use strict";

import { refreshModels } from "./builder.js";
import { renderBundleList, renderImportPanel } from "./dimlab.js";
import { $, api, el } from "./lib.js";
import { showView, state } from "./state.js";

const NEW_MODEL_TEMPLATE = `# new semantic model — SAVE writes models/<name>.yaml
name: my_model
label: My Model
description: What this model covers.
source:
  format: parquet        # parquet | csv | delta
  path: s3://cash-intel/path/*.parquet

# import shared dimensions from a common dimensional model — or use the
# "Common Dimensions" panel on the right to insert one for you:
# dimension_imports:
#   - bundle: geography
#     anchor_dataset: regions
#     on: region

dimensions:
  - name: some_column
  # - name: created_at
  #   type: time

measures:
  - name: rows
    label: Row Count
    expr: pl.len()
`;

const NEW_BUNDLE_TEMPLATE = `# new common dimensional model — SAVE writes dimensions/<name>.yaml
# A bundle is a set of reusable datasets (source + dimensions, no measures)
# that any fact model can import. Datasets may join to each other.
name: my_dimensions
label: My Dimensions
description: Shared dimensions imported by fact models.

datasets:
  - name: accounts
    source: { format: csv, path: s3://cash-intel/ref/accounts.csv }
    dimensions:
      - name: account
        label: Account
      - name: account_tier
        label: Tier
    # joins to another dataset in this same bundle:
    # joins:
    #   - to: territories
    #     on: territory
`;

// per-kind endpoints/labels so the one editor serves both artifacts
const KINDS = {
  model: {
    template: NEW_MODEL_TEMPLATE,
    noun: "model",
    deleteLabel: "DELETE MODEL",
    getYaml: (name) => api(`/api/models/${name}/yaml`),
    validate: (yaml) => api("/api/models/validate", { method: "POST", body: { yaml } }),
    create: (yaml) => api("/api/models", { method: "POST", body: { yaml } }),
    put: (name, yaml) => api(`/api/models/${name}/yaml`, { method: "PUT", body: { yaml } }),
    del: (name) => api(`/api/models/${name}`, { method: "DELETE" }),
    insert: (col) => `pl.col("${col}")`,   // measures use pl.col(...)
  },
  bundle: {
    template: NEW_BUNDLE_TEMPLATE,
    noun: "common model",
    deleteLabel: "DELETE COMMON MODEL",
    getYaml: (name) => api(`/api/dimensions/${name}/yaml`),
    validate: (yaml) => api("/api/dimensions/validate", { method: "POST", body: { yaml } }),
    create: (yaml) => api("/api/dimensions", { method: "POST", body: { yaml } }),
    put: (name, yaml) => api(`/api/dimensions/${name}/yaml`, { method: "PUT", body: { yaml } }),
    del: (name) => api(`/api/dimensions/${name}`, { method: "DELETE" }),
    insert: (col) => col,                    // bundle dims/join keys are bare column names
  },
};

export const editor = { kind: "model", name: null, file: null, original: "" };
let validateTimer = null;

const cfg = () => KINDS[editor.kind];

export async function openEditor(kind, name) {
  editor.kind = kind in KINDS ? kind : "model";
  const ta = $("#yaml-editor");
  if (name) {
    const data = await cfg().getYaml(name);
    editor.name = name;
    editor.file = data.file;
    editor.original = data.yaml;
  } else {
    editor.name = null;
    editor.file = editor.kind === "bundle" ? "dimensions/<name>.yaml" : "models/<name>.yaml";
    editor.original = cfg().template;
  }
  ta.value = editor.original;
  $("#editor-file").textContent = editor.file;
  $("#editor-delete").textContent = cfg().deleteLabel;
  $("#editor-delete").hidden = !editor.name;
  const isModel = editor.kind === "model";
  $("#editor-imports").hidden = !isModel;
  if (isModel) renderImportPanel();      // "Common Dimensions" import affordance
  showView("editor");
  validateEditor();
}

function editorStatus(html) {
  $("#editor-status").innerHTML = html;
}

function renderColChips(cols, note) {
  const colsBox = $("#editor-cols");
  $("#editor-cols-note").textContent = note;
  colsBox.innerHTML = "";
  for (const c of cols || []) {
    const chip = el("div", { class: "col-chip", title: `insert ${cfg().insert(c.name)}` },
      el("span", {}, c.name), el("span", { class: "dt" }, c.dtype));
    chip.addEventListener("click", () => insertAtCursor($("#yaml-editor"), cfg().insert(c.name)));
    colsBox.append(chip);
  }
}

async function validateModel(yaml) {
  const res = await cfg().validate(yaml);
  if (!res.ok) return { ok: false, error: res.error };
  editorStatus(`<span class="ok">✓ valid</span> · ${res.model.name} · ${res.model.dimensions} dims · ${res.model.measures} measures`);
  $("#editor-report").innerHTML = `<b>${res.model.label}</b> (${res.model.name})<br>`
    + `${res.model.dimensions} dimensions · ${res.model.measures} measures`
    + (res.schema_error ? `<br><span class="warn">⚠ ${res.schema_error}</span>` : "");
  renderColChips(res.columns, "click to insert pl.col(...) at cursor");
  return { ok: true };
}

async function validateBundle(yaml) {
  const res = await cfg().validate(yaml);
  if (!res.ok) return { ok: false, error: res.error };
  const b = res.bundle;
  const totalDims = b.datasets.reduce((s, d) => s + d.dimensions, 0);
  editorStatus(`<span class="ok">✓ valid</span> · ${b.name} · ${b.datasets.length} datasets · ${totalDims} dims`);
  $("#editor-report").innerHTML = `<b>${b.label}</b> (${b.name})<br>`
    + b.datasets.map((d) => `${d.name}: ${d.dimensions} dims`
        + (d.joins.length ? ` → ${d.joins.join(", ")}` : "")
        + (d.schema_error ? ` <span class="warn">⚠</span>` : "")).join("<br>");
  // columns grouped per dataset (click inserts the bare column name)
  const colsBox = $("#editor-cols");
  $("#editor-cols-note").textContent = "click to insert a column name at cursor";
  colsBox.innerHTML = "";
  for (const d of b.datasets) {
    colsBox.append(el("div", { class: "ds-head" }, d.name));
    for (const c of d.columns || []) {
      const chip = el("div", { class: "col-chip", title: `insert ${c.name}` },
        el("span", {}, c.name), el("span", { class: "dt" }, c.dtype));
      chip.addEventListener("click", () => insertAtCursor($("#yaml-editor"), c.name));
      colsBox.append(chip);
    }
    if (d.schema_error) colsBox.append(el("div", { class: "empty-note" }, d.schema_error));
  }
  return { ok: true };
}

export async function validateEditor() {
  editorStatus("validating…");
  try {
    const result = editor.kind === "bundle"
      ? await validateBundle($("#yaml-editor").value)
      : await validateModel($("#yaml-editor").value);
    if (!result.ok) {
      editorStatus('<span class="err">✗ invalid</span>');
      $("#editor-report").innerHTML = `<span class="err">${result.error}</span>`;
      $("#editor-cols").innerHTML = "";
      return false;
    }
    return true;
  } catch (err) {
    editorStatus('<span class="err">✗ validation failed</span>');
    $("#editor-report").innerHTML = `<span class="err">${err.message}</span>`;
    return false;
  }
}

export function scheduleValidate() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validateEditor, 500);
}

export function insertAtCursor(ta, text) {
  const s = ta.selectionStart, e = ta.selectionEnd;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + text.length;
  ta.focus();
  scheduleValidate();
}

export async function saveEditor() {
  const yaml = $("#yaml-editor").value;
  editorStatus("saving…");
  try {
    const saved = editor.name
      ? await cfg().put(editor.name, yaml)
      : await cfg().create(yaml);
    editor.name = saved.name;
    editor.file = saved.file;
    editor.original = yaml;
    $("#editor-file").textContent = saved.file;
    $("#editor-delete").hidden = false;
    // both kinds affect the model set: editing a bundle re-resolves importers,
    // and a fresh bundle becomes importable; keep every surface coherent
    await refreshModels();
    if (editor.kind === "bundle") { await renderBundleList(); }
    else { await renderImportPanel(); }
    await validateEditor();
    editorStatus($("#editor-status").innerHTML + ' · <span class="ok">saved ✓</span>');
  } catch (err) {
    editorStatus('<span class="err">✗ save failed</span>');
    $("#editor-report").innerHTML = `<span class="err">${err.message}</span>`;
  }
}

export async function deleteEditorItem() {
  if (!editor.name) return;
  const warn = editor.kind === "bundle"
    ? `Delete common model '${editor.name}' (${editor.file})?`
    : `Delete model '${editor.name}' (${editor.file})? Saved visuals pointing at it will stop working.`;
  if (!confirm(warn)) return;
  try {
    await cfg().del(editor.name);
  } catch (err) {
    // e.g. a bundle still imported by a model — refused with a naming message
    editorStatus('<span class="err">✗ delete refused</span>');
    $("#editor-report").innerHTML = `<span class="err">${err.message}</span>`;
    return;
  }
  await refreshModels();
  if (editor.kind === "bundle") await renderBundleList();
  showView("builder");
}
