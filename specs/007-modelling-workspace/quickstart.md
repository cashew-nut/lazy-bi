# Quickstart & Validation: Modelling Workspace

End-to-end validation for the feature. Covers the automated checks (pytest) and the browser-driven golden paths required by Constitution IV. Do not treat "tests pass" as done — the interactive affordances must be driven in a real browser with a zero-console-errors check.

## Prerequisites

- Repo on branch `feature/modelling-workspace`.
- Python env per project (local dev may need `uv`/3.11+ per project memory; target is 3.12).
- No external S3 needed — the embedded moto emulator + seed bucket run by default.

## Run the app

```bash
./run.sh          # or: uvicorn app.main:app --reload
```

Open the served UI (default `http://127.0.0.1:8000`). Use the in-app browser preview for verification (never ask the user to check manually).

## Automated checks

```bash
pytest -q                       # full suite must stay green
pytest -q tests/test_datasets.py tests/test_api.py::*dataset*   # new grouping + endpoint
pytest -q tests/test_static.py  # IA move/rename smoke checks
```

Expected new coverage:
- `group_objects`/`infer_format` unit tests (prefix grouping, parquet/csv/delta inference, mixed-extension flag, empty input).
- `GET /api/datasets` against the seeded bucket returns known prefixes with correct formats and model mappings (contracts/datasets.md).
- Static smoke: Studio sidebar no longer contains the three authoring buttons; nav shows MODELLING not DATA.

## Browser golden paths (Constitution IV)

### GP1 — IA move (US1 / FR-001, FR-002, FR-004, FR-005)
1. Load app on Studio. **Confirm** the Studio sidebar has no "edit yaml", "+ new model", or "+ new common model".
2. Click **MODELLING** in the nav. **Confirm** it opens with: the datasets↔models overview, the model list, the common-model list, and create/edit entry points for both.
3. **Confirm** Studio still lets you pick a model and build/save a visual (regression).

### GP2 — Dataset picker (US2 / FR-006–FR-009)
1. In Modelling, start **new model**. Open the dataset picker.
2. **Confirm** grouped datasets appear with path/format/object-count and any model-reads badge; expand one group and **confirm** individual objects are listed.
3. Pick a dataset. **Confirm** the editor's YAML `source:` block is populated (path + format) with no manual typing.
4. **Confirm** the source's real columns appear as insertable chips (validate round-trip). Point at an unreachable path and **confirm** a non-blocking "source not reachable" note (edit still possible).

### GP3 — Guided common-model import (US3 / FR-010–FR-012)
1. Ensure a common model exists (e.g. `geography`). Edit a fact model.
2. Use the import affordance: choose common model → dataset → matching key column. **Confirm** a `dimension_imports:` block is inserted and validation passes; the imported dimensions become usable.
3. Temporarily remove all common models and **confirm** the affordance shows a guiding empty state.

### GP4 — Expression intellisense (US4 / FR-013–FR-015)
1. In a measure `expr:` value, type `pl.col("` → **confirm** real column suggestions; type `.` after a column → **confirm** method suggestions; accept one → **confirm** caret lands sensibly (e.g. inside `()`).
2. In a dimension `column:`/`name:` value, type a prefix → **confirm** bare column-name suggestions (no `pl.` wrapper).
3. Write an invalid expression → **confirm** the editor reports invalid and **save is blocked/warned**; fix it → **confirm** it reports valid.

### GP5 — Raw YAML parity (US5 / FR-016–FR-018)
1. Open a model's raw YAML, make a valid textual edit, save → **confirm** persist + hot-reload as before.
2. Make an invalid edit → **confirm** reported and not silently saved.
3. After a guided action (picker/import/suggestion), **confirm** the change is visible and further-editable in the same YAML text (single source of truth).

### GP6 — Unsaved-edit guard + persistence (FR-021 / Constitution V)
1. Edit YAML without saving, then click another mode / open a different model → **confirm** a confirm() warns before discarding.
2. Save a change, then **cold-reload the page** → **confirm** the saved change is present (persisted).
3. Make an unsaved edit, **reload the page** → **confirm** the edit is gone (never persisted).

### Final
- **Zero console errors** across GP1–GP6.
- Screenshots of: Studio sidebar (no authoring buttons), Modelling workspace, dataset picker inserting a source, intellisense dropdown, warn-before-leaving dialog.

## Success mapping

| Golden path | Success criteria |
|-------------|------------------|
| GP2 | SC-001 (create a model without typing a path), SC-007 (<2 min) |
| GP1 | SC-002 (zero authoring controls in Studio) |
| GP3 | SC-003 (guided import) |
| GP4 | SC-004 (columns+methods offered; invalid flagged before save) |
| GP5 | SC-005 (guided changes representable in YAML; raw-edit parity) |
| GP6 | SC-006 (saved survives reload; drafts never persisted) |

## README

Update `README.md` as part of implementation (Constitution Development Workflow): document the Modelling workspace, the dataset picker, guided import, and editor intellisense; note DATA→MODELLING.
