/* Cosmetic yaml (+ embedded pipeline-script) syntax highlighting for the
   yaml editor's read-only backdrop (see editor.js's refreshHighlight). A
   line-based regex tokenizer, not a real parser — good enough to color
   comments/strings/numbers/booleans/keys, never a source of truth (the
   server round trip in validateEditor is still the only real arbiter of a
   valid document). Kept dependency-free like the rest of app/static/js. */
"use strict";

const KEY_RE = /^(\s*(?:-\s*)*)([A-Za-z_][A-Za-z0-9_]*)(:)(?=\s|$)/;
const BOOL_NULL_RE = /^(true|false|null|yes|no|~)$/i;
const NUM_RE = /^-?\d+(\.\d+)?$/;

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Split a line into "code" and a trailing "# comment" — a '#' only starts a
// comment outside a quoted string and at the start of a token (start of
// line or preceded by whitespace), matching both yaml and python conventions.
function splitComment(line) {
  let inQuote = null;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQuote) {
      if (c === inQuote && line[i - 1] !== "\\") inQuote = null;
      continue;
    }
    if (c === '"' || c === "'") { inQuote = c; continue; }
    if (c === "#" && (i === 0 || /\s/.test(line[i - 1]))) {
      return { code: line.slice(0, i), comment: line.slice(i) };
    }
  }
  return { code: line, comment: "" };
}

// Highlight quoted strings, numbers, and booleans/null in a comment-free
// chunk of a line (a mapping key, if any, has already been stripped off).
function highlightValue(text) {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < text.length && !(text[j] === c && text[j - 1] !== "\\")) j++;
      j = Math.min(j + 1, text.length);
      out += `<span class="tok-string">${escapeHtml(text.slice(i, j))}</span>`;
      i = j;
      continue;
    }
    const m = text.slice(i).match(/^[^\s"'#]+/);
    if (m) {
      const word = m[0];
      if (BOOL_NULL_RE.test(word)) out += `<span class="tok-bool">${escapeHtml(word)}</span>`;
      else if (NUM_RE.test(word)) out += `<span class="tok-number">${escapeHtml(word)}</span>`;
      else out += escapeHtml(word);
      i += word.length;
      continue;
    }
    out += escapeHtml(c);   // a run of whitespace
    i++;
  }
  return out;
}

function highlightLine(line) {
  const { code, comment } = splitComment(line);
  let rest = code;
  let out = "";
  const km = rest.match(KEY_RE);
  if (km) {
    out += escapeHtml(km[1]) + `<span class="tok-key">${escapeHtml(km[2])}</span>:`;
    rest = rest.slice(km[0].length);
  }
  out += highlightValue(rest);
  if (comment) out += `<span class="tok-comment">${escapeHtml(comment)}</span>`;
  return out;
}

export function highlightYaml(text) {
  return text.split("\n").map(highlightLine).join("\n");
}
