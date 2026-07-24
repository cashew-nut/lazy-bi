/* Cosmetic python syntax highlighting for the sandbox cell editor's
   read-only backdrop (see sandbox.js's refreshCellHighlight) — the python
   sibling of yamlhighlight.js, same shape: a line-based regex tokenizer, not
   a real parser, never a source of truth (the runner's actual exec/eval is
   still the only real arbiter of valid code). Kept dependency-free like the
   rest of app/static/js. */
"use strict";

const KEYWORDS = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await", "break",
  "class", "continue", "def", "del", "elif", "else", "except", "finally",
  "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
  "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
]);
const BUILTINS = new Set([
  "print", "len", "range", "list", "dict", "set", "tuple", "str", "int",
  "float", "bool", "sum", "min", "max", "sorted", "enumerate", "zip", "map",
  "filter", "read", "pl",
]);
const NUM_RE = /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/;
const IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]*/;

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Tokenize one line, tracking whether it starts inside a triple-quoted
// string continued from a previous line (returns the updated state so the
// caller can thread it to the next line).
function highlightLine(line, inTriple) {
  let out = "";
  let i = 0;
  const n = line.length;

  if (inTriple) {
    const closeIdx = line.indexOf(inTriple);
    if (closeIdx === -1) {
      return { html: `<span class="tok-string">${escapeHtml(line)}</span>`, inTriple };
    }
    out += `<span class="tok-string">${escapeHtml(line.slice(0, closeIdx + 3))}</span>`;
    i = closeIdx + 3;
    inTriple = null;
  }

  while (i < n) {
    const c = line[i];

    if (c === "#") {
      out += `<span class="tok-comment">${escapeHtml(line.slice(i))}</span>`;
      break;
    }

    if (c === '"' || c === "'") {
      const triple = line.slice(i, i + 3) === c + c + c;
      if (triple) {
        const closeIdx = line.indexOf(c + c + c, i + 3);
        if (closeIdx === -1) {
          out += `<span class="tok-string">${escapeHtml(line.slice(i))}</span>`;
          return { html: out, inTriple: c + c + c };
        }
        out += `<span class="tok-string">${escapeHtml(line.slice(i, closeIdx + 3))}</span>`;
        i = closeIdx + 3;
        continue;
      }
      let j = i + 1;
      while (j < n && !(line[j] === c && line[j - 1] !== "\\")) j++;
      j = Math.min(j + 1, n);
      out += `<span class="tok-string">${escapeHtml(line.slice(i, j))}</span>`;
      i = j;
      continue;
    }

    const rest = line.slice(i);
    const identMatch = rest.match(IDENT_RE);
    if (identMatch) {
      const word = identMatch[0];
      if (KEYWORDS.has(word)) out += `<span class="tok-key">${escapeHtml(word)}</span>`;
      else if (BUILTINS.has(word)) out += `<span class="tok-bool">${escapeHtml(word)}</span>`;
      else out += escapeHtml(word);
      i += word.length;
      continue;
    }

    const numMatch = rest.match(/^\d[\d_]*(\.\d+)?([eE][+-]?\d+)?/);
    if (numMatch && NUM_RE.test(numMatch[0].replace(/_/g, ""))) {
      out += `<span class="tok-number">${escapeHtml(numMatch[0])}</span>`;
      i += numMatch[0].length;
      continue;
    }

    out += escapeHtml(c);
    i++;
  }
  return { html: out, inTriple };
}

export function highlightPython(text) {
  let inTriple = null;
  const lines = text.split("\n").map((line) => {
    const res = highlightLine(line, inTriple);
    inTriple = res.inTriple;
    return res.html;
  });
  return lines.join("\n");
}
