/* Entry point: initial load + all top-level DOM wiring. */
"use strict";

import {
  addParameter, refreshSaved, renderBuilderViz, renderChartSeg, renderFilters, saveVisual,
  scheduleRun,
} from "./builder.js";
import { PALETTE } from "./charts/common.js";
import { renderViz, vizMessage } from "./charts/index.js";
import {
  activeView, closeFocus, dashDimUnion, focus,
  paramConflictMessage, publishCurrent, refreshDashList, renderDashboard, renderDashFilters,
  renderFocusFilters, saveDash,
} from "./dashboard.js";
import { attachAccount } from "./admin.js";
import { initAuth } from "./auth.js";
import { attachBundleForm } from "./bundleform.js";
import { attachChat, probeChatAvailability } from "./chat.js";
import { attachEditor, deleteEditorItem, saveEditor, stopRunPolling } from "./editor.js";
// side-effect only: registers hooks.renderHome for the router
import "./home.js";
import { $, api } from "./lib.js";
import { initMeasureLab } from "./measurelab.js";
import { attachModelForm } from "./modelform.js";
import { loadModelling, openCreateChooser } from "./modelling.js";
// side-effect only: nothing here calls into the portal module directly
// anymore (the router dispatches to it via hooks.openPortalFolder), but the
// module still has to be imported somewhere for that registration to run
import "./portal.js";
import { initRouter, navigate, pathForMode, paths } from "./router.js";
import { refreshPubs, state } from "./state.js";
import { initTheme } from "./theme.js";

async function init() {
  try {
    initTheme();  // sync the chart palette to whatever theme the boot script already applied
    await initAuth();   // renders the login view first when no session exists
    const [health, models] = await Promise.all([api("/api/health"), api("/api/models")]);
    $("#conn").innerHTML = `<span class="dot">◉</span> S3 ${health.s3_endpoint.replace(/^https?:\/\//, "")} · POLARS ONLINE`;
    state.models = models;
    if (!models.length) return vizMessage($("#chart"), "no semantic models found — add a yaml file to models/", true);
    initMeasureLab();

    // ── builder ──
    $("#model-select").addEventListener("change", (e) => navigate(paths.studioModel(e.target.value)));
    $("#add-filter").addEventListener("click", () => {
      state.filters.push({ field: state.model.dimensions[0].name, op: "eq", value: "", values: [] });
      renderFilters();
    });
    $("#add-param").addEventListener("click", () => addParameter());
    $("#chart-seg").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      state.chartType = btn.dataset.t;
      state.showTable = false;
      $("#toggle-table").classList.remove("on");
      renderChartSeg();
      renderBuilderViz();
    });
    $("#sort-by").addEventListener("change", (e) => { state.sort.by = e.target.value; scheduleRun(); });
    $("#sort-dir").addEventListener("change", (e) => { state.sort.desc = e.target.value === "desc"; scheduleRun(); });
    $("#limit").addEventListener("change", (e) => { state.limit = Math.max(1, +e.target.value || 1000); scheduleRun(); });
    $("#save").addEventListener("click", () => saveVisual(false));
    $("#save-as").addEventListener("click", () => saveVisual(true));
    $("#toggle-table").addEventListener("click", () => {
      state.showTable = !state.showTable;
      $("#toggle-table").classList.toggle("on", state.showTable);
      renderBuilderViz();
    });

    attachAccount();  // tokens / password / user-management wiring
    attachChat();     // conversational analytics wiring
    probeChatAvailability(health);  // shows the CHAT nav entry only if the server has it configured

    // ── semantic editor + guided forms (opened from Modelling) ──
    attachEditor();   // input/keydown/completion/dataset-picker/revert/beforeunload
    attachModelForm();
    attachBundleForm();
    $("#mk-new-model").addEventListener("click", () => openCreateChooser());
    $("#mk-new-bundle").addEventListener("click", () => navigate(paths.modellingNewBundle()));
    $("#mk-new-pipeline").addEventListener("click", () => navigate(paths.modellingNewPipelineYaml()));
    $("#mk-lineage-graph").addEventListener("click", () => navigate(paths.modellingLineage()));
    $("#lineage-back").addEventListener("click", () => navigate(paths.modelling()));
    $("#editor-save").addEventListener("click", saveEditor);
    $("#editor-delete").addEventListener("click", deleteEditorItem);
    $("#editor-back").addEventListener("click", () => { stopRunPolling(); navigate(paths.modelling()); });

    // ── dashboards ──
    $("#new-dash").addEventListener("click", async () => {
      const created = await api("/api/dashboards", {
        method: "POST",
        body: { name: "untitled_dashboard", items: [], views: [{ name: "default", filters: [] }], active_view: 0 },
      });
      await refreshDashList();
      navigate(paths.studioDashboard(created.id));
    });
    $("#dash-back").addEventListener("click", () => {
      navigate(state.portal ? paths.portalFolder(state.portalFolder) : paths.studio());
    });
    $("#dash-publish").addEventListener("click", publishCurrent);
    $("#dash-name").addEventListener("change", saveDash);
    $("#dash-add").addEventListener("click", async () => {
      const id = +$("#dash-add-select").value;
      if (!id || !state.dash) return;
      const visuals = await api("/api/visuals");
      const candidate = visuals.find((v) => v.id === id);
      const conflict = candidate && paramConflictMessage(candidate);
      if (conflict) { alert("Can't add this visual: " + conflict); return; }
      state.dash.items.push({ visual_id: id, w: 1 });
      await saveDash();
      const av = state.dash.active_view;
      state.dash = await api(`/api/dashboards/${state.dash.id}`); // re-resolve visuals
      state.dash.active_view = av;
      renderDashboard();
    });
    $("#dash-refresh").addEventListener("click", async () => {
      state.dash = await api(`/api/dashboards/${state.dash.id}`);
      renderDashboard();
    });
    $("#dash-delete").addEventListener("click", async () => {
      await api(`/api/dashboards/${state.dash.id}`, { method: "DELETE" });
      await refreshDashList();
      navigate(paths.studio());
    });

    // dashboard views = named filter sets
    $("#dash-view-select").addEventListener("change", (e) => {
      state.dash.active_view = +e.target.value;
      state.crossFilter = null;  // ephemeral: cleared on view switch
      saveDash();
      renderDashboard();
    });
    $("#view-add").addEventListener("click", async () => {
      const name = prompt("New view name (starts with a copy of the current filters):", `view_${state.dash.views.length + 1}`);
      if (!name) return;
      state.dash.views.push({ name: name.trim(), filters: JSON.parse(JSON.stringify(activeView().filters)) });
      state.dash.active_view = state.dash.views.length - 1;
      await saveDash();
      renderDashboard();
    });
    $("#view-rename").addEventListener("click", async () => {
      const view = activeView();
      const name = prompt("View name:", view.name);
      if (!name) return;
      view.name = name.trim();
      await saveDash();
      renderDashboard();
    });
    $("#view-del").addEventListener("click", async () => {
      if (state.dash.views.length < 2) return;
      if (!confirm(`Delete view '${activeView().name}' and its saved filters?`)) return;
      state.dash.views.splice(state.dash.active_view, 1);
      state.dash.active_view = 0;
      await saveDash();
      renderDashboard();
    });
    $("#dash-filter-add").addEventListener("click", () => {
      const union = dashDimUnion();
      const first = union.keys().next().value;
      if (!first) return;
      activeView().filters.push({ field: first, op: "eq", value: "", values: [] });
      renderDashFilters();
    });

    // session-only grain override: deliberately not saved, so a refresh
    // falls back to whatever the saved view specifies
    $("#dash-grain").addEventListener("change", (e) => {
      state.dashGrain = e.target.value;
      e.target.classList.toggle("on", !!state.dashGrain);
      state.tiles.forEach((rec) => rec.visual && rec.run());
    });

    // focus mode
    $("#focus-close").addEventListener("click", closeFocus);
    $("#focus-modal").addEventListener("click", (e) => { if (e.target.id === "focus-modal") closeFocus(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#focus-modal").hidden) closeFocus(); });
    $("#focus-filter-add").addEventListener("click", () => {
      if (!focus.visual) return;
      const model = state.models.find((m) => m.name === focus.visual.model);
      focus.filters.push({ field: model.dimensions[0].name, op: "eq", value: "", values: [] });
      renderFocusFilters();
    });

    // mode nav: studio / modelling / portal / chat / account — the leave-
    // unsaved-edits guard (FR-021) now lives centrally in navigate()
    for (const btn of document.querySelectorAll("#mode-nav button")) {
      btn.addEventListener("click", () => navigate(pathForMode(btn.dataset.mode)));
    }
    $("#logo").addEventListener("click", () => navigate(paths.home()));
    $("#modelling-refresh").addEventListener("click", loadModelling);

    // re-render charts when the window or panel resizes
    let resizeTimer = null;
    const rerenderOnResize = () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (state.view === "builder") renderBuilderViz();
        else state.tileCtxs.forEach((ctx) => renderViz(ctx));
      }, 150);
    };
    window.addEventListener("resize", rerenderOnResize);
    new ResizeObserver(rerenderOnResize).observe($("#chart"));

    await initRouter();   // resolves the current URL (or "/" -> /studio) into a view
    refreshSaved();
    await refreshPubs();
    refreshDashList();
  } catch (err) {
    vizMessage($("#chart"), "BACKEND OFFLINE // " + err.message, true);
  }

  // dev hook: /?validate runs the palette validator in the console, against
  // whichever theme is currently active. validate_palette.js's browser entry
  // point reads its light/dark signal from body.dataset.mode specifically
  // (that's its own fixed contract, left unmodified) — note this is a
  // *different* attribute from the app's own body.dataset.mode (nav mode,
  // set in state.js); this debug-only branch briefly overwrites it, which is
  // harmless since ?validate is a one-off manual invocation, not a normal
  // user flow. The value itself comes from the real source of truth,
  // theme.js's documentElement.dataset.colorScheme.
  if (location.search.includes("validate")) {
    document.body.dataset.palette = PALETTE.join(",");
    document.body.dataset.mode = document.documentElement.dataset.colorScheme || "dark";
    document.body.dataset.surface = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    import("/static/validate_palette.js");
  }
}

init();
