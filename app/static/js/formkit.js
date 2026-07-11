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
