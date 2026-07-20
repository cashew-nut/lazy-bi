/* Notebooks: freeform HTML pages — tabs/collapsibles authored directly in
   the saved `html`, with live visuals and dashboards hydrated into place
   after render. Not a grid: the layout is whatever the author (by hand, or
   via the COMPOSER's LLM) wrote, using a few conventions this module knows
   how to bring to life:
     <details class="nb-collapsible">…</details>        — native collapsible
     <div class="nb-tabs"><div class="nb-tab-list">
       <button class="nb-tab-btn" data-tab="x">X</button>…</div>
       <div class="nb-tab-panel" data-tab="x">…</div>…</div>   — tabs
     <div class="nb-visual" data-visual-id="7"></div>          — one chart
       (add class "compact" for a short stat-height tile)
     <div class="nb-dashboard" data-dashboard-id="3"
          data-view="1"></div>                    — an embedded dashboard,
                                                      rendered at a given
                                                      saved view (defaults to
                                                      the dashboard's own
                                                      active_view)
     <aside class="nb-explainer" data-title="…"
            data-tone="info|method|warn">…</aside> — explainer window: a
                                                      callout that decodes a
                                                      chart or flags a caveat
     <div class="nb-split"><div class="nb-side">…</div>
       <div class="nb-side">…</div></div>         — claim | proof diptych row
   The same vocabulary is enforced server-side for LLM-composed pages
   (app/composer.py's sanitize_notebook_html). */
"use strict";

import { canAuthor } from "./auth.js";
import { renderViz, vizMessage } from "./charts/index.js";
import { isChatEnabled } from "./chat.js";
import { toApiFilter } from "./filters.js";
import { $, api, el } from "./lib.js";
import { navigate, paths } from "./router.js";
import { hooks, modelByName, showView, state } from "./state.js";

export async function refreshNotebookList() {
  state.notebooks = await api("/api/notebooks");
  renderNotebookList();
}

export function renderNotebookList() {
  const box = $("#home-notebook-list");
  if (!box) return;
  box.innerHTML = "";
  if (!state.notebooks.length) {
    box.append(el("div", { class: "empty-note" }, "no notebooks yet"));
  }
  for (const n of state.notebooks) {
    const item = el("div", { class: "mk-card clickable" },
      el("div", { class: "mk-top" }, el("span", { class: "nm" }, n.name)),
      el("div", { class: "mk-sub" }, `updated ${n.updated_at.slice(0, 10)}`));
    item.addEventListener("click", () => navigate(paths.notebook(n.id)));
    box.append(item);
  }
  // the composer needs both authoring rights (it saves notebooks) and a
  // configured LLM (health.llm_enabled, same probe the CHAT nav uses)
  if (canAuthor() && isChatEnabled()) {
    const compose = el("button", { class: "ghost" }, "+ compose a page");
    compose.addEventListener("click", () => navigate(paths.composerNew()));
    box.append(compose);
  }
}
hooks.renderNotebookList = renderNotebookList;
hooks.refreshNotebookList = refreshNotebookList;

export async function openNotebook(id) {
  showView("notebook");
  const content = $("#notebook-content");
  content.innerHTML = "";
  $("#notebook-name").textContent = "";
  const composeBtn = $("#notebook-compose");
  composeBtn.hidden = true;
  let nb;
  try {
    nb = await api(`/api/notebooks/${id}`);
  } catch (err) {
    return vizMessage(content, "notebook not found — " + err.message, true);
  }
  $("#notebook-name").textContent = nb.name;
  if (canAuthor() && isChatEnabled()) {
    composeBtn.hidden = false;
    composeBtn.onclick = () => navigate(paths.composerEdit(id));
  }
  content.innerHTML = stripScripts(nb.html);
  await hydrate(content);
}
hooks.openNotebook = openNotebook;

// author-authored (role-gated) content, same trust boundary as the model
// yaml/measure DSL authors can already write server-side — this strip is
// belt-and-braces against an accidental paste, not a security boundary.
// Exported for the composer's live preview, which renders in-flight LLM
// output that hasn't reached the server-side sanitizer yet.
export function stripScripts(html) {
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  tmp.querySelectorAll("script").forEach((s) => s.remove());
  return tmp.innerHTML;
}

// exported so the composer's preview pane renders drafts through the exact
// pipeline the saved page will use — what you preview is what you publish
export async function hydrate(root) {
  wireTabs(root);
  wireExplainers(root);
  const visuals = await api("/api/visuals");
  const jobs = [
    ...[...root.querySelectorAll(".nb-visual[data-visual-id]")].map((elm) => hydrateVisual(elm, visuals)),
    ...[...root.querySelectorAll(".nb-dashboard[data-dashboard-id]")].map((elm) => hydrateDashboard(elm)),
  ];
  await Promise.all(jobs);
}

// an explainer's data-title becomes a rendered header row; idempotent so
// re-hydrating a preview doesn't stack headers
function wireExplainers(root) {
  root.querySelectorAll(".nb-explainer[data-title]").forEach((box) => {
    if (box.querySelector(":scope > .nb-explainer-title")) return;
    box.prepend(el("div", { class: "nb-explainer-title" }, box.dataset.title));
  });
}

function wireTabs(root) {
  root.querySelectorAll(".nb-tabs").forEach((tabs) => {
    const btns = [...tabs.querySelectorAll(":scope > .nb-tab-list > .nb-tab-btn")];
    const panels = [...tabs.querySelectorAll(":scope > .nb-tab-panel")];
    const activate = (name) => {
      btns.forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
      panels.forEach((p) => { p.hidden = p.dataset.tab !== name; });
    };
    btns.forEach((b) => b.addEventListener("click", () => activate(b.dataset.tab)));
    activate((btns.find((b) => b.classList.contains("on")) || btns[0])?.dataset.tab);
  });
}

function dimsOf(query) {
  return (query.dimensions || []).map((d) => (typeof d === "string" ? { name: d } : d));
}

async function runInto(visual, body, legend, extraFilters = []) {
  const model = modelByName(visual.model);
  if (!model) return vizMessage(body, `model '${visual.model}' is gone`, true);
  const q = visual.spec.query || {};
  const ctx = {
    model, dims: dimsOf(q),
    chartType: visual.spec.chartType || "auto",
    xAxisTitle: visual.spec.xAxisTitle || "",
    yAxisTitle: visual.spec.yAxisTitle || "",
    yScale: visual.spec.yScale || "linear",
    container: body, legendBox: legend,
  };
  ctx.rerender = () => renderViz(ctx);
  vizMessage(body, "querying…");
  try {
    ctx.result = await api("/api/query", { method: "POST", body: { ...q, filters: [...(q.filters || []), ...extraFilters] } });
    renderViz(ctx);
  } catch (err) {
    vizMessage(body, "QUERY ERROR // " + err.message, true);
  }
}

async function hydrateVisual(elm, visuals) {
  elm.classList.add("tile");
  const visual = visuals.find((v) => v.id === +elm.dataset.visualId);
  const legend = el("div", { class: "legend-box" });
  const body = el("div", { class: "chart-box" });
  if (!visual) {
    elm.append(legend, body);
    return vizMessage(body, "visual not found", true);
  }
  elm.append(
    el("div", { class: "tile-head" }, el("span", { class: "nm" }, visual.name), el("span", { class: "tag" }, visual.model)),
    legend, body,
  );
  return runInto(visual, body, legend);
}

async function hydrateDashboard(elm) {
  let dash;
  try {
    dash = await api(`/api/dashboards/${+elm.dataset.dashboardId}`);
  } catch (err) {
    return vizMessage(elm, "dashboard not found — " + err.message, true);
  }
  const viewIdx = elm.dataset.view != null && dash.views[+elm.dataset.view] ? +elm.dataset.view : dash.active_view;
  const view = dash.views[viewIdx];
  elm.append(el("div", { class: "nb-dash-head" },
    el("span", { class: "nm" }, dash.name), el("span", { class: "tag" }, view.name)));
  const grid = el("div", { class: "nb-dash-grid" });
  elm.append(grid);

  const jobs = dash.items.map((item) => {
    const visual = dash.visuals[String(item.visual_id)];
    const tile = el("div", { class: "tile" + (item.w === 2 ? " w2" : "") });
    grid.append(tile);
    if (!visual) return vizMessage(tile, "visual deleted", true);
    const legend = el("div", { class: "legend-box" });
    const body = el("div", { class: "chart-box" });
    tile.append(el("div", { class: "tile-head" }, el("span", { class: "nm" }, visual.name)), legend, body);
    const model = modelByName(visual.model);
    const pushdown = model
      ? (view.filters || []).filter((f) => f.field && model.dimensions.some((d) => d.name === f.field)).map(toApiFilter)
      : [];
    return runInto(visual, body, legend, pushdown);
  });
  await Promise.all(jobs);
}
