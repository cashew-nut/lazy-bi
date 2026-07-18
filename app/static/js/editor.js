/* Semantic editor: a yaml textarea with live validation, a source-column
   palette, a dataset picker, and expression intellisense — shared by two
   kinds of artifact —
     kind "model"  → fact models (models/*.yaml), validated via /api/models
     kind "bundle" → common dimensional models (dimensions/*.yaml), via /api/dimensions
   Fact models additionally get a "Common Dimensions" import panel (dimlab.js)
   and the dataset picker. The yaml textarea is the single source of truth: the
   picker, import panel, and completion all insert/patch that text. */
"use strict";

import { isAdmin } from "./auth.js";
import { refreshModels } from "./builder.js";
import { dslContext, dslItems, makeCompleter } from "./completion.js";
import { renderImportPanel } from "./dimlab.js";
import { $, api, el, fmtBytes } from "./lib.js";
import { navigate, paths } from "./router.js";
import { hooks, showView, state } from "./state.js";

const NEW_MODEL_TEMPLATE = `# new semantic model — SAVE writes models/<name>.yaml
name: my_model
label: My Model
description: What this model covers.
source:
  format: parquet        # parquet | csv | delta
  path: s3://cash-intel/path/*.parquet

# pick a real dataset with the "◇ DATASET" button above to fill in source,
# then use the column palette / intellisense to write dimensions & measures.

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
    expr: count()
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

const NEW_PIPELINE_TEMPLATE = `# new pipeline — SAVE writes pipelines/<name>.yaml
# A pipeline is a real polars script the platform hosts, runs, and
# materializes for you — the script only produces \`output\`; the platform
# performs the write (replace or upsert).
name: my_pipeline
sources:
  - name: raw
    format: parquet
    path: s3://cash-intel/path/*.parquet
target:
  path: s3://cash-intel/path/to/target   # delta table root (or an object key for parquet)
  format: delta                          # delta (default, required for upsert) | parquet (replace only)
materialization:
  mode: replace                          # replace | upsert
  # keys: [id]                           # upsert: required
  # on_delete: soft_delete                # ignore (default) | sync | soft_delete | predicate
  # soft_delete_column: is_deleted        # soft_delete: required
  # delete_predicate: "region = 'EU'"     # predicate: required
timeout_seconds: 600
script: |
  output = sources["raw"]
# lineage:                               # optional — documents transformation logic on the target model
#   - field: id
#     from: [raw.id]
#     transform: pass-through
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
    insert: (col) => col,   // bare names are valid everywhere: dims, joins, and DSL measure exprs
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
  pipeline: {
    template: NEW_PIPELINE_TEMPLATE,
    noun: "pipeline",
    deleteLabel: "DELETE PIPELINE",
    getYaml: (name) => api(`/api/pipelines/${name}/yaml`),
    validate: (yaml) => api("/api/pipelines/validate", { method: "POST", body: { yaml } }),
    create: (yaml) => api("/api/pipelines", { method: "POST", body: { yaml } }),
    put: (name, yaml) => api(`/api/pipelines/${name}/yaml`, { method: "PUT", body: { yaml } }),
    del: (name) => api(`/api/pipelines/${name}`, { method: "DELETE" }),
    insert: (col) => col,
  },
};

// editor.dirty and editor.columns (& friends below) are ephemeral session
// state (never persisted; a reload discards them — see specs/007-modelling-
// workspace/data-model.md). sources/layers/datasetNames feed the structural
// intellisense (schemaItems/scriptSourceItems) the same way columns feeds
// the DSL/column completion.
export const editor = {
  kind: "model", name: null, file: null, original: "", dirty: false,
  columns: [], sources: [], layers: [], datasetNames: [],
};
let validateTimer = null;
let completer = null;
let lastOk = true;   // last validation result — save is guarded when false

const cfg = () => KINDS[editor.kind];

// yaml keys whose value is a bare source-column reference (not an expression)
const COLUMN_KEYS = new Set(["column", "on", "left_on", "right_on", "start", "end", "lat", "lon"]);

// ── structural (schema-aware) key & enum intellisense ──
// A shallow, client-side mirror of each artifact's yaml shape (see
// app/semantic.py / app/pipelines.py for the real source of truth), used
// only to speed up typing: an empty/partial key on its own line suggests
// the keys valid in that block, and a handful of enum-valued keys suggest
// their legal values. This never substitutes for validateEditor() — the
// server round trip is still the only real arbiter of a valid document.
const SCHEMAS = {
  model: {
    top: ["name", "label", "description", "source", "joins", "dimension_imports", "dimensions", "measures"],
    source: ["format", "path"],
    joins: ["name", "on", "left_on", "right_on", "how"],
    dimension_imports: ["bundle", "anchor_dataset", "on", "left_on", "right_on", "how", "datasets"],
    dimensions: ["name", "column", "label", "type", "description", "spine", "geo", "synonyms"],
    measures: ["name", "label", "expr", "format", "description"],
    spine: ["start", "end"],
    geo: ["lat", "lon"],
  },
  bundle: {
    top: ["name", "label", "description", "datasets"],
    datasets: ["name", "source", "dimensions", "joins"],
    source: ["format", "path"],
    dimensions: ["name", "column", "label", "type", "description", "spine", "geo", "synonyms"],
    joins: ["to", "on", "left_on", "right_on", "how"],
    spine: ["start", "end"],
    geo: ["lat", "lon"],
  },
  pipeline: {
    top: ["name", "label", "description", "sources", "target", "materialization", "timeout_seconds", "script", "lineage"],
    sources: ["name", "format", "path", "layer"],
    target: ["path", "format", "layer"],
    materialization: ["mode", "keys", "on_delete", "soft_delete_column", "delete_predicate", "allow_empty_sync"],
    lineage: ["field", "from", "transform"],
  },
};

// static enum values, keyed by [kind][block][key]
const ENUMS = {
  model: {
    source: { format: ["parquet", "csv", "delta"] },
    joins: { how: ["left", "inner"] },
    dimension_imports: { how: ["left", "inner"] },
    dimensions: { type: ["categorical", "time", "numeric"] },
    measures: { format: ["number", "currency", "percent"] },
  },
  bundle: {
    source: { format: ["parquet", "csv", "delta"] },
    joins: { how: ["left", "inner"] },
    dimensions: { type: ["categorical", "time", "numeric"] },
  },
  pipeline: {
    sources: { format: ["parquet", "csv", "delta"] },
    target: { format: ["delta", "parquet"] },
    materialization: { mode: ["replace", "upsert"], on_delete: ["ignore", "sync", "soft_delete", "predicate"] },
    lineage: { transform: ["pass-through"] },
  },
};

// dynamic (schema-external) enum values that depend on the current document
// or bucket state rather than a fixed list — e.g. a pipeline's known layers.
function dynamicEnum(kind, block, key) {
  if (kind === "pipeline" && (block === "sources" || block === "target") && key === "layer") return editor.layers;
  if (kind === "bundle" && block === "joins" && key === "to") return editor.datasetNames;
  return null;
}

function indentOf(line) {
  return line.match(/^[ \t]*/)[0].length;
}

// Walk backward from `lineIdx` to find the enclosing block's key: the
// nearest shallower-indented ancestor line, skipping past list-item marker
// lines ("- foo: bar" — an anonymous entry, not a schema block name) to the
// mapping key that owns them, e.g. a `joins:` list nested three levels deep
// inside a bundle's `datasets:` resolves to "joins", not "datasets". A
// non-list container (`target:`, a dimension's `spine:`) is its own
// immediate ancestor, so it resolves in a single hop. Returns null at the
// document top level.
function blockContext(lines, lineIdx) {
  let indent = indentOf(lines[lineIdx]);
  for (let i = lineIdx - 1; i >= 0; i--) {
    const l = lines[i];
    if (!l.trim()) continue;
    const lineIndent = indentOf(l);
    if (lineIndent >= indent) continue;
    indent = lineIndent;
    if (/^\s*-/.test(l)) continue;   // list-item marker — keep climbing to its list key
    const m = l.slice(lineIndent).match(/^([A-Za-z_]+):/);
    return m ? m[1] : null;
  }
  return null;
}

// yaml key-name / enum-value completion for the current line, schema-driven
// by SCHEMAS/ENUMS above. Returns null inside a pipeline's `script:` block
// (real python, not yaml) — see scriptSourceItems for that case instead.
function schemaItems(kind, lines, lineIdx, line, caret) {
  const schema = SCHEMAS[kind];
  if (!schema) return null;
  const block = blockContext(lines, lineIdx) || "top";
  if (block === "script") return null;
  let m = line.match(/^(\s*(?:-\s*)?)([A-Za-z_]+):[ \t]*(\S*)$/);
  if (m) {
    const key = m[2], prefix = m[3];
    const values = dynamicEnum(kind, block, key) || ENUMS[kind]?.[block]?.[key];
    if (!values) return null;
    const items = values.filter((v) => v.toLowerCase().startsWith(prefix.toLowerCase()))
      .map((v) => ({ text: v, hint: "", insert: v, caretOffset: 0 }));
    return items.length ? { items, start: caret - prefix.length } : null;
  }
  m = line.match(/^(\s*(?:-\s*)?)([A-Za-z_]*)$/);
  if (m) {
    const prefix = m[2];
    const keys = schema[block];
    if (!keys) return null;
    const items = keys.filter((k) => k.startsWith(prefix))
      .map((k) => ({ text: k, hint: "", insert: `${k}: `, caretOffset: 0 }));
    if (items.length) return { items, start: caret - prefix.length };
  }
  return null;
}

// `sources["...` completion inside a pipeline's `script:` block — the
// python-side counterpart of the yaml `source:`/`target:` completion above.
function scriptSourceItems(upto, after, caret) {
  const m = upto.match(/sources\[\s*["']([A-Za-z0-9_]*)$/);
  if (!m) return null;
  const prefix = m[1];
  const closer = after.startsWith('"') || after.startsWith("'") ? "" : '"]';
  const skip = closer ? 0 : 2;
  const items = (editor.sources || [])
    .filter((s) => s.toLowerCase().startsWith(prefix.toLowerCase()))
    .map((s) => ({ text: s, hint: "source", insert: s + closer, caretOffset: skip }));
  return items.length ? { items, start: caret - prefix.length } : null;
}

const FILE_TEMPLATES = { bundle: "dimensions/<name>.yaml", pipeline: "pipelines/<name>.yaml" };

export async function openEditor(kind, name, opts = {}) {
  // guard: never silently drop unsaved edits when opening another artifact
  if (state.view === "editor" && !confirmLeaveEditor()) return;
  stopRunPolling();
  editor.kind = kind in KINDS ? kind : "model";
  const ta = $("#yaml-editor");
  if (name) {
    const data = await cfg().getYaml(name);
    editor.name = name;
    editor.file = data.file;
    editor.original = data.yaml;
  } else {
    editor.name = null;
    editor.file = FILE_TEMPLATES[editor.kind] || "models/<name>.yaml";
    editor.original = cfg().template;
  }
  // opts.text: open with handed-over content (the guided form's generated
  // yaml) as an unsaved edit — REVERT still restores the on-disk original
  ta.value = opts.text ?? editor.original;
  editor.dirty = opts.text != null && opts.text !== editor.original;
  editor.columns = [];
  editor.sources = [];
  editor.layers = [];
  editor.datasetNames = [];
  $("#editor-datasets").hidden = true;
  $("#editor-file").textContent = editor.file;
  $("#editor-delete").textContent = cfg().deleteLabel;
  $("#editor-delete").hidden = !editor.name || !isAdmin();
  const isModel = editor.kind === "model";
  const isPipeline = editor.kind === "pipeline";
  $("#editor-imports").hidden = !isModel;
  $("#editor-pick-dataset").hidden = !isModel;    // dataset source applies to fact models only
  $("#editor-cols-panel").hidden = isPipeline;    // no column palette for a real-python script
  $("#editor-pipeline-panel").hidden = !isPipeline;
  // a brand-new (unsaved) pipeline has nothing to run/suggest yet; both are
  // admin-only actions like SAVE/DELETE — applyRoleGates() only runs once
  // at boot/login, so any later visibility change here must re-check the
  // role itself rather than rely on that one-time pass (data-role is still
  // set in the markup for defense-in-depth / correct initial paint).
  $("#editor-run").hidden = !isPipeline || !editor.name || !isAdmin();
  $("#editor-lineage-suggest").hidden = !isPipeline || !editor.name || !isAdmin();
  if (isModel) renderImportPanel();      // "Common Dimensions" import affordance
  if (isPipeline) {
    loadLayerPicker();
    if (editor.name) loadRunHistory(); else renderRuns([]);
  }
  showView("editor");
  validateEditor();
}
hooks.openEditor = openEditor;

// ── unsaved-edit guard (ephemeral state; FR-021 / Constitution V) ──

export function confirmLeaveEditor() {
  if (!editor.dirty) return true;
  return confirm("You have unsaved changes to this model. Discard them?");
}
hooks.confirmLeaveEditor = confirmLeaveEditor;

function markDirty() {
  editor.dirty = true;
  scheduleValidate();
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
  editor.columns = res.columns || [];   // feeds intellisense + column palette
  editorStatus(`<span class="ok">✓ valid</span> · ${res.model.name} · ${res.model.dimensions} dims · ${res.model.measures} measures`);
  $("#editor-report").innerHTML = `<b>${res.model.label}</b> (${res.model.name})<br>`
    + `${res.model.dimensions} dimensions · ${res.model.measures} measures`
    + (res.schema_error ? `<br><span class="warn">⚠ ${res.schema_error}</span>` : "");
  renderColChips(res.columns, "click to insert the column name at cursor");
  return { ok: true };
}

async function validateBundle(yaml) {
  const res = await cfg().validate(yaml);
  if (!res.ok) return { ok: false, error: res.error };
  const b = res.bundle;
  const totalDims = b.datasets.reduce((s, d) => s + d.dimensions, 0);
  editor.columns = b.datasets.flatMap((d) => d.columns || []);   // bundle-wide column pool for intellisense
  editor.datasetNames = b.datasets.map((d) => d.name);            // feeds `to:` join-target completion
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

async function validatePipeline(yaml) {
  const res = await cfg().validate(yaml);
  if (!res.ok) return { ok: false, error: res.error };
  const p = res.pipeline;
  const m = p.materialization;
  editor.sources = p.sources.map((s) => s.name);   // feeds sources["..."] completion in the script block
  editorStatus(`<span class="ok">✓ valid</span> · ${p.name} · ${m.mode}${m.mode === "upsert" ? ` (${m.on_delete})` : ""}`);
  $("#editor-report").innerHTML = `<b>${p.label}</b> (${p.name})<br>`
    + `target: <code>${p.target.path}</code> (${p.target.format})<br>`
    + `mode: ${m.mode}` + (m.mode === "upsert"
      ? ` · keys: ${m.keys.join(", ")} · on_delete: ${m.on_delete}`
        + (m.soft_delete_column ? ` (${m.soft_delete_column})` : "")
      : "");
  const lineageBox = $("#editor-lineage-list");
  lineageBox.innerHTML = "";
  if (p.lineage.length) {
    for (const entry of p.lineage) {
      lineageBox.append(el("div", { class: "col-chip", title: entry.transform || "" },
        el("span", {}, entry.field), el("span", { class: "dt" }, entry.from.join(", "))));
    }
  } else {
    lineageBox.append(el("div", { class: "empty-note" }, "no lineage declared"));
  }
  return { ok: true };
}

export async function validateEditor() {
  editorStatus("validating…");
  try {
    const result = editor.kind === "bundle" ? await validateBundle($("#yaml-editor").value)
      : editor.kind === "pipeline" ? await validatePipeline($("#yaml-editor").value)
      : await validateModel($("#yaml-editor").value);
    if (!result.ok) {
      lastOk = false;
      editor.columns = [];
      editorStatus('<span class="err">✗ invalid</span>');
      $("#editor-report").innerHTML = `<span class="err">${result.error}</span>`;
      $("#editor-cols").innerHTML = "";
      return false;
    }
    lastOk = true;
    return true;
  } catch (err) {
    lastOk = false;
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
  markDirty();
}

// ── dataset picker: set the model's source from a bucket dataset (US2) ──

// Patch the top-level `source:` block in the yaml (replace if present, else
// insert after the header). Text-level so it stays the single source of truth
// and never corrupts a hand-edited document (worst case: an extra source block).
function applySource(path, format) {
  const ta = $("#yaml-editor");
  const lines = ta.value.split("\n");
  const isTop = (l) => l.trim() && !l.startsWith(" ") && !l.startsWith("\t") && !l.startsWith("#");
  const block = ["source:", `  format: ${format}`, `  path: ${path}`];
  const start = lines.findIndex((l) => /^source:(\s|$)/.test(l));
  let next;
  if (start !== -1) {
    let end = lines.length;
    for (let i = start + 1; i < lines.length; i++) { if (isTop(lines[i])) { end = i; break; } }
    next = [...lines.slice(0, start), ...block, ...lines.slice(end)];
  } else {
    let insertAt = 0;
    for (let i = 0; i < lines.length; i++) { if (/^(name|label|description):/.test(lines[i])) insertAt = i + 1; }
    next = [...lines.slice(0, insertAt), ...block, ...lines.slice(insertAt)];
  }
  ta.value = next.join("\n");
  markDirty();
  $("#editor-datasets").hidden = true;
  editorStatus(`source set → <span class="ok">${path}</span>`);
}

async function toggleDatasetPicker() {
  const panel = $("#editor-datasets");
  if (!panel.hidden) { panel.hidden = true; return; }
  panel.hidden = false;
  panel.innerHTML = "";
  panel.append(el("div", { class: "sec-title" }, "Datasets"),
    el("div", { class: "empty-note" }, "scanning bucket…"));
  let data;
  try {
    data = await api("/api/datasets");
  } catch (err) {
    panel.innerHTML = "";
    panel.append(el("div", { class: "sec-title" }, "Datasets"),
      el("div", { class: "empty-note" }, "bucket not reachable — you can still type a path"));
    return;
  }
  panel.innerHTML = "";
  panel.append(el("div", { class: "sec-title" }, "Datasets"));
  if (!data.datasets.length) {
    panel.append(el("div", { class: "empty-note" }, "no datasets in the bucket"));
    return;
  }
  panel.append(el("div", { class: "empty-note" }, "click to set this model's source"));
  for (const ds of data.datasets) {
    const card = el("div", { class: "import-card" });
    const head = el("div", { class: "ds-pick-head", title: `set source → ${ds.path}` },
      el("span", { class: "nm" }, ds.key || "(root)"),
      el("span", { class: "fmt" }, ds.format));
    head.addEventListener("click", () => applySource(ds.path, ds.format));
    card.append(head);
    const readerNames = [...new Set(ds.models.map((m) => m.name))];
    const readers = readerNames.length ? ` · read by ${readerNames.join(", ")}` : "";
    card.append(el("div", { class: "ds-pick-meta" },
      `${ds.object_count} obj · ${fmtBytes(ds.bytes)}${readers}${ds.format_ambiguous ? " · ⚠ mixed types" : ""}`));
    if (ds.format !== "delta" && ds.objects.length > 1) {
      const drill = el("div", { class: "import-datasets" });
      for (const o of ds.objects) {
        const chip = el("div", { class: "col-chip", title: `set source → this single object` },
          el("span", {}, o.key.split("/").pop()), el("span", { class: "dt" }, o.format));
        chip.addEventListener("click", () => applySource(`s3://${data.bucket}/${o.key}`, o.format));
        drill.append(chip);
      }
      card.append(drill);
    }
    panel.append(card);
  }
}

// ── expression + structural intellisense in the yaml editor (US4) ──

// Context-aware completion, tried in order: measure-DSL completion inside
// a model's `expr:` value, bare column-name completion in dimension/join
// key contexts, a pipeline script's `sources["..."]` dict lookup, and
// finally schema-driven yaml key-name/enum-value completion (see
// specs/007-modelling-workspace/contracts/completion.md and
// specs/008-safe-measure-compilation/contracts/compile_measure.md for the
// first two; SCHEMAS/ENUMS above for the last).
function yamlResolve(upto, after, caret) {
  const line = upto.slice(upto.lastIndexOf("\n") + 1);

  // measure-DSL completion — only inside a measure's `expr:` value (models
  // are the only kind with measures; gating by line avoids offering DSL
  // functions after every unrelated "key: value" colon in the document)
  if (editor.kind === "model" && /^\s*expr:[ \t]?/.test(line)) {
    const pctx = dslContext(upto, caret);
    if (pctx) return { items: dslItems(pctx, editor.columns, after), start: pctx.start };
  }

  // bare column-name completion in column-key contexts
  const m = line.match(/^(\s*(?:-\s*)?)([A-Za-z_]+):[ \t]*(\S*)$/);
  if (m) {
    const isListItem = m[1].includes("-");
    const key = m[2];
    const colKey = COLUMN_KEYS.has(key) || (key === "name" && isListItem);
    if (colKey) {
      const prefix = m[3];
      const items = (editor.columns || [])
        .filter((c) => c.name.toLowerCase().startsWith(prefix.toLowerCase()))
        .map((c) => ({ text: c.name, hint: c.dtype, insert: c.name, caretOffset: 0 }));
      if (items.length) return { items, start: caret - prefix.length };
    }
  }

  // pipeline script: sources["..."] dict-key completion
  if (editor.kind === "pipeline") {
    const si = scriptSourceItems(upto, after, caret);
    if (si) return si;
  }

  // schema-driven yaml key-name / enum-value completion
  const lines = upto.split("\n");
  return schemaItems(editor.kind, lines, lines.length - 1, line, caret);
}

export async function saveEditor() {
  if (!lastOk) {
    // FR-015 / FR-018: never silently persist an invalid document
    if (!confirm("This model currently fails validation and may not load. Save anyway?")) return;
  }
  const yaml = $("#yaml-editor").value;
  const wasNew = !editor.name;
  editorStatus("saving…");
  try {
    const saved = editor.name
      ? await cfg().put(editor.name, yaml)
      : await cfg().create(yaml);
    editor.name = saved.name;
    editor.file = saved.file;
    editor.original = yaml;
    editor.dirty = false;
    $("#editor-file").textContent = saved.file;
    $("#editor-delete").hidden = !isAdmin();
    if (editor.kind === "pipeline") {
      $("#editor-run").hidden = !isAdmin();
      $("#editor-lineage-suggest").hidden = !isAdmin();
      if (hooks.loadModelling) await hooks.loadModelling();
      await loadRunHistory();
    } else {
      // both kinds affect the model set: editing a bundle re-resolves importers,
      // and a fresh bundle becomes importable; keep every surface coherent
      await refreshModels();
      if (editor.kind === "bundle") { if (hooks.loadModelling) await hooks.loadModelling(); }
      else { await renderImportPanel(); }
    }
    await validateEditor();
    editorStatus($("#editor-status").innerHTML + ' · <span class="ok">saved ✓</span>');
    // a brand-new artifact just got its real name — catch the URL up to it
    if (wasNew) {
      const target = editor.kind === "bundle" ? paths.modellingBundleYaml(saved.name)
        : editor.kind === "pipeline" ? paths.modellingPipelineYaml(saved.name)
        : paths.modellingModelYaml(saved.name);
      navigate(target, { replace: true });
    }
  } catch (err) {
    editorStatus('<span class="err">✗ save failed</span>');
    $("#editor-report").innerHTML = `<span class="err">${err.message}</span>`;
  }
}

export async function deleteEditorItem() {
  if (!editor.name) return;
  const warn = editor.kind === "bundle"
    ? `Delete common model '${editor.name}' (${editor.file})?`
    : editor.kind === "pipeline"
    ? `Delete pipeline '${editor.name}' (${editor.file})? Run history is kept; the target model's lineage section (if any) is marked orphaned.`
    : `Delete model '${editor.name}' (${editor.file})? Saved visuals pointing at it will stop working.`;
  if (!confirm(warn)) return;
  try {
    await cfg().del(editor.name);
  } catch (err) {
    // e.g. a bundle still imported by a model, or a pipeline with a run
    // pending — refused with a naming message
    editorStatus('<span class="err">✗ delete refused</span>');
    $("#editor-report").innerHTML = `<span class="err">${err.message}</span>`;
    return;
  }
  editor.dirty = false;
  stopRunPolling();
  if (editor.kind === "pipeline") { if (hooks.loadModelling) await hooks.loadModelling(); }
  else await refreshModels();
  navigate(paths.modelling());
}

// ── pipeline run panel (US1/US2/US3) — ephemeral polling state, never
// persisted (Constitution V): a reload always starts from the saved run
// history, never a resumed poll. ──

let runPollTimer = null;

export function stopRunPolling() {
  clearInterval(runPollTimer);
  runPollTimer = null;
}

const RUN_STATUS_LABEL = {
  queued: "queued", running: "running…", succeeded: "✓ succeeded", failed: "✗ failed",
  timed_out: "⏱ timed out", interrupted: "⚠ interrupted",
};

function renderRuns(runs) {
  const body = $("#editor-runs-body");
  body.innerHTML = "";
  const latest = runs[0];
  $("#editor-run-status").textContent = latest
    ? `latest: ${RUN_STATUS_LABEL[latest.status] || latest.status}` : "not run yet";
  if (!runs.length) return;
  for (const run of runs) {
    const lineage = run.lineage_ok === null || run.lineage_ok === undefined ? "—"
      : run.lineage_ok ? "✓" : `⚠ ${(run.lineage_issues || []).map((i) => i.field).join(", ")}`;
    body.append(el("tr", {},
      el("td", {}, RUN_STATUS_LABEL[run.status] || run.status),
      el("td", {}, (run.started_at || run.queued_at || "").replace("T", " ").slice(0, 19)),
      el("td", { class: "num" }, run.rows_written ?? "—"),
      el("td", { class: "num" }, run.rows_deleted ?? "—"),
      el("td", { class: "num" }, run.rows_flagged ?? "—"),
      el("td", {}, lineage),
      el("td", { title: run.error || "" }, run.error ? run.error.slice(0, 60) : "—")));
  }
}

async function loadRunHistory() {
  if (!editor.name || editor.kind !== "pipeline") return;
  try {
    renderRuns(await api(`/api/pipelines/${editor.name}/runs`));
  } catch { /* pipeline just deleted mid-view, or transient — leave prior render */ }
}

export async function runPipeline() {
  if (!editor.name || editor.kind !== "pipeline") return;
  try {
    await api(`/api/pipelines/${editor.name}/run`, { method: "POST" });
  } catch (err) {
    alert(`Could not start run: ${err.message}`);
    return;
  }
  await loadRunHistory();
  stopRunPolling();
  runPollTimer = setInterval(async () => {
    if (state.view !== "editor" || editor.kind !== "pipeline") { stopRunPolling(); return; }
    const runs = await api(`/api/pipelines/${editor.name}/runs`);
    renderRuns(runs);
    if (runs[0] && runs[0].status !== "queued" && runs[0].status !== "running") stopRunPolling();
  }, 1000);
}

// ── layer picker (US3): a click-to-insert shortcut for the target's layer —
// source layers and anything more exotic stay hand-edited in the yaml,
// which the parser already fully supports. ──

function _topLevelBlockEnd(lines, keyIdx) {
  let end = lines.length;
  for (let i = keyIdx + 1; i < lines.length; i++) {
    if (lines[i].trim() && !lines[i].startsWith(" ") && !lines[i].startsWith("\t") && !lines[i].startsWith("#")) {
      end = i;
      break;
    }
  }
  return end;
}

function applyTargetLayer(layerName) {
  const ta = $("#yaml-editor");
  const lines = ta.value.split("\n");
  const targetIdx = lines.findIndex((l) => l.replace(/\s+$/, "") === "target:");
  if (targetIdx === -1) { alert("no 'target:' block found in this pipeline yaml"); return; }
  const end = _topLevelBlockEnd(lines, targetIdx);
  const body = lines.slice(targetIdx + 1, end).filter((l) => !/^\s*layer:/.test(l));
  const next = [...lines.slice(0, targetIdx + 1), ...body, `  layer: ${layerName}`, ...lines.slice(end)];
  ta.value = next.join("\n");
  markDirty();
}

async function loadLayerPicker() {
  const box = $("#editor-layer-picker");
  box.innerHTML = "";
  let layers;
  try {
    layers = (await api("/api/lineage/layers")).layers;
  } catch {
    box.append(el("div", { class: "empty-note" }, "layers unavailable"));
    return;
  }
  editor.layers = layers.map((l) => l.name);   // feeds `layer:` completion in the yaml editor
  if (!layers.length) {
    box.append(el("div", { class: "empty-note" }, "none declared — see pipelines/layers.yaml"));
    return;
  }
  box.append(el("div", { class: "empty-note" }, "click to set the target's layer"));
  for (const l of layers) {
    const chip = el("div", { class: "col-chip", title: `set target layer → ${l.name}` }, el("span", {}, l.label));
    chip.addEventListener("click", () => applyTargetLayer(l.name));
    box.append(chip);
  }
}

// ── lineage pass-through suggestions (US3) — never auto-persisted (FR-017):
// SUGGEST only inserts draft entries into the yaml text; nothing is saved
// until SAVE + RELOAD. ──

function applyLineageSuggestions(suggestions) {
  const ta = $("#yaml-editor");
  const lines = ta.value.split("\n");
  const entryLines = suggestions.flatMap((s) => [
    `  - field: ${s.field}`,
    `    from: [${s.from.join(", ")}]`,
    `    transform: ${s.transform}`,
  ]);
  const lineageIdx = lines.findIndex((l) => l.replace(/\s+$/, "") === "lineage:");
  let next;
  if (lineageIdx === -1) {
    next = [...lines, "lineage:", ...entryLines];
  } else {
    const end = _topLevelBlockEnd(lines, lineageIdx);
    next = [...lines.slice(0, end), ...entryLines, ...lines.slice(end)];
  }
  ta.value = next.join("\n");
  markDirty();
}

async function suggestLineage() {
  if (!editor.name || editor.kind !== "pipeline") return;
  let data;
  try {
    data = await api(`/api/pipelines/${editor.name}/lineage/suggest`);
  } catch (err) {
    alert(`Could not fetch suggestions: ${err.message}`);
    return;
  }
  const suggestions = data.suggestions || [];
  if (!suggestions.length) {
    alert("No pass-through suggestions available — either every output field is already "
      + "declared, or none match a source column name.");
    return;
  }
  applyLineageSuggestions(suggestions);
}

// ── wiring (called once from main.js) ──

export function attachEditor() {
  const ta = $("#yaml-editor");
  completer = makeCompleter(ta, $("#editor-suggest"), yamlResolve, scheduleValidate);
  ta.addEventListener("input", () => { completer.update(); markDirty(); });
  ta.addEventListener("keydown", (e) => {
    if (completer.onKeydown(e)) return;      // completion popup consumed the key
    if (e.key === "Tab") { e.preventDefault(); insertAtCursor(ta, "  "); }
  });
  ta.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
  $("#editor-pick-dataset").addEventListener("click", toggleDatasetPicker);
  $("#editor-run").addEventListener("click", runPipeline);
  $("#editor-lineage-suggest").addEventListener("click", suggestLineage);
  $("#editor-revert").addEventListener("click", () => {
    ta.value = editor.original;
    editor.dirty = false;
    $("#editor-datasets").hidden = true;
    validateEditor();
  });
  // browser-level guard against navigating away / closing with unsaved edits
  window.addEventListener("beforeunload", (e) => {
    if (editor.dirty && state.view === "editor") { e.preventDefault(); e.returnValue = ""; }
  });
}
