---

description: "Task list for feature implementation"
---

# Tasks: Pre-Packed Theme Selector

**Input**: Design documents from `/specs/013-theme-presets/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/theme-preference-api.md, quickstart.md

**Tests**: Backend (SQLite migration + `/api/users/me/theme`) gets pytest coverage per Constitution Principle III. The frontend has no unit-test runner in this repo (vanilla JS, no build step) — its "tests" are the `validate_palette.js` script plus the browser-verification tasks below, matching how existing chart code in this codebase is already verified (Constitution Principle IV), not a gap.

**Organization**: Tasks are grouped by user story (spec.md P1/P2/P3) to enable independent implementation and testing of each.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- File paths are exact, relative to repo root

## Path Conventions

Existing single web app — FastAPI backend under `app/`, hand-rolled vanilla-JS SPA under `app/static/`. No `frontend/`/`backend/` split; no build step.

---

## Phase 1: Setup (Componentization Groundwork)

**Purpose**: Finish centralizing the *existing* cyberpunk look into tokens before any second theme is introduced — this is FR-009, and it must land with zero visual change (SC-005).

- [ ] T001 Extract the remaining ~20 raw hex literals in `app/static/style.css` into new custom properties on the existing `:root` block (e.g. `--ok: #2bd97c`, `--warn: #e8b339`) and replace the duplicated `--bg`/`--panel-2` gradient literals (lines ~51, 162, 430, 891) with `var(--bg)`/`var(--panel-2)` references. Zero visual change expected.
- [ ] T002 [P] Replace inline hex color literals in `app/static/js/admin.js`, `app/static/js/main.js`, `app/static/js/charts/line.js`, `app/static/js/charts/scatter.js`, `app/static/js/charts/dashboard.js` with references to the centralized tokens/constants from T001 where the value is UI chrome (not chart-series data — those are handled in Phase 5).
- [ ] T003 In `app/static/style.css`, gate the cyberpunk-only decorative scanline/glow overlay (`body::before`, `body::after`) behind a selector (e.g. `[data-theme="cyberpunk"] body::before`) so it can be disabled for the other 3 themes without deleting the rules. (same file as T001 — run after it)

**Checkpoint**: `style.css` still renders identically to today, but every color is now a token — ready for additional `[data-theme="..."]` blocks.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The shared theme-switching engine every user story depends on — none of the 3 stories can be independently demoed until this phase is done.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [ ] T004 [P] Create `app/static/js/theme.js` defining the theme catalog: the 4 ids (`cyberpunk`, `daylight`, `slate`, `contrast`), their labels, and each theme's `decorativeEffects` flag, per `data-model.md`.
- [ ] T005 [P] Author CSS token blocks for the 3 new themes in `app/static/style.css`: `[data-theme="daylight"]`, `[data-theme="slate"]`, `[data-theme="contrast"]`, each overriding all tokens from T001 per the intent in `research.md` §7 (light/clean, muted-dark/no-glow, high-contrast/accessibility respectively).
- [ ] T006 Author each of the 3 new themes' `PALETTE`/`OTHER_COLOR` (8 colors + 1) in `app/static/js/theme.js`, and validate all 4 themes (including the existing cyberpunk one) by running `app/static/validate_palette.js` against each theme's `--bg` — every theme must pass before proceeding (FR-008). Depends on: T004, T005.
- [ ] T007 Implement `theme.js`'s core `applyTheme(id)`: sets `document.documentElement.dataset.theme = id` and mutates `app/static/js/charts/common.js`'s exported `PALETTE`/`OTHER_COLOR` arrays **in place** (`.splice(0, PALETTE.length, ...)`) using that theme's validated palette from T006 — no changes needed to any chart-rendering file per `research.md` §2. Depends on: T006.
- [ ] T008 Add a blocking, non-module inline `<script>` in the `<head>` of `app/static/index.html`, before `style.css`'s effect would otherwise be the only thing painted, that synchronously reads a resolved theme id from `localStorage` (default `cyberpunk` if absent/invalid) and sets `data-theme` on `<html>` before first paint, per `research.md` §3. Depends on: T007.

**Checkpoint**: The theme-switching engine exists and can be invoked programmatically (`applyTheme('daylight')` from a console re-skins the app); nothing is wired to a UI or to persistence-on-selection yet.

---

## Phase 3: User Story 1 - Switch the app's look in one action (Priority: P1) 🎯 MVP

**Goal**: A user can pick any of the 4 themes from within the app and see it applied instantly everywhere, and it's remembered on that browser across reloads.

**Independent Test**: Load the app, open the theme picker, select each of the 4 themes in turn, confirm every screen updates instantly and consistently; reload the browser and confirm the last-chosen theme is still active.

### Implementation for User Story 1

- [ ] T009 [US1] Add a theme picker control to the ACCOUNT view in `app/static/index.html` (and its wiring in `app/static/js/main.js`) listing all 4 themes by label, indicating which is currently active.
- [ ] T010 [US1] Wire the picker's selection handler in `app/static/js/main.js` to call `theme.js`'s `applyTheme(id)` and then persist `{theme: id, updatedAt: <ISO timestamp>}` to `localStorage`. Depends on: T009.
- [ ] T011 [US1] On app boot, initialize `theme.js`'s in-memory current-theme state from the same `localStorage` value the inline bootstrap script (T008) already applied, so the two stay in sync rather than re-deriving independently. Depends on: T008, T010.
- [ ] T012 [US1] Make the app favicon in `app/static/index.html` theme-neutral (or swap it alongside `data-theme`) instead of hardcoded cyberpunk-cyan. Depends on: T007.
- [ ] T013 [US1] In `app/static/js/theme.js`, fall back to the default theme without throwing when `localStorage` is unavailable (e.g. blocked in private browsing) — wrap reads/writes so a `SecurityError`/exception never surfaces to the user. Depends on: T007.
- [ ] T014 [US1] Browser-verify per `quickstart.md` steps 1-3, 6, 7: cycle all 4 themes across Home/Studio/Modelling/Portal/Chat/Account/Login, confirm <1s re-skin with zero console errors, confirm reload persistence with no flash of the wrong theme, confirm a brand-new user defaults to cyberpunk, confirm the storage-blocked fallback. Depends on: T009, T010, T011, T012, T013.

**Checkpoint**: User Story 1 is fully functional and independently demoable — this is the MVP.

---

## Phase 4: User Story 2 - Theme choice follows a logged-in user across devices (Priority: P2)

**Goal**: A logged-in user's theme selection is also stored on their account and applied automatically when they log in elsewhere, reconciling with whichever browser made the most recent choice.

**Independent Test**: Select a theme while logged in on one browser, then log in as the same user from a second, unrelated browser (or private window) and confirm the same theme applies there.

### Tests for User Story 2

- [ ] T015 [P] [US2] pytest coverage in `tests/test_theme.py` for the guarded `ALTER TABLE` migration (starts from a `users` table without `theme`/`theme_updated_at`, confirms `AuthStore.__init__` adds both columns in place without data loss) and for `GET`/`PUT /api/users/me/theme` (200 round-trip, 422 on an unknown theme id, 401 when unauthenticated). Write first; expect it to fail until T016-T018 land.

### Implementation for User Story 2

- [ ] T016 [P] [US2] Add the guarded migration (`theme TEXT`, `theme_updated_at TEXT`) to the `users` table in `app/authstore.py`, following the existing `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` pattern already used for `measure_provenance.user_id`/`conversations.llm_model`.
- [ ] T017 [US2] Add `AuthStore.get_theme(user_id)` / `AuthStore.set_theme(user_id, theme)` in `app/authstore.py`, where `set_theme` stamps `theme_updated_at` from the server clock. Depends on: T016.
- [ ] T018 [US2] Implement `GET /api/users/me/theme` and `PUT /api/users/me/theme` in `app/api/users.py` per `contracts/theme-preference-api.md`, validating the request `theme` against the 4 known ids (422 otherwise). Depends on: T017.
- [ ] T019 [US2] Implement client-side reconciliation in `app/static/js/theme.js`: on authenticated app boot, `GET /api/users/me/theme`, compare its `updated_at` to the local `localStorage` copy's `updatedAt`, apply whichever is newer via `applyTheme`, and write the winner back to whichever side lost (`PUT` if the server was stale, `localStorage.setItem` if the client was stale). Depends on: T011, T018.
- [ ] T020 [US2] Call the reconciliation routine from T019 at successful login and on boot for an already-authenticated session, in `app/static/js/main.js`. Depends on: T019.
- [ ] T021 [US2] Browser-verify per `quickstart.md` steps 4-5: a theme chosen in one browser is applied automatically after logging in from a second browser; when two browsers pick different themes, the most recently chosen one wins after reload. Depends on: T020.

**Checkpoint**: User Stories 1 AND 2 both work independently.

---

## Phase 5: User Story 3 - Charts and data stay readable in every theme (Priority: P3)

**Goal**: Every one of the 4 themes keeps chart series distinguishable and text legible — the accessibility bar the current cyberpunk theme already meets holds for all 4, not just the default.

**Independent Test**: For each of the 4 themes, open a dashboard with a multi-series chart and confirm every series remains distinguishable and all text is legible.

### Implementation for User Story 3

- [ ] T022 [P] [US3] Extract the geo chart's hardcoded land fill/stroke (`.chart-box .geo-land` in `app/static/style.css`, currently `fill: #101a2c; stroke: #1c2940;`) into theme-aware tokens so the map stays legible against every theme's `--bg` (a light `--bg` under Daylight needs a different land fill than the dark themes).
- [ ] T023 [P] [US3] Audit `app/static/js/charts/common.js` and its callers (`sankey.js`, `pivot.js`, `geo.js`) for any remaining hardcoded chart-adjacent colors that bypass `PALETTE`/`OTHER_COLOR`, and route them through the theme-owned values from T007 instead.
- [ ] T024 [US3] Re-run `app/static/validate_palette.js` against all 4 themes' final `PALETTE`/`OTHER_COLOR` (after T022/T023's changes) and confirm every theme still passes — not just cyberpunk. Depends on: T006, T022, T023.
- [ ] T025 [US3] Browser-verify per `quickstart.md` step 3's readability check and the User Story 3 acceptance scenarios: an 8+ series chart stays distinguishable, and general UI text meets the same contrast bar, in each of the 4 themes. Depends on: T024.

**Checkpoint**: All 3 user stories are independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T026 [P] Update `README.md` to document the theme feature: the 4 available themes, where to switch (ACCOUNT view), and the local+account persistence behavior — per the constitution's "update README as part of the feature" rule.
- [ ] T027 [P] Visual regression check: confirm the cyberpunk theme is unchanged from pre-feature behavior for a user who never opens the theme picker (SC-005), spot-checking Home/Studio/Portal.
- [ ] T028 Run the full `quickstart.md` validation end-to-end plus the full `pytest tests/` suite; fix any regressions found before calling the feature done.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup (T001-T003) completing — the new theme token blocks (T005) build on T001's token extraction. BLOCKS all user stories.
- **User Stories (Phase 3-5)**: All depend on Foundational (Phase 2) completing.
  - US1 has no dependency on US2 or US3.
  - US2 depends on US1's `theme.js` boot wiring (T011) to hang reconciliation off of, but is otherwise independently testable per its own Independent Test.
  - US3 depends on Foundational's palette authoring (T006) but not on US1 or US2's UI/persistence work.
- **Polish (Phase 6)**: Depends on all 3 user stories being complete.

### Within Each Phase

- Setup: T001 → T003 (same file, sequential); T002 is `[P]` (different files).
- Foundational: T004 and T005 are `[P]` (different files); T006 needs both; T007 needs T006; T008 needs T007.
- US1: T009 → T010 → T011 (shared files, sequential); T012 and T013 can run any time after T007.
- US2: T015 (test) is written first and expected to fail until T016-T018 land; T016 → T017 → T018; T019 needs T011 and T018; T020 needs T019.
- US3: T022 and T023 are `[P]` (different files); T024 needs both plus T006; T025 needs T024.

### Parallel Opportunities

- Setup: T002 alongside T001/T003.
- Foundational: T004 alongside T005.
- Once Foundational (Phase 2) is done, US3's palette/token audit (T022, T023) can proceed in parallel with US1's UI wiring (T009-T013) — they touch different files. US2 can also start in parallel on its backend half (T015-T018 touch only `app/authstore.py`/`app/api/users.py`/`tests/`), independent of US1's frontend files, though T019-T020 need US1's T011 to exist first.
- Polish: T026 and T027 are `[P]`.

---

## Parallel Example: Foundational Phase

```bash
# Launch together — different files, no shared dependency:
Task: "Create app/static/js/theme.js theme catalog (ids, labels, decorativeEffects)"
Task: "Author [data-theme=...] CSS token blocks for daylight/slate/contrast in app/static/style.css"
```

## Parallel Example: User Story 2 backend vs. User Story 1/3 frontend

```bash
# Once Foundational is done, these can proceed at the same time by different people:
Task: "US2 backend: migration + AuthStore methods + API endpoint + pytest (app/authstore.py, app/api/users.py, tests/test_theme.py)"
Task: "US1 frontend: theme picker UI + local persistence (app/static/index.html, app/static/js/main.js)"
Task: "US3 frontend: geo/chart token audit (app/static/style.css, app/static/js/charts/common.js and callers)"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T003).
2. Complete Phase 2: Foundational (T004-T008) — CRITICAL, blocks everything else.
3. Complete Phase 3: User Story 1 (T009-T014).
4. **STOP and VALIDATE**: run `quickstart.md` steps 1-3, 6, 7 independently.
5. Demo: 4 themes, instant switching, per-browser persistence — already the feature's core value even before account sync exists.

### Incremental Delivery

1. Setup + Foundational → theming engine ready (not yet user-visible).
2. Add US1 → per-browser theme switching → **MVP demoable**.
3. Add US2 → cross-device sync for logged-in users.
4. Add US3 → accessibility/readability hardening across all 4 themes.
5. Polish → README, regression check, full validation pass.

### Parallel Team Strategy

With 2-3 developers, after Foundational (Phase 2) completes:

- Developer A: US1 (frontend picker + local persistence).
- Developer B: US2 (backend migration/endpoint first, then reconciliation logic once US1's T011 lands).
- Developer C: US3 (geo/chart token audit — independent of A and B's files).

---

## Notes

- `[P]` tasks touch different files and have no unmet dependency.
- `[Story]` labels map each task to its user story for traceability; Setup/Foundational/Polish tasks carry no story label by design.
- Commit after each task or logical group, per repo convention.
- Constitution Principle IV (browser-verified) is treated as a required task in every phase that has user-visible output (T014, T021, T025, T028) — not an optional nice-to-have.
