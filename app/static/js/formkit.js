/* Shared plumbing for the guided authoring forms (modelform.js fact models,
   bundleform.js common models): source-schema cache, the bucket dataset
   picker, relationship pair rows, and small field builders. No form state
   lives here — each form owns its own spec. */
"use strict";

import { api, el, fmtBytes } from "./lib.js";

export const NAME_RE = /^[a-z_][a-z0-9_]*$/;

// ── source schemas (columns of an arbitrary path) ──

const schemaCache = {};   // "format|path" -> [{name,dtype}] | null (unreachable)

export async function sourceSchema(path, format) {
  const key = `${format}|${path}`;
  if (key in schemaCache) return schemaCache[key];
  try {
    const res = await api(`/api/datasets/schema?path=${encodeURIComponent(path)}&format=${encodeURIComponent(format)}`);
    schemaCache[key] = res.columns;
  } catch {
    schemaCache[key] = null;   // unreachable — pairs fall back to text inputs
  }
  return schemaCache[key];
}

export const colsOf = (src) => (src && schemaCache[`${src.format}|${src.path}`]) || null;

// ── bucket datasets (fetched once per session, shared by both forms) ──

let datasets = null;   // /api/datasets payload | null (unreachable)

export async function loadDatasets() {
  if (!datasets) datasets = await api("/api/datasets").catch(() => null);
  return datasets;
}

// dataset cards grid: click a card for the grouped glob, or drill into a
// chip to use one exact object (FR-006)
export function datasetCards(onpick, current) {
  const box = el("div", { class: "mf-ds-grid" });
  if (!datasets) { box.append(note("bucket not reachable — enter a path manually below")); return box; }
  for (const ds of datasets.datasets) {
    const on = current && current.path === ds.path;
    const card = el("div", { class: "mk-card clickable" + (on ? " sel" : "") },
      el("div", { class: "mk-top" }, el("span", { class: "nm" }, ds.key || "(root)"), el("span", { class: "fmt" }, ds.format)),
      el("div", { class: "path" }, ds.path),
      el("div", { class: "mk-sub" }, `${ds.object_count} obj · ${fmtBytes(ds.bytes)}`
        + (ds.models.length ? ` · read by ${[...new Set(ds.models.map((m) => m.name))].join(", ")}` : " · unmapped")
        + (ds.format_ambiguous ? " · ⚠ mixed types" : "")));
    card.addEventListener("click", () => onpick({ key: ds.key, path: ds.path, format: ds.format }));
    if (ds.format !== "delta" && ds.objects.length > 1) {
      const drill = el("div", { class: "import-datasets" });
      for (const o of ds.objects) {
        const chip = el("div", { class: "col-chip", title: `use just ${o.key}` },
          el("span", {}, o.key.split("/").pop()), el("span", { class: "dt" }, o.format));
        chip.addEventListener("click", (e) => {
          e.stopPropagation();
          onpick({ key: o.key, path: `s3://${datasets.bucket}/${o.key}`, format: o.format });
        });
        drill.append(chip);
      }
      card.append(drill);
    }
    box.append(card);
  }
  return box;
}

// ── field + relationship builders ──

export const note = (text) => el("div", { class: "empty-note mf-note" }, text);

export function textField(label, value, oninput, ph = "") {
  const input = el("input", { value, placeholder: ph, spellcheck: "false" });
  input.addEventListener("input", () => oninput(input.value));
  return el("div", { class: "mf-field" }, el("div", { class: "field-label" }, label), input);
}

/* A LEFT↔RIGHT relationship pair row; either side degrades to a text input
   when its schema is unreachable. The two names do not have to match. */
export function pairRow(pair, leftCols, rightCols, { leftPh, rightPh, onchange, onremove, oninput = () => {} }) {
  const side = (val, cols, set, ph) => {
    if (!cols || !cols.length) {
      const input = el("input", { value: val, placeholder: ph, spellcheck: "false" });
      input.addEventListener("input", () => { set(input.value); oninput(); });
      return input;
    }
    const sel = el("select", {}, el("option", { value: "" }, `— ${ph} —`));
    if (val && !cols.some((c) => c.name === val)) sel.append(el("option", { value: val }, val));
    for (const c of cols) sel.append(el("option", { value: c.name }, `${c.name} · ${c.dtype}`));
    sel.value = val;
    sel.addEventListener("change", () => { set(sel.value); onchange(); });
    return sel;
  };
  const rm = el("button", { class: "rm", title: "remove pair" }, "✕");
  rm.addEventListener("click", onremove);
  return el("div", { class: "mf-pair" },
    side(pair.left, leftCols, (v) => { pair.left = v; }, leftPh),
    el("span", { class: "mf-link" }, "⇄"),
    side(pair.right, rightCols, (v) => { pair.right = v; }, rightPh),
    rm);
}

/* manual path entry row: input + format select + apply button */
export function manualPathRow(current, onapply) {
  const path = el("input", { value: current?.path || "", placeholder: "s3://bucket/prefix/*.parquet", spellcheck: "false" });
  const fmt = el("select", {}, ...["parquet", "csv", "delta"].map((f) => el("option", { value: f }, f)));
  fmt.value = current?.format || "parquet";
  const load = el("button", { class: "btn plain" }, "USE PATH");
  load.addEventListener("click", () => {
    if (path.value.trim()) onapply({ path: path.value.trim(), format: fmt.value });
  });
  return el("div", { class: "mf-manual" }, el("div", { class: "field-label" }, "OR TYPE A PATH"),
    el("div", { class: "mf-manual-row" }, path, fmt, load));
}

/* default label for a column ticked as a dimension */
export const titleCase = (name) => name.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());

/* default spec dict for a column becoming a dimension */
export const dimFromColumn = (c) => ({
  name: c.name, column: c.name, label: titleCase(c.name),
  type: /date|time/i.test(c.dtype) ? "time" : "categorical",
  description: "", spine: null, geo: null, synonyms: [],
});

// ── section rail (shared by both guided forms) ──
// sections: [{id, label}] · status(id) -> "err" | "done" | "" · badges show
// at a glance which sections still need attention — navigation is never gated
export function sectionRail(rail, sections, currentId, status, onnav) {
  rail.innerHTML = "";
  for (const s of sections) {
    const st = status(s.id);
    const btn = el("button", { class: "mf-step" + (s.id === currentId ? " on" : "") + (st === "done" ? " done" : "") },
      el("span", { class: "num" + (st === "err" ? " bad" : "") }, st === "err" ? "!" : st === "done" ? "✓" : "·"),
      s.label);
    btn.addEventListener("click", () => onnav(s.id));
    rail.append(btn);
  }
}

// ── synonyms chip editor ──
// edits `list` in place; Enter / comma / blur commits the typed synonym
export function synonymsInput(list, onchange, ph = "+ synonym") {
  const box = el("div", { class: "syn-box" });
  const draw = () => {
    box.innerHTML = "";
    list.forEach((s, idx) => {
      const rm = el("b", { title: "remove synonym" }, "✕");
      rm.addEventListener("click", () => { list.splice(idx, 1); onchange(); draw(); });
      box.append(el("span", { class: "syn-chip" }, s, rm));
    });
    const input = el("input", { class: "syn-input", placeholder: ph, spellcheck: "false" });
    const commit = () => {
      const parts = input.value.split(",").map((s) => s.trim()).filter(Boolean);
      const added = parts.filter((p) => !list.includes(p));
      if (!added.length) { input.value = ""; return; }
      list.push(...added);
      onchange();
      draw();
      box.querySelector(".syn-input").focus();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); }
      if (e.key === "Backspace" && !input.value && list.length) { list.pop(); onchange(); draw(); box.querySelector(".syn-input").focus(); }
    });
    input.addEventListener("blur", commit);
    box.append(input);
  };
  draw();
  return box;
}

// ── column import panel ──
// The "bring this dataset's columns in" step: all columns pre-selected
// (import everything in one click) or narrowed down to just the relevant
// ones. `taken` names render as already-in chips instead of choices.
export function columnImportPanel(cols, taken, { verb = "dimension", onapply, ondismiss }) {
  const takenSet = new Set(taken);
  const open = cols.filter((c) => !takenSet.has(c.name));
  const picked = new Set(open.map((c) => c.name));   // default: import all
  const panel = el("div", { class: "mf-import-cols" });

  const draw = () => {
    panel.innerHTML = "";
    const head = el("div", { class: "mf-import-head" },
      el("span", { class: "field-label" }, "IMPORT COLUMNS"),
      el("span", { class: "mf-colcount" }, `${picked.size} of ${open.length} selected`));
    const all = el("button", { class: "mini-btn" }, "all");
    all.addEventListener("click", () => { open.forEach((c) => picked.add(c.name)); draw(); });
    const none = el("button", { class: "mini-btn" }, "none");
    none.addEventListener("click", () => { picked.clear(); draw(); });
    head.append(all, none);
    panel.append(head);

    const grid = el("div", { class: "mf-import-grid" });
    for (const c of cols) {
      if (takenSet.has(c.name)) {
        grid.append(el("span", { class: "chip taken", title: `already a ${verb}` },
          el("span", { class: "tick" }, "◈"), el("span", { class: "lbl" }, c.name), el("span", { class: "hint" }, c.dtype)));
        continue;
      }
      const on = picked.has(c.name);
      const chip = el("button", { class: "chip" + (on ? " on" : "") },
        el("span", { class: "tick" }, on ? "✓" : ""), el("span", { class: "lbl" }, c.name), el("span", { class: "hint" }, c.dtype));
      chip.addEventListener("click", () => { on ? picked.delete(c.name) : picked.add(c.name); draw(); });
      grid.append(chip);
    }
    panel.append(grid);

    const apply = el("button", { class: "btn" },
      picked.size === open.length && open.length
        ? `IMPORT ALL ${open.length} AS ${verb.toUpperCase()}S`
        : `IMPORT ${picked.size} AS ${verb.toUpperCase()}${picked.size === 1 ? "" : "S"}`);
    apply.disabled = !picked.size;
    apply.addEventListener("click", () => onapply(open.filter((c) => picked.has(c.name))));
    const skip = el("button", { class: "btn plain" }, "SKIP — I'LL PICK LATER");
    skip.addEventListener("click", ondismiss);
    panel.append(el("div", { class: "mf-import-actions" }, apply, skip));
  };
  draw();
  return panel;
}

// ── auto-growing textarea (single-line look, grows with content) ──
export function autoGrow(ta) {
  const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight + 2, 220) + "px"; };
  ta.addEventListener("input", fit);
  // fit once mounted (scrollHeight is 0 while detached)
  requestAnimationFrame(fit);
  return ta;
}
