/* Semantic model editor: yaml textarea with live validation, source-column
   palette, save/create/delete against the model API. */
"use strict";

import { refreshModels } from "./builder.js";
import { $, api, el } from "./lib.js";
import { showView } from "./state.js";

const NEW_MODEL_TEMPLATE = `# new semantic model — SAVE writes models/<name>.yaml
name: my_model
label: My Model
description: What this model covers.
source:
  format: parquet        # parquet | csv | delta
  path: s3://cash-intel/path/*.parquet

# optional lookup joins:
# joins:
#   - name: lookup
#     source: { format: csv, path: s3://cash-intel/ref/lookup.csv }
#     on: key_column

dimensions:
  - name: some_column
  # - name: created_at
  #   type: time

measures:
  - name: rows
    label: Row Count
    expr: pl.len()
`;

export const editor = { mode: "edit", modelName: null, file: null, original: "" };
let validateTimer = null;

export async function openEditor(modelName) {
  const ta = $("#yaml-editor");
  if (modelName) {
    const data = await api(`/api/models/${modelName}/yaml`);
    editor.mode = "edit";
    editor.modelName = modelName;
    editor.file = data.file;
    editor.original = data.yaml;
  } else {
    editor.mode = "new";
    editor.modelName = null;
    editor.file = "models/<name>.yaml";
    editor.original = NEW_MODEL_TEMPLATE;
  }
  ta.value = editor.original;
  $("#editor-file").textContent = editor.file;
  $("#editor-delete").hidden = editor.mode === "new";
  showView("editor");
  validateEditor();
}

function editorStatus(html) {
  $("#editor-status").innerHTML = html;
}

export async function validateEditor() {
  editorStatus("validating…");
  const report = $("#editor-report");
  const colsBox = $("#editor-cols");
  try {
    const res = await api("/api/models/validate", { method: "POST", body: { yaml: $("#yaml-editor").value } });
    if (!res.ok) {
      editorStatus('<span class="err">✗ invalid</span>');
      report.innerHTML = `<span class="err">${res.error}</span>`;
      colsBox.innerHTML = "";
      return false;
    }
    editorStatus(`<span class="ok">✓ valid</span> · ${res.model.name} · ${res.model.dimensions} dims · ${res.model.measures} measures`);
    report.innerHTML = `<b>${res.model.label}</b> (${res.model.name})<br>`
      + `${res.model.dimensions} dimensions · ${res.model.measures} measures`
      + (res.schema_error ? `<br><span class="warn">⚠ ${res.schema_error}</span>` : "");
    colsBox.innerHTML = "";
    for (const c of res.columns || []) {
      const chip = el("div", { class: "col-chip", title: `insert pl.col("${c.name}")` },
        el("span", {}, c.name), el("span", { class: "dt" }, c.dtype));
      chip.addEventListener("click", () => insertAtCursor($("#yaml-editor"), `pl.col("${c.name}")`));
      colsBox.append(chip);
    }
    return true;
  } catch (err) {
    editorStatus('<span class="err">✗ validation failed</span>');
    report.innerHTML = `<span class="err">${err.message}</span>`;
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
    const saved = editor.mode === "new"
      ? await api("/api/models", { method: "POST", body: { yaml } })
      : await api(`/api/models/${editor.modelName}/yaml`, { method: "PUT", body: { yaml } });
    editor.mode = "edit";
    editor.modelName = saved.name;
    editor.file = saved.file;
    editor.original = yaml;
    $("#editor-file").textContent = saved.file;
    $("#editor-delete").hidden = false;
    await refreshModels();
    await validateEditor();
    editorStatus($("#editor-status").innerHTML + ' · <span class="ok">saved ✓</span>');
  } catch (err) {
    editorStatus('<span class="err">✗ save failed</span>');
    $("#editor-report").innerHTML = `<span class="err">${err.message}</span>`;
  }
}

export async function deleteEditorModel() {
  if (editor.mode === "new") return;
  if (!confirm(`Delete model '${editor.modelName}' (${editor.file})? Saved visuals pointing at it will stop working.`)) return;
  await api(`/api/models/${editor.modelName}`, { method: "DELETE" });
  await refreshModels();
  showView("builder");
}
