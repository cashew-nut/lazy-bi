/* Client-side router: maps real URL paths to views/entities. Every in-app
   navigation goes through navigate(); popstate and the initial load both
   resolve through the same resolveRoute(). Handlers are read off state.js's
   `hooks` registry rather than imported directly from each view module —
   same convention already used there (hooks.renderDashList, hooks.loadModelling)
   to avoid import cycles, since those modules import navigate()/paths from
   here in the other direction. */
"use strict";

import { hooks, showView, state } from "./state.js";

const seg = (pathname) => pathname.split("/").filter(Boolean).map(decodeURIComponent);
const enc = (s) => encodeURIComponent(s);

export const paths = {
  home: () => "/",
  studio: () => "/studio",
  studioModel: (name) => `/studio/model/${enc(name)}`,
  studioVisual: (id) => `/studio/visual/${id}`,
  studioDashboard: (id) => `/studio/dashboard/${id}`,
  modelling: () => "/modelling",
  modellingModel: (name) => `/modelling/model/${enc(name)}`,
  modellingModelYaml: (name) => `/modelling/model/${enc(name)}/yaml`,
  modellingNewModel: () => "/modelling/model/new",
  modellingNewModelYaml: () => "/modelling/model/new/yaml",
  modellingBundle: (name) => `/modelling/bundle/${enc(name)}`,
  modellingBundleYaml: (name) => `/modelling/bundle/${enc(name)}/yaml`,
  modellingNewBundle: () => "/modelling/bundle/new",
  modellingNewBundleYaml: () => "/modelling/bundle/new/yaml",
  modellingPipelineYaml: (name) => `/modelling/pipeline/${enc(name)}/yaml`,
  modellingNewPipelineYaml: () => "/modelling/pipeline/new/yaml",
  modellingLineage: () => "/modelling/lineage",
  portal: () => "/portal",
  portalFolder: (path) => (path ? `/portal/folder/${path.split("/").map(enc).join("/")}` : "/portal"),
  portalDashboard: (id) => `/portal/dashboard/${id}`,
  chat: () => "/chat",
  chatConversation: (id) => `/chat/${id}`,
  account: () => "/account",
  notebook: (id) => `/notebook/${id}`,
};

const MODE_PATH = {
  home: paths.home, studio: paths.studio, modelling: paths.modelling, portal: paths.portal,
  chat: paths.chat, account: paths.account,
};
export const pathForMode = (mode) => (MODE_PATH[mode] || paths.studio)();

// mirrors the guard chain the mode-nav handler used to run inline —
// centralized here so every navigate() call gets it, not just mode-nav clicks
function guardLeave() {
  if (state.view === "editor" && hooks.confirmLeaveEditor && !hooks.confirmLeaveEditor()) return false;
  if (hooks.confirmLeaveModelForm && !hooks.confirmLeaveModelForm()) return false;
  if (hooks.confirmLeaveBundleForm && !hooks.confirmLeaveBundleForm()) return false;
  return true;
}

async function resolveStudio(rest) {
  if (rest.length === 0) return hooks.defaultStudio && hooks.defaultStudio();
  if (rest[0] === "model" && rest[1]) {
    showView("builder");
    return hooks.selectModel && hooks.selectModel(rest[1]);
  }
  if (rest[0] === "visual" && rest[1]) return hooks.openVisualById && hooks.openVisualById(+rest[1]);
  if (rest[0] === "dashboard" && rest[1]) return hooks.openDashboard && hooks.openDashboard(+rest[1], false);
  throw new Error(`unknown studio route: /${rest.join("/")}`);
}

async function resolveModelling(rest) {
  if (rest.length === 0) {
    showView("modelling");
    return hooks.loadModelling && hooks.loadModelling();
  }
  if (rest[0] === "lineage") {
    showView("lineage");
    return hooks.loadLineageGraph && hooks.loadLineageGraph();
  }
  const [kind, name, sub] = rest;
  const isNew = name === "new";
  if (kind === "model") {
    if (sub === "yaml") return hooks.openEditor && hooks.openEditor("model", isNew ? null : name);
    return hooks.openModelForm && hooks.openModelForm(isNew ? null : name);
  }
  if (kind === "bundle") {
    if (sub === "yaml") return hooks.openEditor && hooks.openEditor("bundle", isNew ? null : name);
    return hooks.openBundleForm && hooks.openBundleForm(isNew ? null : name);
  }
  if (kind === "pipeline") {
    return hooks.openEditor && hooks.openEditor("pipeline", isNew ? null : name);
  }
  throw new Error(`unknown modelling route: /${rest.join("/")}`);
}

async function resolvePortal(rest) {
  if (rest.length === 0) return hooks.openPortalFolder && hooks.openPortalFolder("");
  if (rest[0] === "folder") return hooks.openPortalFolder && hooks.openPortalFolder(rest.slice(1).join("/"));
  if (rest[0] === "dashboard" && rest[1]) return hooks.openDashboard && hooks.openDashboard(+rest[1], true);
  throw new Error(`unknown portal route: /${rest.join("/")}`);
}

async function resolveChat(rest) {
  showView("chat");
  if (rest.length === 0) {
    const openedId = hooks.loadChat && await hooks.loadChat();
    if (openedId) setPath(paths.chatConversation(openedId), { replace: true });
    return;
  }
  return hooks.openConversation && hooks.openConversation(+rest[0]);
}

const FALLBACK = { home: paths.home, studio: paths.studio, modelling: paths.modelling, portal: paths.portal, chat: paths.chat };

async function resolveRoute(pathname) {
  const [mod, ...rest] = seg(pathname === "/" ? "/home" : pathname);
  try {
    switch (mod) {
      case "home":
        showView("home");
        return hooks.renderHome && hooks.renderHome();
      case "studio": return await resolveStudio(rest);
      case "modelling": return await resolveModelling(rest);
      case "portal": return await resolvePortal(rest);
      case "chat": return await resolveChat(rest);
      case "account":
        showView("account");
        return hooks.loadAccount && hooks.loadAccount();
      case "notebook":
        if (!rest[0]) throw new Error("notebook route needs an id");
        return hooks.openNotebook && hooks.openNotebook(+rest[0]);
      default: throw new Error(`unknown route: ${pathname}`);
    }
  } catch (err) {
    console.warn("[router] " + pathname + " -> " + err.message);
    const fallback = (FALLBACK[mod] || paths.home)();
    history.replaceState(null, "", fallback);
    await resolveRoute(fallback);
  }
}

// the one function every in-app action uses to move around — pushes (or
// replaces) the URL and resolves it, unless a dirty editor/form blocks it.
// Returns the resolution's promise (or undefined if a guard blocked it) so
// callers that need the target actually open before continuing (e.g.
// sending a chat message right after creating its conversation) can await it.
export function navigate(path, { replace = false } = {}) {
  if (!guardLeave()) return undefined;
  setPath(path, { replace });
  return resolveRoute(path);
}

// syncs the address bar to `path` without resolving it — for callers that
// already performed the equivalent navigation themselves and just need the
// URL to catch up (e.g. handing generated-but-unsaved yaml text over to the
// editor, which resolveRoute has no way to reconstruct from a bare path)
export function setPath(path, { replace = false } = {}) {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
}

export async function initRouter() {
  window.addEventListener("popstate", () => resolveRoute(location.pathname));
  await resolveRoute(location.pathname);
}
