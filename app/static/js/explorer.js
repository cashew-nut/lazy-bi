/* Data explorer: bucket objects mapped to the models that read them. */
"use strict";

import { selectModel } from "./builder.js";
import { $, api, el, fmtBytes } from "./lib.js";
import { showView } from "./state.js";

export async function loadExplorer() {
  $("#explorer-models").innerHTML = "";
  $("#explorer-files").innerHTML = "";
  $("#explorer-bucket").textContent = "scanning bucket…";
  const data = await api("/api/explorer");
  $("#explorer-bucket").textContent =
    `s3://${data.bucket} @ ${data.endpoint.replace(/^https?:\/\//, "")} · ${data.files.length} objects · ${fmtBytes(data.files.reduce((s, f) => s + f.size, 0))}`;

  const openInBuilder = (name) => { showView("builder"); selectModel(name); };

  const mbox = $("#explorer-models");
  for (const m of data.models) {
    const card = el("div", { class: "x-model", title: "open in the builder" },
      el("span", { class: "fmt" }, m.format),
      el("div", { class: "nm" }, m.label),
      el("div", { class: "path" }, m.path),
      ...m.joins.map((j) => el("div", { class: "join-line" }, `⤷ join ${j.name}: ${j.path} (${j.format})`)),
      el("div", { class: "stats" }, `${m.files} file${m.files === 1 ? "" : "s"} · ${fmtBytes(m.bytes)}`));
    card.addEventListener("click", () => openInBuilder(m.name));
    mbox.append(card);
  }

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
  $("#explorer-files").append(table);
}
