/* Sandbox: ad hoc polars/python scratch notebooks. Multiple cells share one
   namespace when run (see app/sandbox_runner.py) — there's no persistent
   kernel between separate runs, each run replays every cell from the top
   through the one you clicked RUN on. Same trust/role posture as a pipeline
   script (app/pipelines.py): any signed-in role can browse saved notebooks
   read-only, only admin can edit/run/save/delete/convert.

   Cell DOM nodes are created once per structural change (open/add/move/
   delete cell) and reused for typing/output updates — a full rebuild on
   every keystroke would steal focus mid-type, so `cellRefs` keeps direct
   references to each cell's textarea/highlight/output element. */
"use strict";

import { isAdmin } from "./auth.js";
import { openEditor } from "./editor.js";
import { makeCompleter } from "./completion.js";
import { $, api, el, fmtBytes } from "./lib.js";
import { highlightPython } from "./pyhighlight.js";
import { navigate, paths, setPath } from "./router.js";
import { hooks, showView, state } from "./state.js";

export const sandbox = {
  id: null, name: "untitled_sandbox", cells: [], dirty: false,
  bucketFiles: [], listFilter: "", filesFilter: "",
};

let cellSeq = 0;
const newCellId = () => `c${Date.now().toString(36)}_${++cellSeq}`;
const newCell = (source = "") => ({ id: newCellId(), source, output: null });

const TEMPLATE_SOURCE =
  '# pick a file under "Bucket Files" (right) to insert a read("s3://…") call —\n'
  + "# read() infers parquet/csv/delta from the path (iceberg needs an explicit\n"
  + '# read("...", format="iceberg")), and later cells can use earlier cells\'\n'
  + "# variables, like a real notebook\n"
  + 'df = read("s3://cash-intel/sales/*.parquet")\ndf';

// { id -> { row, ta, pre, outBox } } — rebuilt every renderCells() call.
let cellRefs = new Map();
let lastFocusedTa = null;

// ── saved-notebook list (left rail) ──────────────────────────────────────

let notebookList = [];

async function refreshList() {
  try {
    notebookList = await api("/api/sandbox/notebooks");
  } catch {
    notebookList = [];
  }
  renderList();
}

function renderList() {
  const box = $("#sbx-list");
  box.innerHTML = "";
  const q = sandbox.listFilter.toLowerCase();
  const filtered = notebookList.filter((n) => n.name.toLowerCase().includes(q));
  if (!filtered.length) {
    box.append(el("div", { class: "empty-note" }, notebookList.length ? "no matches" : "none saved yet"));
    return;
  }
  for (const nb of filtered) {
    const row = el("div", { class: "mk-row clickable" + (nb.id === sandbox.id ? " on" : "") },
      el("span", { class: "nm" }, nb.name),
      el("span", { class: "mk-meta" }, (nb.updated_at || "").replace("T", " ").slice(0, 16)));
    row.addEventListener("click", () => navigate(paths.sandboxNotebook(nb.id)));
    box.append(row);
  }
}

export function setListFilter(v) { sandbox.listFilter = v; renderList(); }

// ── bucket file browser (right rail) ─────────────────────────────────────

function inferFormat(key) {
  const lower = key.toLowerCase();
  if (lower.endsWith(".csv")) return "csv";
  if (lower.endsWith(".parquet")) return "parquet";
  if (lower.endsWith(".metadata.json")) return "iceberg";
  return "delta";
}

async function loadBucketFiles() {
  try {
    const data = await api("/api/explorer");
    sandbox.bucketFiles = data.files.map((f) => ({
      path: `s3://${data.bucket}/${f.key}`, key: f.key, size: f.size, format: inferFormat(f.key),
    }));
  } catch {
    sandbox.bucketFiles = [];
  }
  renderFiles();
}

function renderFiles() {
  const box = $("#sbx-files");
  box.innerHTML = "";
  const q = sandbox.filesFilter.toLowerCase();
  const filtered = sandbox.bucketFiles.filter((f) => f.key.toLowerCase().includes(q));
  if (!filtered.length) {
    box.append(el("div", { class: "empty-note" }, sandbox.bucketFiles.length ? "no matches" : "bucket not reachable"));
    return;
  }
  for (const f of filtered.slice(0, 300)) {
    const chip = el("div", { class: "col-chip", title: `insert read("${f.path}") at the cursor` },
      el("span", {}, f.key), el("span", { class: "dt" }, `${f.format} · ${fmtBytes(f.size)}`));
    chip.addEventListener("click", () => insertReadAtActiveCell(f));
    box.append(chip);
  }
}

export function setFilesFilter(v) { sandbox.filesFilter = v; renderFiles(); }

function insertAtCursorTa(ta, text) {
  const s = ta.selectionStart, e = ta.selectionEnd;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + text.length;
  ta.focus();
  ta.dispatchEvent(new Event("input", { bubbles: true }));
}

function insertReadAtActiveCell(f) {
  if (!isAdmin()) return;
  const ta = lastFocusedTa && document.body.contains(lastFocusedTa) ? lastFocusedTa : null;
  if (!ta) { alert("click inside a cell first, then click a file to insert it there"); return; }
  insertAtCursorTa(ta, `read("${f.path}")`);
}

// ── autocomplete: read("...") bucket paths, pl.xxx members, bare names ───

const PL_MEMBERS = [
  ["DataFrame(", "construct a DataFrame"], ["LazyFrame(", "construct a LazyFrame"],
  ["scan_parquet(", "lazily scan parquet"], ["scan_csv(", "lazily scan csv"],
  ["scan_delta(", "lazily scan a Delta table"], ["scan_iceberg(", "lazily scan an Iceberg table"],
  ["read_parquet(", "eagerly read parquet"],
  ["col(", "reference a column"], ["when(", "conditional expression"],
  ["concat(", "concatenate frames"], ["lit(", "a literal value"],
  ["Config", "polars configuration"], ["Int64", "dtype"], ["Float64", "dtype"],
  ["Utf8", "dtype"], ["Boolean", "dtype"], ["Date", "dtype"], ["Datetime", "dtype"],
];

function assignedNames() {
  const names = new Set(["read", "pl", "bucket"]);
  const re = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=[^=]/gm;
  for (const cell of sandbox.cells) {
    let m;
    while ((m = re.exec(cell.source))) names.add(m[1]);
  }
  return names;
}

function sandboxResolve(upto, after, caret) {
  const line = upto.slice(upto.lastIndexOf("\n") + 1);

  let m = upto.match(/read\(\s*(["'])([^"']*)$/);
  if (m) {
    const prefix = m[2];
    const closer = after.startsWith('"') || after.startsWith("'") ? "" : '")';
    const skip = closer ? 0 : 2;
    const items = sandbox.bucketFiles
      .filter((f) => f.path.toLowerCase().includes(prefix.toLowerCase()))
      .slice(0, 30)
      .map((f) => ({ text: f.path, hint: f.format, insert: f.path + closer, caretOffset: skip }));
    if (items.length) return { items, start: caret - prefix.length };
  }

  m = upto.match(/pl\.([A-Za-z_]*)$/);
  if (m) {
    const prefix = m[1];
    const items = PL_MEMBERS
      .filter(([t]) => t.toLowerCase().startsWith(prefix.toLowerCase()))
      .map(([t, hint]) => ({ text: t, hint, insert: t, caretOffset: 0 }));
    if (items.length) return { items, start: caret - prefix.length };
  }

  m = line.match(/(?:^|[-+*/%()<>=,!&|:[\s])([A-Za-z_][A-Za-z0-9_]*)$/);
  if (m) {
    const prefix = m[1];
    const items = [...assignedNames()].filter((n) => n.startsWith(prefix) && n !== prefix)
      .sort()
      .map((n) => ({ text: n, hint: "", insert: n, caretOffset: 0 }));
    if (items.length) return { items, start: caret - prefix.length };
  }
  return null;
}

// ── cell rendering ────────────────────────────────────────────────────────

function autoSize(ta, wrap) {
  wrap.style.height = Math.max(ta.scrollHeight, 44) + "px";
}

function renderCellOutput(box, output) {
  box.innerHTML = "";
  if (!output) return;
  if (output.ok === null) return;   // beyond run_upto — nothing to show
  if (output.stdout) box.append(el("pre", { class: "sbx-stdout" }, output.stdout));
  if (!output.ok) {
    box.append(el("pre", { class: "sbx-error" }, output.error));
    return;
  }
  const d = output.display;
  if (!d) return;
  if (d.kind === "text") {
    box.append(el("pre", { class: "sbx-text" }, d.text));
    return;
  }
  const wrap = el("div", { class: "sbx-table-wrap" });
  const thead = el("thead", {}, el("tr", {},
    ...d.columns.map((c) => el("th", {}, c.name, el("span", { class: "dt" }, ` ${c.dtype}`)))));
  const tbody = el("tbody", {}, ...d.rows.map((r) => el("tr", {},
    ...d.columns.map((c) => el("td", {}, r[c.name] === null || r[c.name] === undefined ? "∅" : String(r[c.name]))))));
  wrap.append(el("table", { class: "data" }, thead, tbody));
  box.append(wrap);
  box.append(el("div", { class: "empty-note" },
    d.truncated ? `showing first ${d.rows.length} row(s) (truncated)` : `${d.row_count} row(s)`));
}

function renderCellRow(cell, idx, total) {
  const admin = isAdmin();
  const head = el("div", { class: "sbx-cell-head" },
    el("span", { class: "sbx-cell-idx" }, `[${idx + 1}]`));
  if (admin) {
    const runBtn = el("button", { class: "btn plain", title: "run every cell from the top through this one" }, "▶ RUN");
    runBtn.addEventListener("click", () => runThrough(idx));
    const upBtn = el("button", { class: "btn plain", title: "move up" }, "↑");
    upBtn.disabled = idx === 0;
    upBtn.addEventListener("click", () => moveCell(idx, -1));
    const downBtn = el("button", { class: "btn plain", title: "move down" }, "↓");
    downBtn.disabled = idx === total - 1;
    downBtn.addEventListener("click", () => moveCell(idx, 1));
    const addBtn = el("button", { class: "btn plain", title: "insert a cell below" }, "+");
    addBtn.addEventListener("click", () => addCellAt(idx + 1));
    const delBtn = el("button", { class: "btn plain", title: "delete this cell" }, "✕");
    delBtn.addEventListener("click", () => deleteCell(idx));
    head.append(runBtn, upBtn, downBtn, addBtn, delBtn);
  }

  const editorWrap = el("div", { class: "sbx-cell-editor" });
  const pre = el("pre", { class: "py-highlight", "aria-hidden": "true" }, el("code", {}));
  const ta = el("textarea", { spellcheck: "false", autocomplete: "off", rows: "1" });
  ta.value = cell.source;
  if (!admin) ta.readOnly = true;
  const suggestBox = el("div", { class: "sbx-suggest", hidden: "" });
  editorWrap.append(pre, ta, suggestBox);

  const outBox = el("div", { class: "sbx-cell-output" });
  renderCellOutput(outBox, cell.output);

  const row = el("div", { class: "sbx-cell" }, head, editorWrap, outBox);

  const refreshHighlight = () => {
    pre.querySelector("code").innerHTML = highlightPython(ta.value);
    autoSize(ta, editorWrap);
  };
  refreshHighlight();

  if (admin) {
    const completer = makeCompleter(ta, suggestBox, sandboxResolve, null);
    ta.addEventListener("input", () => {
      cell.source = ta.value;
      markDirty();
      refreshHighlight();
      completer.update();
    });
    ta.addEventListener("keydown", (e) => {
      if (completer.onKeydown(e)) return;
      if (e.key === "Tab") { e.preventDefault(); insertAtCursorTa(ta, "    "); refreshHighlight(); }
    });
    ta.addEventListener("blur", () => setTimeout(() => completer.hide(), 150));
    ta.addEventListener("focus", () => { lastFocusedTa = ta; });
  }
  ta.addEventListener("scroll", () => { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; });

  cellRefs.set(cell.id, { row, ta, pre, outBox, refreshHighlight });
  return row;
}

function renderCells() {
  cellRefs = new Map();
  const box = $("#sbx-cells");
  box.innerHTML = "";
  sandbox.cells.forEach((cell, idx) => box.append(renderCellRow(cell, idx, sandbox.cells.length)));
  // a detached node's textarea reports scrollHeight 0 (no layout yet), so the
  // auto-size baked into renderCellRow's first refreshHighlight() undersizes
  // every cell — redo it once every row is actually in the live document.
  for (const ref of cellRefs.values()) ref.refreshHighlight();
}

// ── structural cell edits (rebuild the whole list — infrequent clicks) ──

function addCellAt(idx) {
  sandbox.cells.splice(idx, 0, newCell());
  markDirty();
  renderCells();
  const added = cellRefs.get(sandbox.cells[idx].id);
  if (added) added.ta.focus();
}

function deleteCell(idx) {
  if (sandbox.cells.length === 1) { sandbox.cells[0] = newCell(); }
  else sandbox.cells.splice(idx, 1);
  markDirty();
  renderCells();
}

function moveCell(idx, dir) {
  const j = idx + dir;
  if (j < 0 || j >= sandbox.cells.length) return;
  [sandbox.cells[idx], sandbox.cells[j]] = [sandbox.cells[j], sandbox.cells[idx]];
  markDirty();
  renderCells();
}

// ── running ───────────────────────────────────────────────────────────────

async function runThrough(idx) {
  if (!isAdmin()) return;
  updateStatus("running…");
  try {
    const res = await api("/api/sandbox/run", {
      method: "POST",
      body: { cells: sandbox.cells.map((c) => ({ id: c.id, source: c.source })), run_upto: idx },
    });
    if (!res.ok && !res.cells.length) {
      alert(`Run failed: ${res.error}`);
      updateStatus();
      return;
    }
    const byId = new Map(res.cells.map((c) => [c.id, c]));
    for (const cell of sandbox.cells) {
      cell.output = byId.get(cell.id) || null;
      const ref = cellRefs.get(cell.id);
      if (ref) renderCellOutput(ref.outBox, cell.output);
    }
  } catch (err) {
    alert(`Run failed: ${err.message}`);
  }
  updateStatus();
}

// ── save / delete ────────────────────────────────────────────────────────

function updateStatus(transient) {
  $("#sbx-status").innerHTML = transient ? transient
    : sandbox.dirty ? '<span class="warn">● unsaved</span>' : '<span class="ok">saved</span>';
}

function markDirty() {
  sandbox.dirty = true;
  updateStatus();
}

async function saveSandbox() {
  const name = $("#sbx-name").value.trim() || "untitled_sandbox";
  sandbox.name = name;
  const cellsOut = sandbox.cells.map((c) => ({ id: c.id, source: c.source }));
  updateStatus("saving…");
  try {
    const saved = sandbox.id
      ? await api(`/api/sandbox/notebooks/${sandbox.id}`, { method: "PUT", body: { name, cells: cellsOut } })
      : await api("/api/sandbox/notebooks", { method: "POST", body: { name, cells: cellsOut } });
    sandbox.id = saved.id;
    sandbox.dirty = false;
    $("#sbx-delete").hidden = !isAdmin();
    updateStatus();
    setPath(paths.sandboxNotebook(saved.id));
    await refreshList();
  } catch (err) {
    updateStatus();
    alert(`Save failed: ${err.message}`);
  }
}

async function saveAsNewSandbox() {
  sandbox.id = null;
  await saveSandbox();
}

async function deleteSandbox() {
  if (!sandbox.id) return;
  if (!confirm(`Delete sandbox notebook '${sandbox.name}'? This cannot be undone.`)) return;
  try {
    await api(`/api/sandbox/notebooks/${sandbox.id}`, { method: "DELETE" });
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
    return;
  }
  sandbox.dirty = false;
  await refreshList();
  navigate(paths.sandbox());
}

// ── convert to pipeline ──────────────────────────────────────────────────

async function convertToPipeline() {
  if (!sandbox.cells.some((c) => c.source.trim())) {
    alert("nothing to convert — write some code first");
    return;
  }
  let data;
  try {
    data = await api("/api/sandbox/convert", {
      method: "POST",
      body: { name: sandbox.name || "untitled_sandbox", cells: sandbox.cells.map((c) => ({ id: c.id, source: c.source })) },
    });
  } catch (err) {
    alert(`Could not convert: ${err.message}`);
    return;
  }
  if (data.warnings.length) alert(data.warnings.join("\n"));
  openEditor("pipeline", null, { text: data.yaml });
  setPath(paths.modellingNewPipelineYaml());
}

// ── opening ──────────────────────────────────────────────────────────────

export async function openSandbox(id) {
  if (id) {
    let nb;
    try {
      nb = await api(`/api/sandbox/notebooks/${id}`);
    } catch (err) {
      alert(`Could not open sandbox notebook: ${err.message}`);
      return navigate(paths.sandbox(), { replace: true });
    }
    sandbox.id = nb.id;
    sandbox.name = nb.name;
    sandbox.cells = nb.cells.length ? nb.cells.map((c) => ({ id: c.id, source: c.source, output: null })) : [newCell()];
  } else {
    sandbox.id = null;
    sandbox.name = "untitled_sandbox";
    sandbox.cells = [newCell(TEMPLATE_SOURCE)];
  }
  sandbox.dirty = false;
  $("#sbx-name").value = sandbox.name;
  $("#sbx-name").readOnly = !isAdmin();
  $("#sbx-delete").hidden = !sandbox.id || !isAdmin();
  updateStatus();
  showView("sandbox");
  renderCells();
  loadBucketFiles();
  refreshList();
}
hooks.openSandbox = openSandbox;

export function confirmLeaveSandbox() {
  if (!sandbox.dirty) return true;
  return confirm("You have unsaved changes to this sandbox notebook. Discard them?");
}
hooks.confirmLeaveSandbox = confirmLeaveSandbox;

// ── wiring (called once from main.js) ────────────────────────────────────

export function attachSandbox() {
  $("#sbx-name").addEventListener("input", () => { sandbox.name = $("#sbx-name").value; markDirty(); });
  $("#sbx-save").addEventListener("click", saveSandbox);
  $("#sbx-save-as").addEventListener("click", saveAsNewSandbox);
  $("#sbx-delete").addEventListener("click", deleteSandbox);
  $("#sbx-new").addEventListener("click", () => navigate(paths.sandbox()));
  $("#sbx-add-cell").addEventListener("click", () => addCellAt(sandbox.cells.length));
  $("#sbx-run-all").addEventListener("click", () => runThrough(sandbox.cells.length - 1));
  $("#sbx-convert").addEventListener("click", convertToPipeline);
  $("#sbx-back").addEventListener("click", () => navigate(paths.home()));
  $("#sbx-list-filter").addEventListener("input", (e) => setListFilter(e.target.value));
  $("#sbx-files-filter").addEventListener("input", (e) => setFilesFilter(e.target.value));
  window.addEventListener("beforeunload", (e) => {
    if (sandbox.dirty && state.view === "sandbox") { e.preventDefault(); e.returnValue = ""; }
  });
}
