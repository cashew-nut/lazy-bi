/* Reusable expression/column completion engine.

   Extracted from measurelab.js so the measure lab and the model YAML editor
   share ONE completion implementation and ONE vocabulary (no drift). A caller
   supplies a `resolve(upto, after, caret)` that returns `{items, start}` (or
   null) for the current caret position; this module owns the popup, keyboard
   navigation, and insertion. */
"use strict";

import { el } from "./lib.js";

// completion vocabulary for the safe measure DSL (see
// specs/008-safe-measure-compilation/contracts/compile_measure.md) — a small
// set of allowlisted functions called directly, no `pl.` prefix and no
// `.method()` chaining: [insert, hint, caretOffset]
export const DSL_FUNCTIONS = [
  ['col("")', "reference a column", -2],
  ["sum()", "total", -1], ["mean()", "average", -1], ["median()", "median", -1],
  ["min()", "minimum", -1], ["max()", "maximum", -1],
  ["count()", "row count (no arg) or non-null count of a column", -1],
  ["count_distinct()", "distinct count", -1],
  ["std()", "standard deviation", -1], ["var()", "variance", -1],
  ["first()", "first value", -1], ["last()", "last value", -1],
  ["where()", 'filter before aggregating: where(value, predicate)', -1],
  ["if_()", "conditional: if_(predicate, then, else)", -1],
  ["coalesce()", "first non-null of the arguments", -1],
  ["cast()", 'change type: cast(value, "int"|"float"|"str"|"bool")', -1],
  // window functions: reference sibling *measures* (not raw columns), and
  // need a time dimension in the query to order by — e.g.
  // running_total(revenue), (revenue - lag(revenue, 1)) / lag(revenue, 1)
  ["running_total()", "cumulative sum over the query's date axis: running_total(measure)", -1],
  ["lag()", "value from n periods back: lag(measure[, periods=1])", -1],
];

// Classify a measure-DSL trigger in the text before the caret.
// Returns { kind: "col"|"name", prefix, start } or null.
export function dslContext(upto, caret) {
  let m;
  if ((m = upto.match(/col\(\s*["']([A-Za-z0-9_ ]*)$/)))
    return { kind: "col", prefix: m[1], start: caret - m[1].length };
  // a bare identifier right after a natural expression boundary (start of
  // value, an operator, a comma/paren, or `and`/`or`/`not`/`where(`) — either
  // a function name or a column reference are valid there
  if ((m = upto.match(/(?:^|[-+*/%()<>=,!&|:]|\b(?:and|or|not|where)\()\s*([A-Za-z_][A-Za-z0-9_]*)$/)))
    return { kind: "name", prefix: m[1], start: caret - m[1].length };
  return null;
}

// Build completion items for a DSL context from a schema column list.
export function dslItems(ctx, columns, after) {
  const cols = (columns || [])
    .filter((c) => c.name.toLowerCase().startsWith(ctx.prefix.toLowerCase()));
  if (ctx.kind === "col") {
    // don't double the closer if a quote already follows the caret
    const closer = after.startsWith('"') ? "" : '")';
    const skip = closer ? 0 : 2;  // hop over the existing `")` instead
    return cols.map((c) => ({ text: c.name, hint: c.dtype, insert: c.name + closer, caretOffset: skip }));
  }
  const fns = DSL_FUNCTIONS
    .filter(([t]) => t.startsWith(ctx.prefix))
    .map(([t, hint, off]) => ({ text: t, hint, insert: t, caretOffset: off }));
  return [...fns, ...cols.map((c) => ({ text: c.name, hint: c.dtype + " (column)", insert: c.name, caretOffset: 0 }))];
}

// Bind a completion popup to a textarea + box element.
// resolve(upto, after, caret) -> { items:[{text,hint,insert,caretOffset}], start } | null
// onApply() runs after an item is inserted (e.g. to re-validate).
export function makeCompleter(textarea, box, resolve, onApply) {
  const sug = { items: [], index: 0, start: 0 };

  function update() {
    const caret = textarea.selectionStart;
    const upto = textarea.value.slice(0, caret);
    const after = textarea.value.slice(caret);
    const res = resolve(upto, after, caret);
    if (!res || !res.items.length) return hide();
    sug.items = res.items.slice(0, 8);
    sug.index = 0;
    sug.start = res.start;
    render();
  }
  function render() {
    box.innerHTML = "";
    sug.items.forEach((item, i) => {
      const row = el("div", { class: "sug" + (i === sug.index ? " sel" : "") },
        el("span", {}, item.text), el("span", { class: "hint" }, item.hint));
      row.addEventListener("mousedown", (e) => { e.preventDefault(); apply(item); });
      box.append(row);
    });
    box.hidden = false;
  }
  function hide() { box.hidden = true; sug.items = []; }
  function apply(item) {
    const end = textarea.selectionStart;
    textarea.value = textarea.value.slice(0, sug.start) + item.insert + textarea.value.slice(end);
    const caret = sug.start + item.insert.length + item.caretOffset;
    textarea.selectionStart = textarea.selectionEnd = caret;
    textarea.focus();
    hide();
    // setting .value programmatically fires no native "input" event, but
    // callers rely on one (e.g. to mirror the field into their own state) —
    // dispatch it ourselves so an applied suggestion looks like a keystroke
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    update();          // e.g. col("") immediately offers columns
    if (onApply) onApply();
  }
  function onKeydown(e) {
    if (box.hidden) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = sug.items.length;
      sug.index = (sug.index + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      render();
      return true;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      apply(sug.items[sug.index]);
      return true;
    }
    if (e.key === "Escape") { hide(); return true; }
    return false;
  }
  return { update, hide, onKeydown, isOpen: () => !box.hidden };
}
