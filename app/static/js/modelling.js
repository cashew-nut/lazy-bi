/* Modelling workspace: the home for the semantic layer (formerly the "Data"
   explorer mode). Left rail = the bucket's datasets as a collapsible folder
   tree (which objects feed which models); center = manage fact models,
   common models and pipelines (create / edit / open-in-builder), each its
   own collapsible section. All model authoring lives here now — Studio only
   builds visuals. Creation goes through a chooser: fact model (blank, or
   started from a common model) vs common dimension model. */
"use strict";

import { isAdmin } from "./auth.js";
import { $, api, el, fmtBytes } from "./lib.js";
import { uploadRow } from "./formkit.js";
import { setModelSeed } from "./modelform.js";
import { navigate, paths } from "./router.js";
import { hooks, state } from "./state.js";

let lastModels = [], lastBundles = [], lastPipelines = [], lastDatasetStats = {};

// Upload a dataset independent of building any particular model on it — a
// user may want a file staged for a common model, another for a fact model,
// or just to poke at with the schema endpoint before deciding. The row
// itself is stateless (it only needs a fresh dataset listing after a
// successful upload), so it's built once and left in place across reloads.
function renderUploadRow() {
  const host = $("#modelling-upload");
  if (!host || host.childElementCount) return;
  host.append(uploadRow(() => loadModelling(), { compact: true, label: "UPLOAD A DATASET" }));
}

export async function loadModelling() {
  $("#modelling-bucket").textContent = "scanning bucket…";
  renderUploadRow();
  const [models, bundles, pipelines, datasets] = await Promise.all([
    api("/api/models"), api("/api/dimensions"), api("/api/pipelines"), api("/api/datasets"),
  ]);
  state.models = models;
  state.bundles = bundles;
  $("#modelling-bucket").textContent =
    `s3://${datasets.bucket} @ ${datasets.endpoint.replace(/^https?:\/\//, "")} · ${datasets.object_count} objects · ${fmtBytes(datasets.bytes)}`;
  renderDatasetTree(datasets.datasets);
  lastModels = models;
  lastBundles = bundles;
  lastPipelines = pipelines;
  lastDatasetStats = Object.fromEntries(datasets.models.map((m) => [m.name, m]));
  renderModelsList();
  renderBundlesList();
  renderPipelinesList();
}
hooks.loadModelling = loadModelling;

const matchesQuery = (q, ...fields) => !q || fields.some((f) => f && f.toLowerCase().includes(q));

// ── datasets: bucket objects, grouped into pickable datasets (same grouping
// the model-authoring source picker uses — a delta table or glob prefix
// stays one node), laid out as a collapsible folder tree keyed by path —
// collapsed by default (US: information overload), with type-to-filter
// pruning the tree down to matching branches and force-opening what's left ──

let dsFilter = "";
let lastDatasets = [];

export function setDatasetFilter(text) {
  dsFilter = text.trim().toLowerCase();
  renderDatasetTree(lastDatasets);
}

const dsMatches = (ds, q) => ds.path.toLowerCase().includes(q) || (ds.key || "").toLowerCase().includes(q);

// filtered copy of a tree node keeping only branches with a matching
// dataset somewhere inside; null when nothing in this branch matches
function pruneTree(node, q) {
  const dataset = node.dataset && dsMatches(node.dataset, q) ? node.dataset : null;
  const children = new Map();
  for (const [key, child] of node.children) {
    const kept = pruneTree(child, q);
    if (kept) children.set(key, kept);
  }
  if (!dataset && children.size === 0) return null;
  return { name: node.name, dataset, children };
}

function datasetTree(datasets) {
  const root = { name: "", children: new Map(), dataset: null };
  for (const ds of datasets) {
    if (!ds.key) { root.dataset = ds; continue; }
    let node = root;
    for (const seg of ds.key.split("/")) {
      if (!node.children.has(seg)) node.children.set(seg, { name: seg, children: new Map(), dataset: null });
      node = node.children.get(seg);
    }
    node.dataset = ds;
  }
  return root;
}

const sortedChildren = (node) => [...node.children.values()].sort((a, b) => {
  const af = a.children.size > 0;
  const bf = b.children.size > 0;
  return af === bf ? a.name.localeCompare(b.name) : af ? -1 : 1;   // folders before datasets
});

const countDatasets = (node) => (node.dataset ? 1 : 0)
  + [...node.children.values()].reduce((n, c) => n + countDatasets(c), 0);

const objCount = (ds) => `${ds.object_count} obj · ${fmtBytes(ds.bytes)}`;

// a single-object dataset: name + object count/size, nothing else
function datasetLeaf(ds, label) {
  return el("div", { class: "tree-leaf", title: ds.path },
    el("div", { class: "nm" }, label),
    el("div", { class: "tree-leaf-sub" }, objCount(ds)));
}

// a multi-object dataset: the same name + count/size, expandable to list
// the individual objects backing it
function datasetLeafExpand(ds, label) {
  const objRows = ds.objects.map((o) => el("div", { class: "tree-object" },
    el("span", { class: "nm" }, o.key.split("/").pop()),
    el("span", {}, fmtBytes(o.size))));
  return el("details", { class: "tree-leaf-expand", title: ds.path },
    el("summary", {},
      el("span", { class: "tree-caret" }, "▸"),
      el("div", { class: "tree-leaf-info" },
        el("div", { class: "nm" }, label),
        el("div", { class: "tree-leaf-sub" }, objCount(ds)))),
    el("div", { class: "tree-object-list" }, ...objRows));
}

const renderDatasetLeaf = (ds, label) => (ds.objects.length > 1 ? datasetLeafExpand : datasetLeaf)(ds, label);

// forceOpen is set only while a filter is active, so the pruned matches are
// immediately visible instead of hidden behind their (default-collapsed)
// ancestor folders
function datasetFolder(node, forceOpen) {
  const children = el("div", { class: "tree-children" });
  if (node.dataset) children.append(renderDatasetLeaf(node.dataset, "(this level)"));
  for (const child of sortedChildren(node)) {
    children.append(child.children.size > 0 ? datasetFolder(child, forceOpen) : renderDatasetLeaf(child.dataset, child.name));
  }
  const attrs = { class: "tree-folder" };
  if (forceOpen) attrs.open = "";
  return el("details", attrs,
    el("summary", {},
      el("span", { class: "tree-caret" }, "▸"),
      el("span", { class: "nm" }, node.name),
      el("span", { class: "tree-count" }, String(countDatasets(node)))),
    children);
}

function renderDatasetTree(datasets) {
  lastDatasets = datasets;
  const box = $("#modelling-datasets");
  box.innerHTML = "";
  if (!datasets.length) {
    box.append(el("div", { class: "empty-note" }, "bucket is empty"));
    return;
  }
  const root = datasetTree(datasets);
  const tree = dsFilter ? pruneTree(root, dsFilter) : root;
  if (!tree) { box.append(el("div", { class: "empty-note" }, "no matches")); return; }
  if (tree.dataset) box.append(renderDatasetLeaf(tree.dataset, "(bucket root)"));
  for (const child of sortedChildren(tree)) {
    box.append(child.children.size > 0 ? datasetFolder(child, !!dsFilter) : renderDatasetLeaf(child.dataset, child.name));
  }
}

const RUN_STATUS_LABEL = {
  queued: "queued", running: "running…", succeeded: "✓ succeeded", failed: "✗ failed",
  timed_out: "⏱ timed out", interrupted: "⚠ interrupted",
};

// ── center column: models / common models / pipelines, each a collapsed-
// by-default section (see index.html) whose body is a plain filterable list
// row per entity — full detail (path, target, dataset breakdown) moves to
// the row's hover title instead of sitting on the page permanently ──

let modelsFilter = "", bundlesFilter = "", pipelinesFilter = "";
export function setModelsFilter(text) { modelsFilter = text.trim().toLowerCase(); renderModelsList(); }
export function setBundlesFilter(text) { bundlesFilter = text.trim().toLowerCase(); renderBundlesList(); }
export function setPipelinesFilter(text) { pipelinesFilter = text.trim().toLowerCase(); renderPipelinesList(); }

// only a local (unlocked) model is deletable — a built-in one 403s anyway,
// so admins never see a button that can only fail
function modelDeleteBtn(m) {
  const btn = el("button", { class: "mini-btn", title: `delete '${m.label}'` }, "✕");
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();   // don't also trigger the row's navigate-to-edit
    if (!confirm(`Delete model '${m.label}'? Saved visuals pointing at it will stop working.`)) return;
    try {
      await api(`/api/models/${m.name}`, { method: "DELETE" });
    } catch (err) {
      alert(`Delete failed: ${err.message}`);
      return;
    }
    await loadModelling();
  });
  return btn;
}

function renderModelsList() {
  $("#mk-models-count").textContent = String(lastModels.length);
  const box = $("#mk-models-list");
  box.innerHTML = "";
  if (!lastModels.length) { box.append(el("div", { class: "empty-note" }, "none yet")); return; }
  const models = lastModels.filter((m) => matchesQuery(modelsFilter, m.label, m.name));
  if (!models.length) { box.append(el("div", { class: "empty-note" }, "no matches")); return; }
  for (const m of models) {
    const st = lastDatasetStats[m.name] || { files: 0, bytes: 0 };
    // a multi-fact model has no source of its own: it reads no bucket objects,
    // so it describes itself by its facts instead. Either shape goes straight
    // to the yaml editor — the guided form edits one fact table and has no
    // control for `facts:`, so a save through it would drop the list.
    const composite = m.kind === "composite";
    const borrows = m.facts.length > 0;
    const title = composite
      ? `multi-fact · ${m.facts.map((f) => f.model).join(" + ")}`
      : `${m.path}\n${st.files} file${st.files === 1 ? "" : "s"} · ${fmtBytes(st.bytes)}`
        + (borrows ? `\n+ ${m.facts.map((f) => f.model).join(", ")}` : "");
    const meta = composite
      ? `${m.facts.length} facts · ${m.dimensions.length} shared dims · ${m.measures.length} measures`
      : `${m.dimensions.length} dims · ${m.measures.length} measures`
        + (borrows ? ` · + ${m.facts.length} fact${m.facts.length === 1 ? "" : "s"}` : "");
    const row = el("div", { class: "mk-row clickable", title },
      el("span", { class: "nm" }, m.label),
      ...(composite ? [el("span", { class: "mk-tag" }, "multi-fact")] : []),
      el("span", { class: "mk-meta" }, meta),
      ...(!m.locked && isAdmin() ? [modelDeleteBtn(m)] : []));
    row.addEventListener("click", () => navigate(
      composite || borrows ? paths.modellingModelYaml(m.name) : paths.modellingModel(m.name)));
    box.append(row);
  }
}

function renderBundlesList() {
  $("#mk-bundles-count").textContent = String(lastBundles.length);
  const box = $("#mk-bundles-list");
  box.innerHTML = "";
  if (!lastBundles.length) { box.append(el("div", { class: "empty-note" }, "none yet — shared dimensions across models")); return; }
  const bundles = lastBundles.filter((b) => matchesQuery(bundlesFilter, b.label, b.name));
  if (!bundles.length) { box.append(el("div", { class: "empty-note" }, "no matches")); return; }
  for (const b of bundles) {
    const row = el("div", { class: "mk-row clickable", title: b.datasets.map((d) => d.name).join(", ") || "—" },
      el("span", { class: "nm" }, b.label),
      el("span", { class: "mk-meta" }, `${b.datasets.length} set${b.datasets.length === 1 ? "" : "s"}`));
    row.addEventListener("click", () => navigate(paths.modellingBundle(b.name)));
    box.append(row);
  }
}

function renderPipelinesList() {
  $("#mk-pipelines-count").textContent = String(lastPipelines.length);
  const box = $("#mk-pipelines-list");
  box.innerHTML = "";
  if (!lastPipelines.length) { box.append(el("div", { class: "empty-note" }, "none yet — hosted polars transformation scripts")); return; }
  const pipelines = lastPipelines.filter((p) => matchesQuery(pipelinesFilter, p.label, p.name));
  if (!pipelines.length) { box.append(el("div", { class: "empty-note" }, "no matches")); return; }
  for (const p of pipelines) {
    const latest = p.latest_run;
    const statusClass = latest?.status === "succeeded" ? "ok" : latest?.status === "failed" || latest?.status === "timed_out" ? "err" : "";
    const statusLabel = latest ? RUN_STATUS_LABEL[latest.status] || latest.status : "not run yet";
    const row = el("div", {
      class: "mk-row clickable",
      title: `${p.target.path} (${p.materialization.mode}${p.materialization.mode === "upsert" ? `/${p.materialization.on_delete}` : ""})`
        + (p.target.layer ? ` · layer: ${p.target.layer}` : ""),
    },
      el("span", { class: "nm" }, p.label),
      el("span", { class: `mk-meta ${statusClass}` }, statusLabel));
    row.addEventListener("click", () => navigate(paths.modellingPipelineYaml(p.name)));
    box.append(row);
  }
}

// ── create chooser: fact model (blank / seeded) vs common dimension model ──

function closeCreateChooser() {
  $("#create-modal").hidden = true;
  $("#create-modal").innerHTML = "";
}

export function openCreateChooser(bundles = state.bundles) {
  const overlay = $("#create-modal");
  overlay.innerHTML = "";

  const close = el("button", { class: "btn" }, "✕ CLOSE");
  close.addEventListener("click", closeCreateChooser);

  const go = (path, seed = null) => {
    setModelSeed(seed);
    closeCreateChooser();
    navigate(path);
  };

  const factStart = el("div", { class: "cc-start" },
    el("span", { class: "field-label" }, "START FROM"));
  const blank = el("button", { class: "chip" },
    el("span", { class: "tick" }, "▢"), el("span", { class: "lbl" }, "blank"));
  blank.addEventListener("click", () => go(paths.modellingNewModel()));
  factStart.append(blank);
  for (const b of bundles) {
    const chip = el("button", { class: "chip", title: `import '${b.label}' from the start — its shared dimensions arrive ready to relate` },
      el("span", { class: "tick" }, "◈"), el("span", { class: "lbl" }, b.label));
    chip.addEventListener("click", () => go(paths.modellingNewModel(), b.name));
    factStart.append(chip);
  }

  const fact = el("div", { class: "cc-option" },
    el("div", { class: "cc-name" }, "FACT MODEL"),
    el("div", { class: "cc-desc" }, "A dataset you measure — orders, shipments, spend. Declares dimensions "
      + "and measures; queried from Studio, dashboards and Chat."),
    factStart);

  // a multi-fact model has no source to pick, so it starts in the yaml editor
  // (which seeds the `facts:` template) rather than the guided form
  const mkMulti = el("button", { class: "btn alt" }, "+ CREATE MULTI-FACT MODEL");
  mkMulti.addEventListener("click", () => go(paths.modellingNewModelYaml()));
  const multi = el("div", { class: "cc-option" },
    el("div", { class: "cc-name" }, "MULTI-FACT MODEL"),
    el("div", { class: "cc-desc" }, "Several fact models read on one axis — spend, revenue and actives on "
      + "the same chart. They are never joined to each other: each is queried on its own and the results "
      + "merge on the dimensions they share, so no measure inflates."),
    el("div", { class: "cc-start" }, mkMulti));

  const mkCommon = el("button", { class: "btn alt" }, "+ CREATE COMMON MODEL");
  mkCommon.addEventListener("click", () => go(paths.modellingNewBundle()));
  const common = el("div", { class: "cc-option" },
    el("div", { class: "cc-name" }, "COMMON DIMENSION MODEL"),
    el("div", { class: "cc-desc" }, "Shared dimensions — geography, calendars, hierarchies — declared once "
      + "and imported by any fact model. No measures; importers see its dimensions read-only."),
    el("div", { class: "cc-start" }, mkCommon));

  overlay.append(el("div", { class: "mm-card cc-card" },
    el("div", { class: "chart-head" },
      el("span", { class: "editor-file" }, "create"),
      el("span", { style: "flex:1" }), close),
    el("div", { class: "cc-body" }, fact, multi, common)));
  overlay.hidden = false;
  overlay.onclick = (e) => { if (e.target === overlay) closeCreateChooser(); };
}
hooks.openCreateChooser = openCreateChooser;

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#create-modal").hidden) closeCreateChooser();
});

// ── layers editor (US3): the optional, deployment-wide ordered layer list
// (bronze/silver/gold, or any naming a deployment prefers) pipelines assign
// their sources/target to. Reuses the same overlay as the create chooser. ──

export async function openLayersModal() {
  const overlay = $("#create-modal");
  overlay.innerHTML = "";

  let rows;
  try {
    rows = (await api("/api/lineage/layers")).layers.map((l) => ({ ...l }));
  } catch {
    rows = [];
  }

  const body = el("div", { class: "cc-body" });

  const renderRows = () => {
    body.innerHTML = "";
    rows.forEach((row, i) => {
      const nameInput = el("input", { value: row.name, placeholder: "name (a-z0-9_)" });
      nameInput.addEventListener("input", (e) => { row.name = e.target.value; });
      const labelInput = el("input", { value: row.label || "", placeholder: "label (optional)" });
      labelInput.addEventListener("input", (e) => { row.label = e.target.value; });
      const up = el("button", { class: "mini-btn", title: "move up", disabled: i === 0 ? "" : undefined },
        "▲");
      up.addEventListener("click", () => { [rows[i - 1], rows[i]] = [rows[i], rows[i - 1]]; renderRows(); });
      const down = el("button", {
        class: "mini-btn", title: "move down", disabled: i === rows.length - 1 ? "" : undefined,
      }, "▼");
      down.addEventListener("click", () => { [rows[i], rows[i + 1]] = [rows[i + 1], rows[i]]; renderRows(); });
      const remove = el("button", { class: "mini-btn", title: "remove" }, "✕");
      remove.addEventListener("click", () => { rows.splice(i, 1); renderRows(); });
      body.append(el("div", { class: "layer-row" }, nameInput, labelInput, up, down, remove));
    });
  };
  renderRows();

  const addBtn = el("button", { class: "ghost mk-new" }, "+ add layer");
  addBtn.addEventListener("click", () => { rows.push({ name: "", label: "" }); renderRows(); });

  const status = el("span", {});
  const save = el("button", { class: "btn alt" }, "SAVE");
  save.addEventListener("click", async () => {
    const payload = rows.filter((r) => r.name.trim());
    try {
      await api("/api/lineage/layers", { method: "PUT", body: { layers: payload } });
      closeLayersModal();
      if (hooks.loadModelling) await hooks.loadModelling();
    } catch (err) {
      status.textContent = err.message;
      status.className = "err";
    }
  });
  const close = el("button", { class: "btn" }, "✕ CLOSE");
  close.addEventListener("click", closeLayersModal);

  overlay.append(el("div", { class: "mm-card cc-card" },
    el("div", { class: "chart-head" },
      el("span", { class: "editor-file" }, "layers"),
      status,
      el("span", { style: "flex:1" }),
      save, close),
    body,
    el("div", { style: "padding:0 16px 16px" }, addBtn)));
  overlay.hidden = false;
  overlay.onclick = (e) => { if (e.target === overlay) closeLayersModal(); };
}
hooks.openLayersModal = openLayersModal;

function closeLayersModal() {
  $("#create-modal").hidden = true;
  $("#create-modal").innerHTML = "";
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#create-modal").hidden) closeLayersModal();
});
