/* Home: the operator console shown at "/" — a mode picker (studio /
   modelling / portal / chat), admin shortcuts, and a glance at the
   registered semantic models. Pure presentation; router.js drives it via
   hooks.renderHome once showView("home") has unhidden #home-view. */
"use strict";

import { isAdmin, user } from "./auth.js";
import { isChatEnabled } from "./chat.js";
import { $, el } from "./lib.js";
import { navigate, paths } from "./router.js";
import { hooks, state } from "./state.js";

const MODES = [
  { mode: "studio", icon: "▣", label: "STUDIO", desc: "Build queries, chart data, save visuals", path: paths.studio },
  { mode: "modelling", icon: "◈", label: "MODELLING", desc: "Semantic layer: models, dimensions, measures", path: paths.modelling },
  { mode: "portal", icon: "▦", label: "PORTAL", desc: "Browse published dashboards, read-only", path: paths.portal },
  { mode: "chat", icon: "✦", label: "CHAT", desc: "Ask questions in plain language", path: paths.chat },
];

export function renderHome() {
  const u = user();
  const first = (u?.display_name || "").trim().split(/\s+/)[0] || "operator";
  $("#home-greeting").textContent = `Where to, ${first}?`;

  const cards = $("#home-cards");
  cards.innerHTML = "";
  for (const m of MODES) {
    if (m.mode === "chat" && !isChatEnabled()) continue;
    const card = el("button", { class: `home-card ${m.mode}`, type: "button" },
      el("div", { class: "ic" }, m.icon),
      el("div", { class: "nm" }, m.label),
      el("div", { class: "desc" }, m.desc));
    card.addEventListener("click", () => navigate(m.path()));
    cards.append(card);
  }

  const adminPanel = $("#home-admin");
  adminPanel.hidden = !isAdmin();
  if (isAdmin()) {
    const adminCards = $("#home-admin-cards");
    adminCards.innerHTML = "";
    const manageUsers = el("button", { class: "home-admin-card", type: "button" },
      el("div", { class: "ic" }, "⚑"),
      el("div", {},
        el("div", { class: "nm" }, "MANAGE USERS"),
        el("div", { class: "desc" }, "Roles, tokens & account access")));
    manageUsers.addEventListener("click", () => navigate(paths.account()));
    const modelRegistry = el("button", { class: "home-admin-card", type: "button" },
      el("div", { class: "ic" }, "⌁"),
      el("div", {},
        el("div", { class: "nm" }, "MODEL REGISTRY"),
        el("div", { class: "desc" }, "Author fact & common dimension models")));
    modelRegistry.addEventListener("click", () => navigate(paths.modelling()));
    adminCards.append(manageUsers, modelRegistry);
  }

  const list = $("#home-models-list");
  list.innerHTML = "";
  $("#home-models-count").textContent = state.models.length
    ? `${state.models.length} model${state.models.length === 1 ? "" : "s"}`
    : "";
  for (const m of state.models) {
    const chip = el("div", { class: "home-model-chip" },
      el("span", { class: "nm" }, m.label || m.name),
      el("span", { class: "fmt" }, m.format),
      el("span", { class: "meta" }, `${m.dimensions.length} dim · ${m.measures.length} msr`));
    chip.addEventListener("click", () => navigate(paths.studioModel(m.name)));
    list.append(chip);
  }
  if (!state.models.length) {
    list.append(el("div", { class: "empty-note" }, "no semantic models found"));
  }
}
hooks.renderHome = renderHome;
