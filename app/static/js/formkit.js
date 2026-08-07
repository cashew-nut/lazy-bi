/* Shared plumbing for the guided authoring forms (modelform.js fact models,
   bundleform.js common models): source-schema cache, the bucket dataset
   picker, relationship pair rows, and small field builders. No form state
   lives here — each form owns its own spec. */
"use strict";

import { api, apiUpload, el, fmtBytes } from "./lib.js";

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

/* The bucket's datasets as a ledger — one hairline-ruled row per dataset,
   not a wall of identical bordered tiles. A dataset is an entry in an index:
   what matters is the key, then whether anything already reads it, then the
   path. Tiles gave all three the same weight and boxed each one, which is
   why eight datasets read as eight cards instead of one list.

   Click a row to take the grouped glob. A multi-object dataset also gets a
   drill toggle that reveals its objects as indented sub-rows, so "use just
   recruitment.parquet" is one click without a chip cloud inside a card.

   Shared with the model editor's side panel (editor.js) so both pickers are
   the same object; `bucket` is only needed to build single-object paths. */
export function datasetLedger(list, bucket, onpick, current) {
  const box = el("div", { class: "ds-ledger" });
  for (const ds of list) {
    const on = current && current.path === ds.path;
    const readers = [...new Set(ds.models.map((m) => m.name))];
    const row = el("button", {
      type: "button", class: "ds-row" + (on ? " on" : ""), title: `use ${ds.path}`,
    },
      el("span", { class: "ds-nm" }, ds.key || "(root)"),
      el("span", { class: "ds-fmt" }, ds.format),
      el("span", { class: "ds-path" }, ds.path),
      el("span", { class: "ds-meta" }, `${ds.object_count} obj · ${fmtBytes(ds.bytes)}`),
      readers.length
        ? el("span", { class: "ds-read" }, `read by ${readers.join(", ")}`)
        : el("span", { class: "ds-read ds-unmapped" }, "unmapped"));
    if (ds.format_ambiguous) row.append(el("span", { class: "ds-warn", title: "objects here are not all one format" }, "⚠ mixed types"));
    row.addEventListener("click", () => onpick({ key: ds.key, path: ds.path, format: ds.format }));

    const group = el("div", { class: "ds-group" }, row);
    // delta/iceberg are single logical tables — their files aren't separately
    // selectable, so they never get a drill toggle
    if (ds.format !== "delta" && ds.format !== "iceberg" && ds.objects.length > 1) {
      const objects = el("div", { class: "ds-objects", hidden: "" });
      for (const o of ds.objects) {
        const leaf = el("button", { type: "button", class: "ds-object", title: `use just ${o.key}` },
          el("span", { class: "nm" }, o.key.split("/").pop()),
          el("span", { class: "dt" }, o.format));
        leaf.addEventListener("click", () => {
          onpick({ key: o.key, path: `s3://${bucket}/${o.key}`, format: o.format });
        });
        objects.append(leaf);
      }
      const drill = el("button", {
        type: "button", class: "ds-drill", "aria-expanded": "false",
        title: "pick one object instead of the whole prefix",
      }, el("span", { class: "tree-caret" }, "▸"), `${ds.objects.length} objects`);
      drill.addEventListener("click", () => {
        const open = objects.hidden;
        objects.hidden = !open;
        drill.setAttribute("aria-expanded", String(open));
        drill.classList.toggle("open", open);
      });
      group.append(drill, objects);
    }
    box.append(group);
  }
  return box;
}

// the guided forms' source picker — the session-cached bucket listing,
// rendered as the ledger above (FR-006)
export function datasetCards(onpick, current) {
  if (!datasets) {
    const box = el("div", { class: "ds-ledger" });
    box.append(note("bucket not reachable — enter a path manually below"));
    return box;
  }
  return datasetLedger(datasets.datasets, datasets.bucket, onpick, current);
}

// ── field + relationship builders ──

export const note = (text) => el("div", { class: "empty-note mf-note" }, text);

export function textField(label, value, oninput, ph = "") {
  const input = el("input", { value, placeholder: ph, spellcheck: "false" });
  input.addEventListener("input", () => oninput(input.value));
  return el("div", { class: "mf-field" }, el("div", { class: "field-label" }, label), input);
}

// Multi-line twin of textField, for a field worth more room than a single
// input row (e.g. a model's description) — auto-grows with its content
// rather than scrolling internally.
export function textAreaField(label, value, oninput, ph = "") {
  const ta = el("textarea", { placeholder: ph, spellcheck: "false", rows: "2", class: "mf-textarea" });
  ta.value = value;
  ta.addEventListener("input", () => oninput(ta.value));
  autoGrow(ta);
  return el("div", { class: "mf-field mf-field-wide" }, el("div", { class: "field-label" }, label), ta);
}

/* How a row's [start, end] interval is matched against a reporting period.
   Shared by the two point-in-time mechanisms — a spine dimension and an
   interval (`how: between`) import — because they answer the same question;
   mirrors MATCH_MODES in app/semantic.py. */
export const MATCH_MODES = [
  ["overlap", "it overlaps the period at all",
   "open Feb 2nd–15th counts in February, in Q1 and in the year — \"active during\""],
  ["period_start", "it was open on the period's first day",
   "a snapshot: February counts only what was already open on the 1st"],
  ["period_end", "it was open on the period's last day",
   "a snapshot: February counts only what was still open on the last day"],
];

export function matchRow(holder, set, label = "COUNT A ROW IN A PERIOD WHEN") {
  const sel = el("select", { class: "match" },
    ...MATCH_MODES.map(([v, lbl]) => el("option", { value: v }, lbl)));
  sel.value = holder.match || "overlap";
  sel.addEventListener("change", () => set(sel.value));
  return el("div", { class: "mf-anchor-row" },
    el("span", { class: "field-label" }, label), sel,
    el("span", { class: "mf-colcount" }, MATCH_MODES.find(([v]) => v === sel.value)[2]));
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
  const fmt = el("select", {}, ...["parquet", "csv", "delta", "iceberg"].map((f) => el("option", { value: f }, f)));
  fmt.value = current?.format || "parquet";
  const load = el("button", { class: "btn plain" }, "USE PATH");
  load.addEventListener("click", () => {
    if (path.value.trim()) onapply({ path: path.value.trim(), format: fmt.value });
  });
  return el("div", { class: "mf-manual" }, el("div", { class: "field-label" }, "OR TYPE A PATH"),
    el("div", { class: "mf-manual-row" }, path, fmt, load));
}

const UPLOAD_NAME_RE = /^[A-Za-z0-9_-]+$/;

/* A folder pick's File.webkitRelativePath is "<picked-folder>/sub/leaf.csv"
   — the picked folder's own name is meaningless (often a temp/export dir),
   so only what's under it travels to the server as the file's relative
   path; a plain (non-folder) pick has no webkitRelativePath at all. */
const relpathOf = (f) => {
  const parts = (f.webkitRelativePath || "").split("/");
  return parts.length > 1 ? parts.slice(1).join("/") : f.name;
};

/* Upload one or more .csv/.parquet files into the bucket under local/<name>/
   (POST /api/datasets/local) — never a file in the codebase, and not tied to
   any particular model: the same upload backs a fact model's source, a
   related dataset, or a common model's dataset, so it's just as often used
   standalone (the Modelling landing page's sidebar, `compact: true`) as it
   is inside a form's source picker. Takes either several individually-picked
   files or a whole folder (its structure preserved under local/<name>/).
   Invalidates the cached dataset listing so the upload shows up in
   datasetCards() right away, then hands the caller {path, format} — a form
   uses that to set its source/relation; a standalone caller can ignore it
   and just refresh its own view. */
export function uploadRow(onuploaded, { compact = false, label = "OR UPLOAD YOUR OWN .CSV / .PARQUET" } = {}) {
  const name = el("input", { placeholder: "dataset name (a-z, 0-9, _, -)", spellcheck: "false" });
  const filesInput = el("input", { type: "file", accept: ".csv,.parquet", multiple: "" });
  const folderInput = el("input", {
    type: "file", webkitdirectory: "", directory: "", multiple: "", style: "display:none",
  });
  const pickFolder = el("button", { class: "btn plain", type: "button" }, "OR PICK A FOLDER");
  const btn = el("button", { class: "btn plain" }, "UPLOAD");
  const msg = el("div", { class: "mf-note" });

  let selected = [];   // File[] — from whichever input was used last
  const noteSelection = () => {
    if (!selected.length) { msg.textContent = ""; return; }
    msg.textContent = `${selected.length} file${selected.length === 1 ? "" : "s"} selected`;
  };
  pickFolder.addEventListener("click", (e) => { e.preventDefault(); folderInput.click(); });
  filesInput.addEventListener("change", () => {
    selected = [...filesInput.files];
    folderInput.value = "";
    noteSelection();
  });
  folderInput.addEventListener("change", () => {
    selected = [...folderInput.files];
    filesInput.value = "";
    noteSelection();
  });

  btn.addEventListener("click", async () => {
    const nm = name.value.trim();
    if (!nm || !UPLOAD_NAME_RE.test(nm)) { msg.textContent = "name must be alphanumeric (a-z, 0-9, _, -)"; return; }
    if (!selected.length) { msg.textContent = "choose one or more .csv/.parquet files, or a folder"; return; }
    btn.disabled = true;
    msg.textContent = "uploading…";
    try {
      const fd = new FormData();
      fd.append("name", nm);
      for (const f of selected) fd.append("files", f, relpathOf(f));
      const res = await apiUpload("/api/datasets/local", fd);
      datasets = null;   // force a refetch so the upload appears in datasetCards()
      await loadDatasets();
      const skippedNote = res.skipped.length ? ` (skipped ${res.skipped.length}: ${res.skipped.join(", ")})` : "";
      msg.textContent = `uploaded ${res.uploaded.length} file${res.uploaded.length === 1 ? "" : "s"}${skippedNote}`;
      onuploaded({ path: res.path, format: res.format });
    } catch (e) {
      msg.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  });
  return el("div", { class: "mf-manual" },
    el("div", { class: "field-label" }, label),
    el("div", { class: compact ? "mf-upload-compact" : "mf-manual-row" }, name, filesInput, pickFolder, btn),
    folderInput,
    msg);
}

/* default label for a column ticked as a dimension */
export const titleCase = (name) => name.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());

/* default spec dict for a column becoming a dimension */
export const dimFromColumn = (c) => ({
  name: c.name, column: c.name, label: titleCase(c.name),
  type: /date|time/i.test(c.dtype) ? "time" : "categorical",
  description: "", spine: null, geo: null, grain: null, synonyms: [],
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

// ── time-spine dimension (point-in-time "active" measures) ──
// Not backed by one column: it's a generated timeline interval-joined
// against a [start, end] column pair (engine.py's _spine_prepare/query) —
// e.g. subscriptions.yaml's active_at, backing "active customers"/"MRR"
// measures that mean "as of this point in time", not "on this exact row's
// date". Distinct creation flow from a plain column tick since it needs two
// columns, not one, and the server requires type: time on the result.
export function spineCreatePanel(cols, { onapply, ondismiss }) {
  const dateCols = cols.filter((c) => /date|time/i.test(c.dtype));
  const pick = dateCols.length ? dateCols : cols;
  const opt = (c) => el("option", { value: c.name }, `${c.name} · ${c.dtype}`);
  const name = el("input", { placeholder: "active_at", spellcheck: "false" });
  const label = el("input", { placeholder: "Active At", spellcheck: "false" });
  const startSel = el("select", {}, el("option", { value: "" }, "— start column —"), ...pick.map(opt));
  const endSel = el("select", {}, el("option", { value: "" }, "— end column —"), ...pick.map(opt));
  const matchSel = el("select", { class: "match" },
    ...MATCH_MODES.map(([v, lbl]) => el("option", { value: v }, lbl)));
  const create = el("button", { class: "btn" }, "CREATE SPINE DIMENSION");
  create.addEventListener("click", () => {
    const n = name.value.trim();
    if (!n || !startSel.value || !endSel.value) return;
    onapply({
      name: n, column: n, label: label.value.trim() || titleCase(n), type: "time",
      description: "", geo: null, synonyms: [],
      spine: { start: startSel.value, end: endSel.value, match: matchSel.value },
    });
  });
  const cancel = el("button", { class: "btn plain" }, "CANCEL");
  cancel.addEventListener("click", ondismiss);
  return el("div", { class: "mf-import-cols" },
    el("div", { class: "mf-import-head" }, el("span", { class: "field-label" }, "NEW TIME-SPINE DIMENSION")),
    note("a generated timeline, not a real column — every row counts in each period it's active for "
      + "(the [start, end] interval; a null end means still active), at whatever grain the query asks "
      + "for. Powers point-in-time \"active\" measures like active customers or MRR — see "
      + "subscriptions.yaml for a worked example."),
    matchRow({ match: "overlap" }, () => {}),
    el("div", { class: "mf-row3" },
      el("div", { class: "mf-field" }, el("div", { class: "field-label" }, "NAME"), name),
      el("div", { class: "mf-field" }, el("div", { class: "field-label" }, "LABEL"), label)),
    el("div", { class: "mf-row3", style: "margin-top:8px" },
      el("div", { class: "mf-field" }, el("div", { class: "field-label" }, "START COLUMN"), startSel),
      el("div", { class: "mf-field" }, el("div", { class: "field-label" }, "END COLUMN (nullable = still active)"), endSel)),
    el("div", { class: "mf-import-actions" }, create, cancel));
}

/* inline start/end column selects for an already-declared spine dimension —
   the row-level counterpart of spineCreatePanel, for editing one in place */
export function spineFields(dim, cols, onchange) {
  const dateCols = cols.filter((c) => /date|time/i.test(c.dtype));
  const pick = dateCols.length ? dateCols : cols;
  const sel = (val, set) => {
    const s = el("select", {}, ...pick.map((c) => el("option", { value: c.name }, c.name)));
    if (val && !pick.some((c) => c.name === val)) s.append(el("option", { value: val }, val));
    s.value = val;
    s.addEventListener("change", () => { set(s.value); onchange(); });
    return s;
  };
  const matchSel = el("select", { class: "match" },
    ...MATCH_MODES.map(([v, lbl]) => el("option", { value: v }, lbl)));
  matchSel.value = dim.spine.match || "overlap";
  matchSel.title = MATCH_MODES.find(([v]) => v === matchSel.value)[2];
  matchSel.addEventListener("change", () => { dim.spine.match = matchSel.value; onchange(); });
  return el("div", { class: "mf-spine-fields" },
    el("span", { class: "mf-colcount" }, "⧗ spine"),
    el("span", { class: "field-label" }, "START"), sel(dim.spine.start, (v) => { dim.spine.start = v; }),
    el("span", { class: "field-label" }, "END"), sel(dim.spine.end, (v) => { dim.spine.end = v; }),
    el("span", { class: "field-label" }, "COUNTS WHEN"), matchSel);
}

// ── auto-growing textarea (single-line look, grows with content) ──
export function autoGrow(ta) {
  const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight + 2, 220) + "px"; };
  ta.addEventListener("input", fit);
  // fit once mounted (scrollHeight is 0 while detached)
  requestAnimationFrame(fit);
  return ta;
}
