/* Modelling workspace: the home for the semantic layer (formerly the "Data"
   explorer mode). Left rail = the bucket's datasets as a collapsible folder
   tree (which objects feed which models); center = manage fact models,
   common models and pipelines (create / edit / open-in-builder), each its
   own collapsible section. All model authoring lives here now — Studio only
   builds visuals. Creation goes through a chooser: fact model (blank, or
   started from a common model) vs common dimension model. */
"use strict";

import { $, api, el, fmtBytes } from "./lib.js";
import { setModelSeed } from "./modelform.js";
import { navigate, paths } from "./router.js";
import { hooks, state } from "./state.js";

export async function loadModelling() {
  $("#modelling-datasets").innerHTML = "";
  $("#modelling-main").innerHTML = "";
  $("#modelling-bucket").textContent = "scanning bucket…";
  const [models, bundles, pipelines, datasets] = await Promise.all([
    api("/api/models"), api("/api/dimensions"), api("/api/pipelines"), api("/api/datasets"),
  ]);
  state.models = models;
  state.bundles = bundles;
  $("#modelling-bucket").textContent =
    `s3://${datasets.bucket} @ ${datasets.endpoint.replace(/^https?:\/\//, "")} · ${datasets.object_count} objects · ${fmtBytes(datasets.bytes)}`;
  renderDatasetTree(datasets.datasets);
  renderSide(models, bundles, datasets);
  renderPipelines(pipelines);
}
hooks.loadModelling = loadModelling;

// ── datasets: bucket objects, grouped into pickable datasets (same grouping
// the model-authoring source picker uses — a delta table or glob prefix
// stays one node), laid out as a collapsible folder tree keyed by path ──

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

function datasetFolder(node, depth) {
  const children = el("div", { class: "tree-children" });
  if (node.dataset) children.append(renderDatasetLeaf(node.dataset, "(this level)"));
  for (const child of sortedChildren(node)) {
    children.append(child.children.size > 0 ? datasetFolder(child, depth + 1) : renderDatasetLeaf(child.dataset, child.name));
  }
  const attrs = { class: "tree-folder" };
  if (depth === 0) attrs.open = "";   // top-level folders start open; deeper nesting stays tucked away
  return el("details", attrs,
    el("summary", {},
      el("span", { class: "tree-caret" }, "▸"),
      el("span", { class: "nm" }, node.name),
      el("span", { class: "tree-count" }, String(countDatasets(node)))),
    children);
}

function renderDatasetTree(datasets) {
  const box = $("#modelling-datasets");
  box.append(el("div", { class: "sec-title" }, "Datasets"));
  if (!datasets.length) {
    box.append(el("div", { class: "empty-note" }, "bucket is empty"));
    return;
  }
  const root = datasetTree(datasets);
  if (root.dataset) box.append(renderDatasetLeaf(root.dataset, "(bucket root)"));
  for (const child of sortedChildren(root)) {
    box.append(child.children.size > 0 ? datasetFolder(child, 0) : renderDatasetLeaf(child.dataset, child.name));
  }
}

const RUN_STATUS_LABEL = {
  queued: "queued", running: "running…", succeeded: "✓ succeeded", failed: "✗ failed",
  timed_out: "⏱ timed out", interrupted: "⚠ interrupted",
};

// a collapsible <details> section for the center column: caret + label +
// count in a .sec-title summary, a card grid body, tucked-in create button
function mkSection(label, count, cards, emptyNote, newBtn) {
  const body = el("div", { class: "mk-section-body" });
  if (cards.length) body.append(el("div", { class: "mf-ds-grid" }, ...cards));
  else if (emptyNote) body.append(el("div", { class: "empty-note" }, emptyNote));
  body.append(newBtn);
  return el("details", { class: "mk-section", open: "" },
    el("summary", { class: "sec-title" },
      el("span", { class: "tree-caret" }, "▸"),
      el("span", {}, label),
      el("span", { class: "tree-count" }, String(count))),
    body);
}

function renderPipelines(pipelines) {
  const cards = pipelines.map((p) => {
    const latest = p.latest_run;
    const statusClass = latest?.status === "succeeded" ? "ok" : latest?.status === "failed" || latest?.status === "timed_out" ? "err" : "";
    const layerBadge = p.target.layer ? el("span", { class: "model-chip", title: "target layer" }, p.target.layer) : null;
    const top = [
      el("span", { class: "nm" }, p.label),
      el("span", { class: `fmt ${statusClass}` }, latest ? RUN_STATUS_LABEL[latest.status] || latest.status : "not run yet"),
    ];
    if (layerBadge) top.push(layerBadge);
    const card = el("div", { class: "mk-card clickable" },
      el("div", { class: "mk-top" }, ...top),
      el("div", { class: "path" }, `${p.target.path} (${p.materialization.mode}${p.materialization.mode === "upsert" ? `/${p.materialization.on_delete}` : ""})`));
    card.addEventListener("click", () => navigate(paths.modellingPipelineYaml(p.name)));
    return card;
  });
  const newBtn = el("button", { class: "ghost mk-new", onclick: () => navigate(paths.modellingNewPipelineYaml()) }, "+ new pipeline");
  $("#modelling-main").append(mkSection("Pipelines", pipelines.length, cards, "none yet — hosted polars transformation scripts", newBtn));
}

function renderSide(models, bundles, data) {
  const box = $("#modelling-main");
  const stats = Object.fromEntries(data.models.map((m) => [m.name, m]));

  const modelCards = models.map((m) => {
    const st = stats[m.name] || { files: 0, bytes: 0 };
    const card = el("div", { class: "mk-card clickable" },
      el("div", { class: "nm" }, m.label),
      el("div", { class: "path" }, m.path),
      el("div", { class: "mk-sub" }, `${st.files} file${st.files === 1 ? "" : "s"} · ${fmtBytes(st.bytes)} · ${m.dimensions.length} dims · ${m.measures.length} measures`));
    card.addEventListener("click", () => navigate(paths.modellingModel(m.name)));
    return card;
  });
  const newModelBtn = el("button", { class: "ghost mk-new", onclick: () => openCreateChooser(bundles) }, "+ new fact model");
  box.append(mkSection("Models", models.length, modelCards, "", newModelBtn));

  const bundleCards = bundles.map((b) => {
    const card = el("div", { class: "mk-card clickable" },
      el("div", { class: "mk-top" },
        el("span", { class: "nm" }, b.label),
        el("span", { class: "fmt" }, `${b.datasets.length} set${b.datasets.length === 1 ? "" : "s"}`)),
      el("div", { class: "path" }, b.datasets.map((d) => d.name).join(", ") || "—"));
    card.addEventListener("click", () => navigate(paths.modellingBundle(b.name)));
    return card;
  });
  const newBundleBtn = el("button", { class: "ghost mk-new", onclick: () => navigate(paths.modellingNewBundle()) }, "+ new common model");
  box.append(mkSection("Common Models", bundles.length, bundleCards, "none yet — shared dimensions across models", newBundleBtn));
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
    el("div", { class: "cc-body" }, fact, common)));
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
