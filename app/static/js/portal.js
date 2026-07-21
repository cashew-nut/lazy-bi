/* Portal: the consumption surface — a nested folder tree of published
   dashboards, opened read-only via dashboard.js. Only the current folder's
   direct children (subfolders + dashboards) are shown, split into two
   collapsed-by-default sections; drilling into a subfolder still navigates
   via breadcrumbs rather than expanding in place (spec 004: breadcrumb-only
   navigation). */
"use strict";

import { $, el } from "./lib.js";
import { navigate, paths } from "./router.js";
import { hooks, refreshPubs, showView, state } from "./state.js";

export async function loadPortal() {
  await refreshPubs();
  renderPortal();
}

let folderFilter = "", dashFilter = "";
export function setPortalFolderFilter(text) { folderFilter = text.trim().toLowerCase(); renderPortal(); }
export function setPortalDashFilter(text) { dashFilter = text.trim().toLowerCase(); renderPortal(); }

function resetPortalFilters() {
  folderFilter = ""; dashFilter = "";
  if ($("#portal-folders-filter")) $("#portal-folders-filter").value = "";
  if ($("#portal-dashboards-filter")) $("#portal-dashboards-filter").value = "";
}

// router entry point for /portal and /portal/folder/*path — see router.js
export async function openPortalFolder(path) {
  state.portalFolder = path;
  resetPortalFilters();
  showView("portal");
  await loadPortal();
}
hooks.openPortalFolder = openPortalFolder;

export function renderPortal() {
  const crumbs = $("#portal-crumbs");
  crumbs.innerHTML = "";
  const cur = state.portalFolder;

  const rootLink = el("a", {}, "◉ portal");
  rootLink.addEventListener("click", () => navigate(paths.portalFolder("")));
  crumbs.append(rootLink);
  const acc = [];
  for (const seg of cur.split("/").filter(Boolean)) {
    acc.push(seg);
    const path = acc.join("/");
    const link = el("a", {}, seg);
    link.addEventListener("click", () => navigate(paths.portalFolder(path)));
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
  const folderNames = [...subs].sort();
  const dashes = state.publications.filter((p) => p.folder === cur);

  $("#portal-folders-count").textContent = String(folderNames.length);
  $("#portal-dashboards-count").textContent = String(dashes.length);

  const folderBox = $("#portal-folders-list");
  folderBox.innerHTML = "";
  if (!folderNames.length) {
    folderBox.append(el("div", { class: "empty-note" }, "no subfolders here"));
  } else {
    const shown = folderNames.filter((name) => !folderFilter || name.toLowerCase().includes(folderFilter));
    if (!shown.length) folderBox.append(el("div", { class: "empty-note" }, "no matches"));
    for (const name of shown) {
      const inside = state.publications.filter((p) => p.folder === (prefix + name) || p.folder.startsWith(prefix + name + "/")).length;
      const row = el("div", { class: "mk-row clickable" },
        el("span", { class: "ic" }, "◫"),
        el("span", { class: "nm" }, name),
        el("span", { class: "mk-meta" }, `${inside} dashboard${inside === 1 ? "" : "s"}`));
      row.addEventListener("click", () => navigate(paths.portalFolder(prefix + name)));
      folderBox.append(row);
    }
  }

  const dashBox = $("#portal-dashboards-list");
  dashBox.innerHTML = "";
  if (!dashes.length) {
    dashBox.append(el("div", { class: "empty-note" }, "nothing published here yet — publish a dashboard from the studio"));
  } else {
    const shown = dashes.filter((p) => !dashFilter || p.name.toLowerCase().includes(dashFilter));
    if (!shown.length) dashBox.append(el("div", { class: "empty-note" }, "no matches"));
    for (const p of shown) {
      const row = el("div", { class: "mk-row clickable" },
        el("span", { class: "ic dash" }, "▦"),
        el("span", { class: "nm" }, p.name),
        el("span", { class: "mk-meta" }, `${p.tiles} tiles · published ${p.published_at.slice(0, 10)}`));
      row.addEventListener("click", () => navigate(paths.portalDashboard(p.dashboard_id)));
      dashBox.append(row);
    }
  }
}
