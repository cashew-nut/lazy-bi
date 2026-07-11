# Contract: Expression completion & live validation

Intellisense in the model editor is powered by **existing** endpoints plus one small optional vocabulary endpoint. No new resolve/eval endpoint is introduced (Constitution VI — the trusted-config eval boundary is not widened).

## Column suggestions & live validity — reuse `POST /api/models/validate` (existing)

Already implemented in `app/api/models.py`. The editor calls it on every (debounced) keystroke via `scheduleValidate`.

Request:
```json
{ "yaml": "<current editor text>" }
```

Response (relevant fields):
```jsonc
{
  "ok": true,
  "model": { "name": "...", "label": "...", "dimensions": 3, "measures": 2 },
  "columns": [ { "name": "region", "dtype": "String" } ],   // null if source unreachable
  "schema_error": "source not reachable: ..."               // present only on introspection failure
}
```

- **Column completion** reads `columns` from the latest response — no extra round-trip.
- **Live validity (FR-015)**: when `ok` is `false`, `error` carries the parse/eval failure (an invalid `measures[].expr` fails the whole parse because `Measure.expr()` is evaluated in `_parse_model`). The editor's existing validation report is the live feedback surface; **save is blocked / warned when `ok` is false** — an invalid expression cannot be silently written.
- The bundle equivalent, `POST /api/dimensions/validate`, returns per-dataset `columns` the same way for common-model authoring.

No change to these endpoints is required. This contract documents the dependency and the save-guard behavior.

## Method / function vocabulary — `GET /api/completion/methods` (new, OPTIONAL)

A tiny static list so the Measure Lab and the model editor share **one** vocabulary and a test can assert they do not drift. If we instead keep the list in a shared frontend module and assert shared usage, this endpoint is dropped — see research.md Decision 3.

Response `200`:
```jsonc
{
  "top": [
    { "insert": "col(\"\")", "hint": "reference a column", "caret_offset": -2 },
    { "insert": "len()", "hint": "row count", "caret_offset": 0 }
  ],
  "methods": [
    { "insert": "sum()", "hint": "total", "caret_offset": 0 },
    { "insert": "mean()", "hint": "average", "caret_offset": 0 }
  ]
}
```

Mirrors the `TOP_FNS` / `METHODS` arrays currently in `app/static/js/measurelab.js`. Pure/static; testable with a plain assertion.

## Context classification (frontend, no endpoint)

The editor decides *what* to offer from caret position in the YAML (research.md Decision 3):

| Caret context | Completion offered |
|---------------|--------------------|
| inside a `expr:` value (measure) | polars: `pl.` fns, `.` methods, `pl.col("` → columns |
| value of `name:`/`column:` (dimensions), `on:`/`left_on:`/`right_on:` (joins & imports), spine `start:`/`end:`, geo `lat:`/`lon:` | bare column names (no `pl.` wrapper) |
| anywhere else | none |

This reuses the extracted lab completion engine (`suggestContext`/`updateSuggest`/`applySuggest`), parameterized by the offered item set. Verified in-browser (quickstart.md).
