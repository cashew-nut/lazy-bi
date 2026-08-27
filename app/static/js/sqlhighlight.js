/* Cosmetic SQL syntax highlighting for the sandbox cell editor's read-only
   backdrop (see sandbox.js's refreshHighlight) — the SQL sibling of
   yamlhighlight.js, same shape: a line-based regex tokenizer, not a real
   parser, never a source of truth (DuckDB's own parser is still the only
   arbiter of valid SQL). Kept dependency-free like the rest of
   app/static/js. */
"use strict";

// Reserved words that shape a statement. Deliberately not exhaustive: this
// only has to make a cell readable at a glance, and a word it misses simply
// renders unstyled rather than wrong.
const KEYWORDS = new Set([
  "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "ATTACH", "BETWEEN", "BY",
  "CASE", "CAST", "COPY", "CREATE", "CROSS", "CUBE", "DELETE", "DESC",
  "DESCRIBE", "DETACH", "DISTINCT", "DROP", "ELSE", "END", "EXCEPT",
  "EXCLUDE", "EXISTS", "EXPLAIN", "FILTER", "FOLLOWING", "FROM", "FULL",
  "GROUP", "GROUPING", "HAVING", "IF", "ILIKE", "IN", "INNER", "INSERT",
  "INTERSECT", "INTERVAL", "INTO", "IS", "JOIN", "LATERAL", "LEFT", "LIKE",
  "LIMIT", "NATURAL", "NOT", "NULLS", "OFFSET", "ON", "OR", "ORDER", "OUTER",
  "OVER", "PARTITION", "PIVOT", "PRAGMA", "PRECEDING", "QUALIFY", "RANGE",
  "REPLACE", "RIGHT", "ROLLUP", "ROW", "ROWS", "SAMPLE", "SELECT", "SEMI",
  "SET", "SHOW", "SIMILAR", "SUMMARIZE", "TABLE", "TEMP", "TEMPORARY",
  "THEN", "TO", "UNBOUNDED", "UNION", "UNPIVOT", "UPDATE", "USING", "VALUES",
  "VIEW", "WHEN", "WHERE", "WINDOW", "WITH",
]);

// Highlighted the same way as python's builtins were: the vocabulary you
// reach for rather than the grammar you write it in. Readers first, since
// every sandbox cell starts with one.
const BUILTINS = new Set([
  "READ_PARQUET", "READ_CSV", "READ_CSV_AUTO", "READ_JSON", "READ_JSON_AUTO",
  "DELTA_SCAN", "ICEBERG_SCAN", "GLOB",
  "SUM", "COUNT", "AVG", "MIN", "MAX", "MEDIAN", "MODE", "STDDEV", "VARIANCE",
  "QUANTILE_CONT", "QUANTILE_DISC", "ARG_MAX", "ARG_MIN", "FIRST", "LAST",
  "LIST", "STRING_AGG", "ANY_VALUE", "APPROX_COUNT_DISTINCT",
  "LAG", "LEAD", "RANK", "DENSE_RANK", "ROW_NUMBER", "NTILE", "PERCENT_RANK",
  "COALESCE", "NULLIF", "GREATEST", "LEAST", "IFNULL",
  "DATE_TRUNC", "DATE_DIFF", "DATE_PART", "DATE_ADD", "STRFTIME", "STRPTIME",
  "EPOCH", "NOW", "TODAY", "AGE",
  "ABS", "ROUND", "CEIL", "FLOOR", "LN", "LOG", "EXP", "POWER", "SQRT",
  "LOWER", "UPPER", "TRIM", "LENGTH", "SUBSTR", "SUBSTRING", "CONCAT",
  "REGEXP_MATCHES", "REGEXP_EXTRACT", "REGEXP_REPLACE", "SPLIT_PART",
  "TRUE", "FALSE", "NULL",
]);

const IDENT_RE = /^[A-Za-z_][A-Za-z0-9_$]*/;

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Tokenize one line, tracking whether it starts inside a /* */ block comment
// continued from a previous line (returns the updated state so the caller
// can thread it to the next line).
function highlightLine(line, inBlock) {
  let out = "";
  let i = 0;
  const n = line.length;

  if (inBlock) {
    const closeIdx = line.indexOf("*/");
    if (closeIdx === -1) {
      return { html: `<span class="tok-comment">${escapeHtml(line)}</span>`, inBlock: true };
    }
    out += `<span class="tok-comment">${escapeHtml(line.slice(0, closeIdx + 2))}</span>`;
    i = closeIdx + 2;
    inBlock = false;
  }

  while (i < n) {
    const c = line[i];

    if (c === "-" && line[i + 1] === "-") {
      out += `<span class="tok-comment">${escapeHtml(line.slice(i))}</span>`;
      break;
    }

    if (c === "/" && line[i + 1] === "*") {
      const closeIdx = line.indexOf("*/", i + 2);
      if (closeIdx === -1) {
        out += `<span class="tok-comment">${escapeHtml(line.slice(i))}</span>`;
        return { html: out, inBlock: true };
      }
      out += `<span class="tok-comment">${escapeHtml(line.slice(i, closeIdx + 2))}</span>`;
      i = closeIdx + 2;
      continue;
    }

    // a single-quoted literal, where '' is an escaped quote rather than a
    // close immediately followed by an open
    if (c === "'") {
      let j = i + 1;
      while (j < n) {
        if (line[j] === "'") {
          if (line[j + 1] === "'") { j += 2; continue; }
          break;
        }
        j++;
      }
      j = Math.min(j + 1, n);
      out += `<span class="tok-string">${escapeHtml(line.slice(i, j))}</span>`;
      i = j;
      continue;
    }

    // a double-quoted *identifier* — a different thing from a string in SQL,
    // so it gets the neutral colour a bare identifier would have
    if (c === '"') {
      let j = i + 1;
      while (j < n && line[j] !== '"') j++;
      j = Math.min(j + 1, n);
      out += escapeHtml(line.slice(i, j));
      i = j;
      continue;
    }

    const rest = line.slice(i);
    const identMatch = rest.match(IDENT_RE);
    if (identMatch) {
      const word = identMatch[0];
      const upper = word.toUpperCase();
      if (KEYWORDS.has(upper)) out += `<span class="tok-key">${escapeHtml(word)}</span>`;
      else if (BUILTINS.has(upper)) out += `<span class="tok-bool">${escapeHtml(word)}</span>`;
      else out += escapeHtml(word);
      i += word.length;
      continue;
    }

    const numMatch = rest.match(/^\d+(\.\d+)?([eE][+-]?\d+)?/);
    if (numMatch) {
      out += `<span class="tok-number">${escapeHtml(numMatch[0])}</span>`;
      i += numMatch[0].length;
      continue;
    }

    out += escapeHtml(c);
    i++;
  }
  return { html: out, inBlock };
}

export function highlightSql(text) {
  let inBlock = false;
  const lines = text.split("\n").map((line) => {
    const res = highlightLine(line, inBlock);
    inBlock = res.inBlock;
    return res.html;
  });
  return lines.join("\n");
}
