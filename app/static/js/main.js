/* Entry point: initial load + all top-level DOM wiring. */
"use strict";

import {
  closeAllPopovers, copyQueryJson, deleteCurrentVisual, duplicateVisual, openDashPickMenu,
  refreshSaved, renderBuilderViz, renderChartSeg, renderYScaleSeg, resetDisplay,
  saveVisual, scheduleRun, setFieldFilter, setSavedFilter, syncAutoBtn, syncDisplayBtn,
  toggleAutoMenu, toggleDisplayPop, toggleFirstFieldMatch, toggleFooterRow, toggleHeadMenu,
  toggleModelPop,
} from "./builder.js";
import { PALETTE } from "./charts/common.js";
import { renderViz, vizMessage } from "./charts/index.js";
import {
  activeView, closeFocus, dashDimUnion, focus,
  paramConflictMessage, publishCurrent, refreshDashList, renderDashboard, renderDashFilters,
  renderFocusFilters, saveDash, setDashListFilter,
} from "./dashboard.js";
import { attachAccount } from "./admin.js";
import { initAuth } from "./auth.js";
import { attachBundleForm } from "./bundleform.js";
import { attachChat, probeChatAvailability } from "./chat.js";
// registers hooks.openComposer for the router + wires the compose form
import { attachComposer } from "./composer.js";
import { attachEditor, deleteEditorItem, saveEditor, stopRunPolling } from "./editor.js";
// side-effect only: registers hooks.loadLineageGraph for the router
import "./lineagegraph.js";
// side-effect only: registers hooks.renderHome for the router
import "./home.js";
import { $, api } from "./lib.js";
// side-effect only: registers hooks.openNotebook/renderNotebookList for the router/home
import "./notebook.js";
import { initMeasureLab } from "./measurelab.js";
import { attachModelForm } from "./modelform.js";
import {
  loadModelling, openCreateChooser, openLayersModal,
  setBundlesFilter, setDatasetFilter, setModelsFilter, setPipelinesFilter,
} from "./modelling.js";
import { attachPanelChat } from "./panelchat.js";
// the router dispatches into the portal module via hooks.openPortalFolder;
// the filter setters are the only exports called directly from here
import { setPortalDashFilter, setPortalFolderFilter } from "./portal.js";
import { initRouter, navigate, pathForMode, paths } from "./router.js";
import { attachSandbox } from "./sandbox.js";
import { attachSandboxAgent, probeSandboxAgent } from "./sandboxagent.js";
import { refreshPubs, state } from "./state.js";
import { initTheme } from "./theme.js";

async function init() {
  try {
    initTheme();  // sync the chart palette to whatever theme the boot script already applied
    await initAuth();   // renders the login view first when no session exists
    const [health, models] = await Promise.all([api("/api/health"), api("/api/models")]);
    $("#conn").innerHTML = `<span class="dot">◉</span> S3 ${health.s3_endpoint.replace(/^https?:\/\//, "")} · DUCKDB ONLINE`;
    state.models = models;
    if (!models.length) return vizMessage($("#chart"), "no semantic models found — add a yaml file to models/", true);
    initMeasureLab();

    // ── builder: rail (model switch, field search, footer rows) ──
    $("#model-switch").addEventListener("click", () => toggleModelPop());
    $("#model-switch").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleModelPop(); }
    });
    $("#field-search").addEventListener("input", (e) => setFieldFilter(e.target.value));
    $("#field-search").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); toggleFirstFieldMatch(); }
      else if (e.key === "Escape") { e.target.value = ""; setFieldFilter(""); e.target.blur(); }
    });
    $("#saved-filter").addEventListener("input", (e) => setSavedFilter(e.target.value));
    $("#dash-list-filter").addEventListener("input", (e) => setDashListFilter(e.target.value));
    $("#footer-row-saved").addEventListener("click", () => toggleFooterRow("saved"));
    $("#footer-row-dash").addEventListener("click", () => toggleFooterRow("dashboards"));

    // ── builder: chart-head (save badge, table toggle, overflow menu, save) ──
    $("#head-menu-btn").addEventListener("click", () => toggleHeadMenu());
    $("#hm-save-as").addEventListener("click", () => { closeAllPopovers(); saveVisual(true); });
    $("#hm-add-dash").addEventListener("click", () => openDashPickMenu());
    $("#hm-duplicate").addEventListener("click", () => { closeAllPopovers(); duplicateVisual(); });
    $("#hm-copy-json").addEventListener("click", () => { closeAllPopovers(); copyQueryJson(); });
    $("#hm-delete").addEventListener("click", () => { closeAllPopovers(); deleteCurrentVisual(); });
    $("#dash-pick-back").addEventListener("click", () => { $("#dash-pick-menu").hidden = true; $("#head-menu").hidden = false; });
    $("#save").addEventListener("click", () => saveVisual(false));
    $("#toggle-table").addEventListener("click", () => {
      state.showTable = !state.showTable;
      $("#toggle-table").classList.toggle("on", state.showTable);
      renderBuilderViz();
    });

    // ── builder: chart toolbar (chart-type menu, display popover) ──
    $("#auto-btn").addEventListener("click", () => toggleAutoMenu());
    $("#chart-seg").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      state.chartType = btn.dataset.t;
      state.showTable = false;
      $("#toggle-table").classList.remove("on");
      closeAllPopovers();
      renderChartSeg();
      syncAutoBtn();
      renderBuilderViz();
    });
    $("#display-btn").addEventListener("click", () => toggleDisplayPop());
    $("#display-reset").addEventListener("click", () => resetDisplay());
    $("#sort-by").addEventListener("change", (e) => { state.sort.by = e.target.value; syncDisplayBtn(); scheduleRun(); });
    $("#sort-dir").addEventListener("change", (e) => { state.sort.desc = e.target.value === "desc"; syncDisplayBtn(); scheduleRun(); });
    $("#limit").addEventListener("change", (e) => { state.limit = Math.max(1, +e.target.value || 1000); syncDisplayBtn(); scheduleRun(); });
    $("#axis-title-x").addEventListener("change", (e) => { state.xAxisTitle = e.target.value.trim(); syncDisplayBtn(); renderBuilderViz(); });
    $("#axis-title-y").addEventListener("change", (e) => { state.yAxisTitle = e.target.value.trim(); syncDisplayBtn(); renderBuilderViz(); });
    $("#yscale-seg").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      state.yScale = btn.dataset.s;
      renderYScaleSeg();
      syncDisplayBtn();
      renderBuilderViz();
    });

    // ⌘K focuses the field search; Esc closes whichever builder popover is
    // open; an outside click closes it too (the popovers themselves handle
    // their own toggle-open, so this is purely the "click away" / Esc path)
    document.addEventListener("keydown", (e) => {
      if (state.view !== "builder") return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        $("#field-search").focus();
        return;
      }
      if (e.key === "Escape" && document.activeElement !== $("#field-search")) closeAllPopovers();
    });
    document.addEventListener("click", (e) => {
      if (state.view !== "builder") return;
      if (e.target.closest(".menu-wrap, .model-switch, .model-pop, .display-pop, #display-btn, .qs-picker, .qs-add, .qs-pill")) return;
      closeAllPopovers();
    });

    attachAccount();  // tokens / password / user-management wiring
    attachChat();     // conversational analytics wiring
    probeChatAvailability(health);  // shows the CHAT nav entry only if the server has it configured
    attachComposer(); // notebook composer wiring (same llm_enabled gate as chat)

    // ── semantic editor + guided forms (opened from Modelling) ──
    attachEditor();   // input/keydown/completion/dataset-picker/revert/beforeunload
    attachModelForm();
    attachPanelChat();  // ephemeral right-hand chat panel, scoped to the model being edited
    attachBundleForm();
    attachSandbox();  // sandbox notebooks: cells, run, convert-to-pipeline, bucket file browser
    attachSandboxAgent();          // the notebook's coding agent panel
    probeSandboxAgent(health);     // hides the agent entirely when the server has no LLM key
    $("#mk-new-model").addEventListener("click", () => openCreateChooser());
    $("#mk-new-bundle").addEventListener("click", () => navigate(paths.modellingNewBundle()));
    $("#mk-new-pipeline").addEventListener("click", () => navigate(paths.modellingNewPipelineYaml()));
    $("#mk-lineage-graph").addEventListener("click", () => navigate(paths.modellingLineage()));
    $("#mk-layers").addEventListener("click", () => openLayersModal());
    $("#lineage-back").addEventListener("click", () => navigate(paths.modelling()));
    $("#modelling-ds-filter").addEventListener("input", (e) => setDatasetFilter(e.target.value));
    $("#mk-models-filter").addEventListener("input", (e) => setModelsFilter(e.target.value));
    $("#mk-bundles-filter").addEventListener("input", (e) => setBundlesFilter(e.target.value));
    $("#mk-pipelines-filter").addEventListener("input", (e) => setPipelinesFilter(e.target.value));
    $("#portal-folders-filter").addEventListener("input", (e) => setPortalFolderFilter(e.target.value));
    $("#portal-dashboards-filter").addEventListener("input", (e) => setPortalDashFilter(e.target.value));
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
    $("#notebook-back").addEventListener("click", () => navigate(paths.home()));
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

    // instant mode: persisted per dashboard, and a full reload either way —
    // turning it on has to fetch extracts, turning it off has to drop them
    $("#dash-instant").addEventListener("change", async (e) => {
      if (!state.dash) return;
      state.dash.instant = e.target.checked;
      await saveDash();
      renderDashboard();
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
