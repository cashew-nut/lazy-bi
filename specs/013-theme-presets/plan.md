# Implementation Plan: Pre-Packed Theme Selector

**Branch**: `013-theme-presets` | **Date**: 2026-07-17 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/013-theme-presets/spec.md`

## Summary

Ship 4 pre-packed, non-editable visual themes (Cyberpunk — existing, unchanged — plus Daylight, Slate, and Contrast) that a user can switch between instantly from the existing ACCOUNT area. Technically this means finishing the componentization the codebase already started: `style.css` already drives UI chrome off 9 CSS custom properties, so the work is (a) extracting the ~30 remaining raw hex literals into theme-scoped tokens selected by a `data-theme` attribute, (b) making the hardcoded chart categorical palette (`charts/common.js`) swappable in place without touching its 6 call sites, and (c) adding a small local+account persistence layer (localStorage + one new column pair on `users`) with last-write-wins reconciliation, matching FR-006. No build step, no new frontend framework, no new backend service — additive changes to the existing FastAPI + vanilla-JS-SPA architecture.

## Technical Context

**Language/Version**: Python 3.12 (FastAPI backend), vanilla ES2020+ JS modules (frontend) — no TypeScript, no transpilation

**Primary Dependencies**: FastAPI, Polars (unaffected by this feature), Python stdlib `sqlite3`; frontend has zero dependencies — no bundler, no framework (constitution: "Technology Constraints — Frontend")

**Storage**: Existing SQLite file (`AuthStore` in `app/authstore.py`, same DB as `VisualStore`) — add two nullable columns to `users` (`theme`, `theme_updated_at`) via guarded `ALTER TABLE`, following the existing migration precedent in `app/store.py`/`app/conversationstore.py`. Browser `localStorage` holds the per-browser copy — no new storage technology.

**Testing**: pytest + FastAPI `TestClient` for the new `/api/users/me/theme` endpoint and the `AuthStore` migration/CRUD (constitution Principle III). No JS unit-test runner exists in this repo; palette/token correctness is verified with the existing `app/static/validate_palette.js` script (run manually per theme) plus browser verification (Principle IV) — called out explicitly rather than silently skipped.

**Target Platform**: Existing Docker image (`python:3.12-slim`, single uvicorn worker) serving to any evergreen browser — no new platform surface.

**Project Type**: Web application — single FastAPI backend serving a hand-rolled vanilla-JS SPA from `app/static/` (existing structure, not the template's generic multi-project layout).

**Performance Goals**: Theme switch is a local CSS custom-property + in-place array update — no network round-trip on the switch itself, satisfying SC-001 (<1s, no reload) trivially. Account sync (`PUT /api/users/me/theme`) is fire-and-forget against the existing SQLite single-writer, no new load profile.

**Constraints**: No build step/bundler (hard constitution constraint) — all 4 themes ship as plain CSS + a small JS theme module loaded natively. Must avoid a flash-of-wrong-theme on load, which requires resolving `data-theme` synchronously before first paint (a blocking inline `<script>` in `<head>`, not the deferred `type=module` bundle). Single SQLite writer — the new column write is a simple single-row `UPDATE`, no contention concerns beyond what already exists.

**Scale/Scope**: ~9 files touched (`style.css`, `charts/common.js`, `admin.js`, `line.js`, `scatter.js`, `dashboard.js`, `main.js`, `index.html`, `authstore.py`) + 1 new small JS module (`static/js/theme.js`) + 1 new API router addition (`app/api/users.py` or a new `app/api/theme.py`) + 4 theme token definitions + 4 validated chart palettes.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

- **I. Semantic Layer Is the Only Contract** — N/A. Purely presentational; no dimension/measure/model touched.
- **II. Lazy Evaluation, Pushdown by Default** — N/A. No query engine path touched.
- **III. Every Feature Ships With Tests** — Satisfied by plan: pytest coverage for the new theme-preference endpoint and the `AuthStore` migration (mirrors existing `test_auth.py`/`test_store.py` patterns). Flagged explicitly: there is no JS test runner in this repo, so palette-swap and token-application logic is verified via `validate_palette.js` + manual browser check, not an automated unit test — consistent with how existing chart code is verified today.
- **IV. Browser-Verified Before "Done"** — Plan requires: cycle through all 4 themes on Home/Studio/Modelling/Portal/Chat/Account/Login, confirm no console errors, confirm a chart with 8 series stays readable in each theme, confirm reload preserves the selection, confirm a second browser picks up an account-synced theme after login. Screenshots taken before/after per theme.
- **V. Ephemeral vs. Persisted State Is a Deliberate Choice** — Explicit and deliberate: the theme selection is **persisted**, not ephemeral, in both its local (localStorage) and account (SQLite) forms — this must survive reload by design (FR-004/FR-005), unlike cross-filtering/focus-mode state elsewhere in the app.
- **VI. Trusted-Config Security Boundary** — N/A. No measure DSL, no `eval`/`exec` path touched. The new endpoint only lets an authenticated user set their *own* theme string, validated against a fixed enum of 4 known ids server-side (not free text, not rendered as CSS/HTML) — no injection surface.
- **VII. Feature Branches, One Development Per Branch** — Satisfied; developed on `claude/app-theme-componentization-h17xo2`.

No violations requiring justification — Complexity Tracking table is not needed.

## Project Structure

### Documentation (this feature)

```text
specs/013-theme-presets/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   └── theme-preference-api.md
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
app/
├── authstore.py                # + theme, theme_updated_at columns on `users`
│                                #   (guarded ALTER TABLE, same pattern as
│                                #   the existing user_id/llm_model migrations)
├── api/
│   └── users.py                # + GET/PUT /api/users/me/theme
│                                #   (or a new app/api/theme.py if that reads
│                                #   cleaner alongside the existing router-per-
│                                #   resource convention — decided in tasks)
└── static/
    ├── index.html               # + blocking inline bootstrap <script> in
    │                             #   <head> that sets data-theme before
    │                             #   style.css paints; favicon becomes
    │                             #   theme-aware (or a single neutral icon)
    ├── style.css                 # existing :root block becomes the
    │                             #   "cyberpunk" (default) token set;
    │                             #   3 new `[data-theme="..."]` blocks added;
    │                             #   remaining raw hex literals promoted to
    │                             #   tokens (status colors, duplicated
    │                             #   gradients); decorative scanline/glow
    │                             #   overlay gated per-theme
    ├── validate_palette.js       # reused unmodified, run once per theme's
    │                             #   --bg during design of the 3 new palettes
    └── js/
        ├── theme.js               # NEW — owns current-theme state: applies
        │                           #   data-theme, mutates PALETTE/OTHER_COLOR
        │                           #   in place, persists to localStorage,
        │                           #   syncs with the account endpoint,
        │                           #   does last-write-wins reconciliation
        ├── main.js                 # wires theme.js init + theme picker
        │                           #   control into the ACCOUNT view; the
        │                           #   ~handful of inline hex literals here
        │                           #   move to CSS tokens
        └── charts/
            └── common.js            # PALETTE/OTHER_COLOR become theme-owned
                                      #   mutable arrays (contents replaced by
                                      #   theme.js, not the binding) — geo.js,
                                      #   line.js, pivot.js, sankey.js,
                                      #   scatter.js, dashboard.js need no
                                      #   import-contract changes since they
                                      #   already index PALETTE at call time

tests/
└── test_auth.py or a new test_theme.py   # theme-preference endpoint +
                                            # AuthStore migration coverage
```

**Structure Decision**: This is the existing single web application (FastAPI backend + hand-rolled vanilla-JS SPA under `app/static/`) — no new project, no frontend/backend split beyond what already exists. All changes are additive to existing files plus one new small frontend module (`theme.js`) and one small backend addition (a theme-preference endpoint). The key structural finding from research (below) is that the chart palette can be re-themed by **mutating the existing exported array in place**, which keeps this feature from needing to touch the import contracts of 6 chart-rendering files — the biggest scope-reduction decision in this plan.

## Complexity Tracking

*Not applicable — no constitution violations.*
