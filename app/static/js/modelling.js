/* Modelling workspace: the home for the semantic layer (formerly the "Data"
   explorer mode). Left rail = manage fact models and common models (create /
   edit / open-in-builder); right = the datasets↔models overview (which bucket
   objects feed which models), carried over from the old explorer. All model
   authoring lives here now — Studio only builds visuals. Creation goes
   through a chooser: fact model (blank, or started from a common model) vs
   common dimension model. */
"use strict";

import { isAdmin } from "./auth.js";
import { $, api, el, fmtBytes } from "./lib.js";
import { openMemoriesModal } from "./memories.js";
import { setModelSeed } from "./modelform.js";
import { navigate, paths } from "./router.js";
import { hooks, state } from "./state.js";

const openInBuilder = (name) => navigate(paths.studioModel(name));

// yaml keys stay `joins` for compatibility; the UI vocabulary is "relation"
const roleLabel = (role) => role.replace(/^join: /, "relation: ");

export async function loadModelling() {
  $("#modelling-side").innerHTML = "";
  $("#modelling-files").innerHTML = "";
  $("#modelling-bucket").textContent = "scanning bucket…";
  const [models, bundles, pipelines, data] = await Promise.all([
    api("/api/models"), api("/api/dimensions"), api("/api/pipelines"), api("/api/explorer"),
  ]);
  state.models = models;
  state.bundles = bundles;
  $("#modelling-bucket").textContent =
    `s3://${data.bucket} @ ${data.endpoint.replace(/^https?:\/\//, "")} · ${data.files.length} objects · ${fmtBytes(data.files.reduce((s, f) => s + f.size, 0))}`;
  renderSide(models, bundles, data);
  renderPipelines(pipelines);
  renderFiles(data);
}
hooks.loadModelling = loadModelling;

const RUN_STATUS_LABEL = {
  queued: "queued", running: "running…", succeeded: "✓ succeeded", failed: "✗ failed",
  timed_out: "⏱ timed out", interrupted: "⚠ interrupted",
};

function renderPipelines(pipelines) {
  const box = $("#modelling-side");
  box.append(el("div", { class: "sec-title", style: "margin-top:16px" }, "Pipelines"));
  if (!pipelines.length) {
    box.append(el("div", { class: "empty-note" }, "none yet — hosted polars transformation scripts"));
  }
  for (const p of pipelines) {
    const latest = p.latest_run;
    const statusClass = latest?.status === "succeeded" ? "ok" : latest?.status === "failed" || latest?.status === "timed_out" ? "err" : "";
    const layerBadge = p.target.layer ? el("span", { class: "model-chip", title: "target layer" }, p.target.layer) : null;
    const top = [
      el("span", { class: "nm" }, p.label),
      el("span", { class: `fmt ${statusClass}` }, latest ? RUN_STATUS_LABEL[latest.status] || latest.status : "not run yet"),
    ];
    if (layerBadge) top.push(layerBadge);
    const card = el("div", { class: "mk-card" },
      el("div", { class: "mk-top" }, ...top),
      el("div", { class: "path" }, `${p.target.path} (${p.materialization.mode}${p.materialization.mode === "upsert" ? `/${p.materialization.on_delete}` : ""})`),
      el("div", { class: "mk-actions" },
        el("button", { class: "mini-btn", onclick: () => navigate(paths.modellingPipelineYaml(p.name)) }, "{ } yaml")));
    box.append(card);
  }
  box.append(el("button", { class: "ghost mk-new", onclick: () => navigate(paths.modellingNewPipelineYaml()) }, "+ new pipeline"));
}

function renderSide(models, bundles, data) {
  const box = $("#modelling-side");
  const stats = Object.fromEntries(data.models.map((m) => [m.name, m]));

  box.append(el("div", { class: "sec-title" }, "Models"));
  for (const m of models) {
    const st = stats[m.name] || { files: 0, bytes: 0 };
    const card = el("div", { class: "mk-card" },
      el("div", { class: "mk-top" },
        el("span", { class: "nm" }, m.label),
        el("span", { class: "fmt" }, m.format)),
      el("div", { class: "path" }, m.path),
      el("div", { class: "mk-sub" }, `${st.files} file${st.files === 1 ? "" : "s"} · ${fmtBytes(st.bytes)} · ${m.dimensions.length} dims · ${m.measures.length} measures`),
      el("div", { class: "mk-actions" },
        el("button", { class: "mini-btn", onclick: () => navigate(paths.modellingModel(m.name)) }, "✎ edit"),
        el("button", { class: "mini-btn", title: "edit the raw yaml", onclick: () => navigate(paths.modellingModelYaml(m.name)) }, "{ } yaml"),
        // curate what the chat assistant remembers about this model —
        // memory mutations are admin-gated server-side, so only admins
        // ever see the entry point
        ...(isAdmin()
          ? [el("button", { class: "mini-btn", title: "chat-learned memories (synonyms, notes) for this model", onclick: () => openMemoriesModal(m) }, "◈ memory")]
          : []),
        el("button", { class: "mini-btn go", onclick: () => openInBuilder(m.name) }, "build ►")));
    box.append(card);
  }
  box.append(el("button", { class: "ghost mk-new", onclick: () => openCreateChooser(bundles) }, "+ new fact model"));

  box.append(el("div", { class: "sec-title", style: "margin-top:16px" }, "Common Models"));
  if (!bundles.length) {
    box.append(el("div", { class: "empty-note" }, "none yet — shared dimensions across models"));
  }
  for (const b of bundles) {
    const card = el("div", { class: "mk-card" },
      el("div", { class: "mk-top" },
        el("span", { class: "nm" }, b.label),
        el("span", { class: "fmt" }, `${b.datasets.length} set${b.datasets.length === 1 ? "" : "s"}`)),
      el("div", { class: "path" }, b.datasets.map((d) => d.name).join(", ") || "—"),
      el("div", { class: "mk-actions" },
        el("button", { class: "mini-btn", onclick: () => navigate(paths.modellingBundle(b.name)) }, "✎ edit"),
        el("button", { class: "mini-btn", title: "edit the raw yaml", onclick: () => navigate(paths.modellingBundleYaml(b.name)) }, "{ } yaml")));
    box.append(card);
  }
  box.append(el("button", { class: "ghost mk-new", onclick: () => navigate(paths.modellingNewBundle()) }, "+ new common model"));
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

// datasets↔models overview (carried over verbatim from the old explorer)
function renderFiles(data) {
  const table = el("table", { class: "data" });
  const head = el("tr");
  for (const h of ["Key", "Size", "Modified", "Models"]) {
    head.append(el("th", { class: h === "Size" ? "num" : "" }, h));
  }
  table.append(el("thead", {}, head));
  const body = el("tbody");
  for (const f of data.files.sort((a, b) => a.key.localeCompare(b.key))) {
    const models = el("td");
    const seen = new Set();
    for (const hit of f.models) {
      const key = hit.model + "|" + hit.role;
      if (seen.has(key)) continue;
      seen.add(key);
      const chip = el("span", {
        class: "model-chip" + (hit.role.startsWith("join") ? " join" : ""),
        title: roleLabel(hit.role) + " — open in builder",
      }, hit.model);
      chip.addEventListener("click", () => openInBuilder(hit.model));
      models.append(chip);
    }
    if (!f.models.length) models.append(el("span", { class: "unmapped" }, "unmapped — no model reads this"));
    const tr = el("tr");
    tr.append(
      el("td", {}, f.key),
      el("td", { class: "num" }, fmtBytes(f.size)),
      el("td", {}, f.modified.slice(0, 10)),
      models);
    body.append(tr);
  }
  table.append(body);
  $("#modelling-files").append(table);
}
