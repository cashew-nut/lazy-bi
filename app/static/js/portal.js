/* Portal: the consumption surface — a nested folder tree of published
   dashboards, opened read-only via dashboard.js. */
"use strict";

import { openDashboard } from "./dashboard.js";
import { $, el } from "./lib.js";
import { refreshPubs, state } from "./state.js";

export async function loadPortal() {
  await refreshPubs();
  renderPortal();
}

export function renderPortal() {
  const grid = $("#portal-grid");
  const crumbs = $("#portal-crumbs");
  grid.innerHTML = "";
  crumbs.innerHTML = "";
  const cur = state.portalFolder;

  const rootLink = el("a", {}, "◉ portal");
  rootLink.addEventListener("click", () => { state.portalFolder = ""; renderPortal(); });
  crumbs.append(rootLink);
  const acc = [];
  for (const seg of cur.split("/").filter(Boolean)) {
    acc.push(seg);
    const path = acc.join("/");
    const link = el("a", {}, seg);
    link.addEventListener("click", () => { state.portalFolder = path; renderPortal(); });
    crumbs.append(" / ", link);
  }

  // direct subfolders of the current path, derived from publication folders
  const prefix = cur ? cur + "/" : "";
  const subs = new Set();
  for (const p of state.publications) {
    if (p.folder !== cur && p.folder.startsWith(prefix)) {
      subs.add(p.folder.slice(prefix.length).split("/")[0]);
    }
  }
  for (const name of [...subs].sort()) {
    const inside = state.publications.filter((p) => p.folder === (prefix + name) || p.folder.startsWith(prefix + name + "/")).length;
    const card = el("div", { class: "p-card folder" },
      el("div", { class: "ic" }, "◫"),
      el("div", { class: "nm" }, name),
      el("div", { class: "sub" }, `${inside} dashboard${inside === 1 ? "" : "s"}`));
    card.addEventListener("click", () => { state.portalFolder = prefix + name; renderPortal(); });
    grid.append(card);
  }
  for (const p of state.publications.filter((x) => x.folder === cur)) {
    const card = el("div", { class: "p-card dash" },
      el("div", { class: "ic" }, "▦"),
      el("div", { class: "nm" }, p.name),
      el("div", { class: "sub" }, `${p.tiles} tiles · published ${p.published_at.slice(0, 10)}`));
    card.addEventListener("click", () => openDashboard(p.dashboard_id, true));
    grid.append(card);
  }
  if (!grid.children.length) {
    grid.append(el("div", { class: "msg", style: "grid-column: 1 / -1" },
      "nothing published here yet — publish a dashboard from the studio"));
  }
}
