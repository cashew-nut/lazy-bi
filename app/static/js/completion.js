/* Reusable expression/column completion engine.

   Extracted from measurelab.js so the measure lab and the model YAML editor
   share ONE completion implementation and ONE vocabulary (no drift). A caller
   supplies a `resolve(upto, after, caret)` that returns `{items, start}` (or
   null) for the current caret position; this module owns the popup, keyboard
   navigation, and insertion. */
"use strict";

import { el } from "./lib.js";

// completion vocabulary: [insert, hint, caretOffset]
export const TOP_FNS = [
  ['col("")', "reference a column", -2],
  ["len()", "row count", 0],
  ["lit()", "literal value", -1],
  ["when().then().otherwise()", "conditional", -20],
];
export const METHODS = [
  ["sum()", "total", 0], ["mean()", "average", 0], ["median()", "median", 0],
  ["min()", "minimum", 0], ["max()", "maximum", 0],
  ["n_unique()", "distinct count", 0], ["count()", "non-null count", 0],
  ["std()", "std deviation", 0], ["quantile(0.5)", "quantile", -1],
  ["first()", "first value", 0], ["last()", "last value", 0],
  ["abs()", "absolute", 0], ["round(2)", "round", -1],
  ["cast(pl.Float64)", "change type", -1], ["fill_null(0)", "replace nulls", -1],
  ["is_null()", "null test", 0], ["is_not_null()", "non-null test", 0],
  ["filter()", "aggregate matching rows only", -1],
  ["dt.year()", "extract year", 0], ["str.contains()", "text match", -1],
];

// Classify a polars-expression trigger in the text before the caret.
// Returns { kind: "col"|"top"|"method", prefix, start } or null.
export function polarsContext(upto, caret) {
  let m;
  if ((m = upto.match(/pl\.col\(\s*["']([A-Za-z0-9_ ]*)$/)))
    return { kind: "col", prefix: m[1], start: caret - m[1].length };
  if ((m = upto.match(/(?:^|[^A-Za-z0-9_.])pl\.([a-z_]*)$/)))
    return { kind: "top", prefix: m[1], start: caret - m[1].length };
  if ((m = upto.match(/[)\]"'A-Za-z0-9_]\.([a-z_]*)$/)))
    return { kind: "method", prefix: m[1], start: caret - m[1].length };
  return null;
}

// Build completion items for a polars context from a schema column list.
export function polarsItems(ctx, columns, after) {
  if (ctx.kind === "col") {
    // don't double the closer if a quote already follows the caret
    const closer = after.startsWith('"') ? "" : '")';
    const skip = closer ? 0 : 2;  // hop over the existing `")` instead
    return (columns || [])
      .filter((c) => c.name.toLowerCase().startsWith(ctx.prefix.toLowerCase()))
      .map((c) => ({ text: c.name, hint: c.dtype, insert: c.name + closer, caretOffset: skip }));
  }
  const source = ctx.kind === "top" ? TOP_FNS : METHODS;
  return source
    .filter(([t]) => t.startsWith(ctx.prefix))
    .map(([t, hint, off]) => ({ text: t, hint, insert: t, caretOffset: off }));
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
