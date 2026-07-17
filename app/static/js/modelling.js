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
  const [models, bundles, data] = await Promise.all([
    api("/api/models"), api("/api/dimensions"), api("/api/explorer"),
  ]);
  state.models = models;
  state.bundles = bundles;
  $("#modelling-bucket").textContent =
    `s3://${data.bucket} @ ${data.endpoint.replace(/^https?:\/\//, "")} · ${data.files.length} objects · ${fmtBytes(data.files.reduce((s, f) => s + f.size, 0))}`;
  renderSide(models, bundles, data);
  renderFiles(data);
}
hooks.loadModelling = loadModelling;

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
