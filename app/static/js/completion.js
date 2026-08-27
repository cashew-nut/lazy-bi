/* Reusable expression/column completion engine.

   Extracted from measurelab.js so the measure lab and the model YAML editor
   share ONE completion implementation and ONE vocabulary (no drift). A caller
   supplies a `resolve(upto, after, caret)` that returns `{items, start}` (or
   null) for the current caret position; this module owns the popup, keyboard
   navigation, and insertion.

   The vocabulary is SQL: a measure is an aggregate, a window measure is a
   window function over the engine-supplied `w`, and a column is written as
   its own name rather than wrapped in a call. */
"use strict";

import { el } from "./lib.js";

// completion vocabulary for the SQL measure grammar (see
// specs/018-duckdb-sql-engine/contracts/measure-sql.md). A measure is a SQL
// aggregate, so these are the aggregates worth offering plus the few scalar
// shapes that show up inside one: [insert, hint, caretOffset]
export const DSL_FUNCTIONS = [
  ["SUM()", "total", -1],
  ["COUNT(*)", "row count", 0],
  ["COUNT()", "non-null count of a column", -1],
  ["COUNT(DISTINCT )", "distinct count", -1],
  ["AVG()", "average", -1], ["MEDIAN()", "median", -1],
  ["MIN()", "minimum", -1], ["MAX()", "maximum", -1],
  ["STDDEV()", "standard deviation", -1], ["VARIANCE()", "variance", -1],
  ["FIRST()", "first value", -1], ["LAST()", "last value", -1],
  ["QUANTILE_CONT(, 0.95)", "a percentile — 0.95 is the 95th", -7],
  ["ARG_MAX(, )", "the value of one column where another is highest", -4],
  ["FILTER (WHERE )", "aggregate only the rows matching a predicate", -1],
  ["CASE WHEN  THEN  ELSE  END", "conditional", -21],
  ["COALESCE(, )", "first non-null of the arguments", -4],
  ["CAST( AS BIGINT)", "change type", -12],
  // window functions: reference sibling *measures* (not raw columns), and
  // need a time dimension in the query to order by. `w` is the window the
  // engine supplies — PARTITION BY the query's other dimensions, ORDER BY
  // its time dimension — so you never write its contents yourself.
  ["SUM() OVER w", "running total over the query's date axis", -8],
  ["LAG() OVER w", "value from the previous period: LAG(measure[, n]) OVER w", -8],
  ["LEAD() OVER w", "value from the next period", -8],
  ["RANK() OVER w", "rank within the partition", 0],
  ["ROW_NUMBER() OVER w", "position within the partition", 0],
];

// param('') as a bare-name suggestion (offered alongside DSL_FUNCTIONS) —
// kept out of the static list above and synthesized only when the caller
// actually has parameters to offer (dslItems' `parameters` arg is
// non-empty). The model yaml editor and guided model form never pass
// parameters (model measures can't reference one — FR-007), so they never
// see this suggested, rather than offering something that would be
// rejected on save.
const PARAM_FN = ["param('')", "reference a declared parameter — legal anywhere a literal is", -2];

// Classify a measure-grammar trigger in the text before the caret.
// Returns { kind: "col"|"param"|"name", prefix, start } or null.
export function dslContext(upto, caret) {
  let m;
  if ((m = upto.match(/param\(\s*["']([A-Za-z0-9_]*)$/)))
    return { kind: "param", prefix: m[1], start: caret - m[1].length };
  // a bare identifier right after a natural expression boundary (start of
  // value, an operator, a comma/paren, or a SQL keyword that introduces one)
  // — either a function name or a column reference is valid there
  if ((m = upto.match(
    /(?:^|[-+*/%()<>=,!&|:]|\b(?:AND|OR|NOT|WHERE|THEN|ELSE|WHEN|DISTINCT|BY|AS|FILTER)\s)\s*([A-Za-z_][A-Za-z0-9_]*)$/i)))
    return { kind: "name", prefix: m[1], start: caret - m[1].length };
  return null;
}

// Build completion items for a grammar context from a schema column list
// (columns and/or sibling measure names — the caller decides the mix, see
// e.g. measurelab.js's exprPool()/modelform.js's exprColumns()) and,
// separately, a list of declared parameters ({name, values, default}) for
// the "param(" context — omit/pass [] where parameters don't apply.
export function dslItems(ctx, columns, after, parameters) {
  if (ctx.kind === "param") {
    // don't double the closer if a quote already follows the caret
    const quoted = after.startsWith('"') || after.startsWith("'");
    const closer = quoted ? "" : "')";
    const skip = quoted ? 0 : 2;  // hop over the existing closing quote+paren instead
    return (parameters || [])
      .filter((p) => p.name.toLowerCase().startsWith(ctx.prefix.toLowerCase()))
      .map((p) => ({
        text: p.name, hint: `${p.type || "int"} · values: ${p.values.join(", ")} (default ${p.default})`,
        insert: p.name + closer, caretOffset: skip,
      }));
  }
  const cols = (columns || [])
    .filter((c) => c.name.toLowerCase().startsWith(ctx.prefix.toLowerCase()));
  const fnList = parameters && parameters.length ? [...DSL_FUNCTIONS, PARAM_FN] : DSL_FUNCTIONS;
  // case-insensitively, since SQL is: typing `sum` should still offer SUM()
  const fns = fnList
    .filter(([t]) => t.toLowerCase().startsWith(ctx.prefix.toLowerCase()))
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
    update();          // e.g. SUM( immediately offers columns
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
