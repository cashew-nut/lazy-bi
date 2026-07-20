/* Home: the operator console shown at "/" — a numbered destination index
   (studio / modelling / portal / chat) and admin shortcuts. Pure
   presentation; router.js drives it via hooks.renderHome once
   showView("home") has unhidden #home-view. */
"use strict";

import { isAdmin, user } from "./auth.js";
import { isChatEnabled } from "./chat.js";
import { $, el } from "./lib.js";
import { navigate, paths } from "./router.js";
import { hooks } from "./state.js";

const MODES = [
  { mode: "studio", icon: "▣", label: "STUDIO", desc: "Build queries, chart data, save visuals", path: paths.studio },
  { mode: "modelling", icon: "◈", label: "MODELLING", desc: "Semantic layer: models, dimensions, measures", path: paths.modelling },
  { mode: "portal", icon: "▦", label: "PORTAL", desc: "Browse published dashboards, read-only", path: paths.portal },
  { mode: "chat", icon: "✦", label: "CHAT", desc: "Ask questions in plain language", path: paths.chat },
];

const idx = (n) => String(n).padStart(2, "0");

export function renderHome() {
  const u = user();
  const first = (u?.display_name || "").trim().split(/\s+/)[0] || "operator";
  $("#home-greeting").textContent = `where to, ${first.toLowerCase()}?`;

  const rows = $("#home-cards");
  rows.innerHTML = "";
  const visible = MODES.filter((m) => m.mode !== "chat" || isChatEnabled());
  visible.forEach((m, i) => {
    const row = el("button", { class: `home-row ${m.mode}`, type: "button" },
      el("span", { class: "idx" }, idx(i)),
      el("span", { class: "ic" }, m.icon),
      el("span", { class: "body" },
        el("span", { class: "nm" }, m.label),
        el("span", { class: "desc" }, m.desc)));
    row.addEventListener("click", () => navigate(m.path()));
    rows.append(row);
  });

  const adminPanel = $("#home-admin");
  adminPanel.hidden = !isAdmin();
  if (isAdmin()) {
    const adminRow = $("#home-admin-cards");
    adminRow.innerHTML = "";
    const manageUsers = el("button", { class: "home-admin-chip", type: "button" },
      el("span", { class: "ic" }, "⚑"), "manage users");
    manageUsers.addEventListener("click", () => navigate(paths.account()));
    const modelRegistry = el("button", { class: "home-admin-chip", type: "button" },
      el("span", { class: "ic" }, "⌁"), "model registry");
    modelRegistry.addEventListener("click", () => navigate(paths.modelling()));
    adminRow.append(manageUsers, modelRegistry);
  }

  hooks.refreshNotebookList && hooks.refreshNotebookList();
}
hooks.renderHome = renderHome;
