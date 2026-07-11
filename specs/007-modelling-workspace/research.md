# Phase 0 Research: Modelling Workspace

All four clarifications were resolved in `/speckit-clarify` (see spec "Clarifications" section), so there are no open `NEEDS CLARIFICATION` markers. This document records the design decisions that shape Phase 1, grounded in the existing codebase.

## Decision 1 — Dataset picker source: group the Explorer's object walk

**Decision**: Add a read-only `GET /api/datasets` that reuses the same `list_objects_v2` bucket walk `app/api/explorer.py` already performs, and groups objects into **datasets** by their directory prefix (everything up to the last `/`). Each group yields a **glob source** (`s3://<bucket>/<prefix>/*.<ext>`) plus the list of individual objects for drill-down, an inferred `format`, an object count, and which models already read it.

**Rationale**: The clarification chose "grouped, drillable." Objects sharing a prefix are almost always one partitioned dataset (the seed data and existing models already source `.../*.parquet`). Grouping by prefix + inferring a `*.<ext>` glob matches how `Source.path` is written today (`semantic.Source.path` examples: `s3://.../prefix/*.parquet`). The Explorer already proves the bucket walk is cheap and correct.

**Alternatives considered**:
- *Per-object only* — rejected by the clarification (noisy, doesn't match multi-file datasets).
- *A persisted catalog* — rejected: violates "no new persistence store"; the bucket is the catalog.
- *Server-side scan to confirm schema at list time* — rejected: unnecessary work; schema is fetched lazily only once a source is chosen, via the existing validate endpoint.

**Format inference**: extension → format map (`.parquet`→parquet, `.csv`→csv, Delta detected by a `_delta_log/` sibling under the prefix → delta root). Mixed-extension prefixes fall back to the dominant extension and are flagged so the picker can warn. This is a small pure helper in `semantic.py` (`infer_format`, `group_objects`) — unit-testable without a bucket.

## Decision 2 — Editor stays a single YAML document; affordances patch text

**Decision**: Keep the existing single `#yaml-editor` textarea as the source of truth. The dataset picker, import panel, and intellisense all act by inserting/patching YAML text (extending today's `insertAtCursor`). No structured form model, no second document.

**Rationale**: The clarification chose "augment the text editor." `editor.js` already works this way (`insertAtCursor`, `renderColChips`, `renderImportPanel`). This guarantees FR-017 (guided and textual views cannot diverge) for free and honors the assumption that a full form-builder is out of scope. Lowest risk, smallest diff, fully aligned with the no-build-step constraint.

**Source-block patching**: picking a dataset must set the `source:` block. Because the doc is text, the safe operation is: if a top-level `source:` block exists, replace its `path:`/`format:` lines; otherwise insert a `source:` block after the `name:`/`label:`/`description:` header. Implemented as a small, well-tested text transform mirroring the spirit of `semantic.append_measure_yaml` (which already does comment-preserving block insertion). If the document is mid-edit and has no parseable header, the picker falls back to inserting a `source:` block at the cursor and surfaces a note (FR edge case: guided edit vs hand-edited YAML must fail safe, never corrupt).

**Alternatives considered**: two-pane form+YAML and new-vs-edit wizard — both rejected in clarification for added surface and sync-bug risk.

## Decision 3 — Intellisense is context-aware over the YAML, reusing the Measure Lab engine

**Decision**: Extract the completion engine currently embedded in `measurelab.js` (the `suggestContext`/`updateSuggest`/`applySuggest`/`renderSuggest` machinery and the `TOP_FNS`/`METHODS` vocabulary) into a shared module, and drive it from the YAML editor with a **YAML-context classifier** that decides what to offer based on where the caret sits:

- **Inside an `expr:` value** (a measure expression) → full polars completion: `pl.` top-level fns, `.` methods, `pl.col("` → real source columns. Identical behavior to the lab.
- **In a column-name context** — the value of `name:`/`column:` under `dimensions:`, `on:`/`left_on:`/`right_on:` under `joins:`/`dimension_imports:`, `start:`/`end:` (spine), `lat:`/`lon:` (geo) → **bare column-name** completion (no `pl.` wrapper), because the semantic layer stores these as raw column strings (`semantic.Dimension.column`, `Join.left_on`, etc.), not expressions.
- **Elsewhere** → no suggestions.

**Rationale**: The clarification chose "anywhere in the YAML," but the semantic model only has *one* expression field (`measures[].expr`); dimensions and join keys are plain column names (confirmed in `semantic.py`: `Dimension` has `column`, not an expr; only `Measure` has `expr_source`). So "anywhere" correctly means "column completion in every column context, and full polars completion in expression context" — which is strictly more helpful than expression-only. Reusing the lab engine avoids a second completion implementation and keeps one vocabulary to maintain (DRY, and no behavior drift between the lab and the editor).

**Columns source**: the already-existing `POST /api/models/validate` returns `columns` (name+dtype) whenever the draft's source is reachable; the editor already calls it on every keystroke (`scheduleValidate`). Completion reads those columns from the latest validate response — no extra round-trip per keystroke.

**Method vocabulary endpoint**: to keep the vocabulary in one place and testable, expose the method/function list as a tiny `GET /api/completion/methods` (static list mirroring `measurelab.js`'s `TOP_FNS`/`METHODS`). Frontend may also keep the list inline (it is small and static); the endpoint exists so a test can assert the lab and editor share one vocabulary. **This is the one genuinely optional piece** — see contracts/completion.md; if we keep the list purely client-side, drop the endpoint and assert shared-module usage instead.

**Live validity**: measure "does it resolve" feedback already exists two ways — `/api/models/validate` fails the whole parse if any measure expr is invalid (`Measure.expr()` is called at parse in `_parse_model`), and the editor shows that error. No new resolve endpoint needed; the editor's existing validation report is the live feedback surface (FR-015).

## Decision 4 — Unsaved-edit guard is session-only ephemeral state

**Decision**: Track a `dirty` flag in frontend state (set on editor input, cleared on save/revert/open). Intercept the three ways to leave the editor — mode-nav click, opening a different model/bundle, and `beforeunload` — and `confirm()` before discarding when `dirty`. Nothing is written to disk unless the user saves.

**Rationale**: Clarification chose "warn before leaving." Constitution V demands an explicit ephemeral/persisted decision; this is squarely ephemeral (like cross-filter and grain override). A page reload must lose unsaved edits (proving they were never persisted) while saved edits survive — both are quickstart checks. Reuses the existing `showView` chokepoint in `state.js` so there is a single place to guard navigation.

**Alternatives considered**: silent discard (loses work) and retain-in-memory (stale-draft confusion) — both rejected in clarification.

## Decision 5 — Information architecture: rename in place, absorb the Explorer

**Decision**: Rename the `data` mode to `modelling` across `index.html` (nav button label + `data-mode`), `state.js` (`showView` mode mapping and the `view` vocabulary), and `main.js` (nav wiring). The Modelling view hosts: the existing data-overview (Explorer's datasets↔models table, moved from `explorer.js` into the workspace), the model list with edit/new entry points, and the common-model list with edit/new entry points. The three Studio-sidebar buttons (`#edit-model`, `#new-model`, `#new-bundle`) and their sections are removed from Studio and re-homed in Modelling.

**Rationale**: FR-001/002/004/005. Minimal churn: `editor.js`/`dimlab.js` already implement the editing behavior and are view-agnostic; only their *entry points* move. The Explorer view (`#explorer-view`) becomes a panel within Modelling rather than a separate mode. Studio's `#model-select` stays (you still choose which model to build against) but loses the authoring buttons.

**Backward compatibility**: no persisted state references the mode name; `showView`'s `document.body.dataset.mode` is cosmetic/CSS. Renaming is safe. Keep a note in the README update.

## Decision 6 — Testing approach

**Decision**:
- `tests/test_datasets.py` (or extend `test_semantic.py`): unit-test `group_objects`/`infer_format` on synthetic key lists (prefix grouping, extension inference, delta-root detection, mixed-extension flagging) — no bucket needed.
- `tests/test_api.py`: TestClient tests for `GET /api/datasets` against the `seeded` moto bucket (asserts known seed prefixes appear, formats correct, model-mapping present), and the completion-methods endpoint if kept. Round-trip: create a model whose `source` matches a listed dataset via `POST /api/models`, confirm it loads and its columns are introspectable via `/validate`.
- `tests/test_static.py`: assert the DATA→MODELLING rename and that the three authoring buttons are no longer in the Studio sidebar markup.
- Browser verification per quickstart.md for the interactive affordances (picker insert, intellisense dropdown, warn-before-leaving).

**Rationale**: Constitution III (tests alongside) + IV (browser-verified). Pure helpers get fast unit tests; the endpoint gets integration coverage against the real emulated bucket; interactive UI gets browser proof.

## Open questions

None. All clarifications resolved; all decisions grounded in existing code. Proceed to Phase 1.
