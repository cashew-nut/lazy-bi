/* Modelling workspace: the home for the semantic layer (formerly the "Data"
   explorer mode). Left rail = manage fact models and common models (create /
   edit / open-in-builder); right = the datasets↔models overview (which bucket
   objects feed which models), carried over from the old explorer. All model
   authoring lives here now — Studio only builds visuals. */
"use strict";

import { selectModel } from "./builder.js";
import { openBundleForm } from "./bundleform.js";
import { openEditor } from "./editor.js";
import { $, api, el, fmtBytes } from "./lib.js";
import { openModelForm } from "./modelform.js";
import { hooks, showView, state } from "./state.js";

const openInBuilder = (name) => { showView("builder"); selectModel(name); };

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
        el("button", { class: "mini-btn", onclick: () => openModelForm(m.name) }, "✎ edit"),
        el("button", { class: "mini-btn", title: "edit the raw yaml", onclick: () => openEditor("model", m.name) }, "{ } yaml"),
        el("button", { class: "mini-btn go", onclick: () => openInBuilder(m.name) }, "build ►")));
    box.append(card);
  }
  box.append(el("button", { class: "ghost mk-new", onclick: () => openModelForm(null) }, "+ new model"));

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
        el("button", { class: "mini-btn", onclick: () => openBundleForm(b.name) }, "✎ edit"),
        el("button", { class: "mini-btn", title: "edit the raw yaml", onclick: () => openEditor("bundle", b.name) }, "{ } yaml")));
    box.append(card);
  }
  box.append(el("button", { class: "ghost mk-new", onclick: () => openBundleForm(null) }, "+ new common model"));
}

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
        title: hit.role + " — open in builder",
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
