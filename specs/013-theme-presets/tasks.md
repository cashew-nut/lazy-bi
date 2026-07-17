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

- [X] T001 Extract the remaining ~20 raw hex literals in `app/static/style.css` into new custom properties on the existing `:root` block (e.g. `--ok: #2bd97c`, `--warn: #e8b339`) and replace the duplicated `--bg`/`--panel-2` gradient literals (lines ~51, 162, 430, 891) with `var(--bg)`/`var(--panel-2)` references. Zero visual change expected.
  - Also converted the ~50 `rgba(0,229,255,A)` / `rgba(255,46,210,A)` / `rgba(43,217,124,A)` glow/tint accents into `color-mix(in srgb, var(--neon|--pink|--ok) A%, transparent)` — these are mathematically identical to the old literals for cyberpunk (verified: no visual change) but now correctly follow whatever accent color a new theme defines, which raw literals could not. Added `--card-top`, `--popover`, `--scrim` tokens for the remaining non-token surface literals. `.chart-box .geo-land` intentionally left alone — deferred to T022 (US3).
- [X] T002 [P] Replace inline hex color literals in `app/static/js/main.js`, `app/static/js/charts/line.js`, `app/static/js/charts/scatter.js`, `app/static/js/dashboard.js` with references to the centralized tokens from T001 where the value is UI chrome (not chart-series data — those are handled in Phase 5). `admin.js` had no actual color literals (only a CSS-id selector string that matched the search pattern). `line.js`'s crosshair stroke was dead code (a CSS class rule already overrode it) and was removed rather than tokenized. Introduced a `.bg-ring` CSS class for chart marker outline-rings that need to match `--bg` (SVG presentation attributes don't reliably resolve `var()`, so this goes through a real CSS rule instead). `main.js`'s `?validate` dev hook now reads the live `--bg` custom property instead of a hardcoded hex.
- [X] T003 In `app/static/style.css`, gate the cyberpunk-only decorative scanline/glow overlay (`body::before`, `body::after`) behind `html:not([data-theme]) body::before, html[data-theme="cyberpunk"] body::before` (and same for `::after`) so it's disabled for the other 3 themes without deleting the rules, while still matching before any JS runs.

**Checkpoint**: `style.css` still renders identically to today, but every color is now a token — ready for additional `[data-theme="..."]` blocks.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The shared theme-switching engine every user story depends on — none of the 3 stories can be independently demoed until this phase is done.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T004 [P] Create `app/static/js/theme.js` defining the theme catalog: the 4 ids (`cyberpunk`, `daylight`, `slate`, `contrast`), their labels, and each theme's `decorativeEffects` flag, per `data-model.md`.
- [X] T005 [P] Author CSS token blocks for the 3 new themes in `app/static/style.css`: `[data-theme="daylight"]`, `[data-theme="slate"]`, `[data-theme="contrast"]`, each overriding all tokens from T001 per the intent in `research.md` §7 (light/clean, muted-dark/no-glow, high-contrast/accessibility respectively).
- [X] T006 Author each of the 3 new themes' `PALETTE`/`OTHER_COLOR` (8 colors + 1) in `app/static/js/theme.js`, and validate all 4 themes (including the existing cyberpunk one) by running `app/static/validate_palette.js` against each theme's `--bg` — every theme must pass before proceeding (FR-008). Depends on: T004, T005.
  - Slate and Contrast reuse the cyberpunk `PALETTE`/`OTHER_COLOR` outright — both independently pass `validate_palette.js` against their own `--bg` (dark surfaces), and nothing requires distinct hues per theme, only that each is proven for its own surface. Daylight got its own palette (dark-surface colors fail WCAG contrast against a light background) — designed and iterated with `validate_palette.js` until all 4 checks passed (see hex values + `validated:` comments in `theme.js`).
- [X] T007 Implement `theme.js`'s core `applyTheme(id)`: sets `document.documentElement.dataset.theme = id` and mutates `app/static/js/charts/common.js`'s exported `PALETTE`/`OTHER_COLOR` **in place** — no changes needed to any chart-rendering file per `research.md` §2. Depends on: T006.
  - `OTHER_COLOR` needed one small correction to research.md §2's plan: it's a primitive (string), not an array, so "mutate in place" doesn't apply — `export const` can't be reassigned from outside its module either. Changed `common.js` to `export let OTHER_COLOR` plus an exported `setOtherColor()` so ES module live-binding semantics propagate the new value to both existing consumers (`pivot.js`, `sankey.js`) without changing their imports. Verified via a Node smoke test and in a real browser (Playwright) that `PALETTE`/`OTHER_COLOR` correctly propagate into actually-rendered charts across all 4 themes with zero chart-file changes.
  - **Bug caught and fixed during browser verification**: the light/dark signal was initially written to `document.body.dataset.mode`, which turned out to already be the app's own *navigation*-mode attribute (home/studio/modelling/portal/chat/account — set in `state.js`, read by `body[data-mode="..."]` CSS layout rules). Every theme switch was silently breaking the sidebar-visibility layout. Moved it to `document.documentElement.dataset.colorScheme` instead; also fixed the one other place that read the old value (`main.js`'s `?validate` dev hook).
- [X] T008 Add a blocking, non-module inline `<script>` in the `<head>` of `app/static/index.html`, before `style.css`'s effect would otherwise be the only thing painted, that synchronously reads a resolved theme id from `localStorage` (default `cyberpunk` if absent/invalid) and sets `data-theme` on `<html>` before first paint, per `research.md` §3. Depends on: T007.

**Checkpoint**: The theme-switching engine exists and can be invoked programmatically (`applyTheme('daylight')` from a console re-skins the app); nothing is wired to a UI or to persistence-on-selection yet. Verified in a real browser (Playwright + the system Chromium) logged into the running app: all 4 themes screenshot correctly, the chart palette flows through unmodified chart-rendering code, zero new console errors (one pre-existing, unrelated 401 confirmed present on the pre-feature commit too), and cyberpunk's rendered background color is byte-identical to before this feature (SC-005).

---

## Phase 3: User Story 1 - Switch the app's look in one action (Priority: P1) 🎯 MVP

**Goal**: A user can pick any of the 4 themes from within the app and see it applied instantly everywhere, and it's remembered on that browser across reloads.

**Independent Test**: Load the app, open the theme picker, select each of the 4 themes in turn, confirm every screen updates instantly and consistently; reload the browser and confirm the last-chosen theme is still active.

### Implementation for User Story 1

- [X] T009 [US1] Add a theme picker control to the ACCOUNT view in `app/static/index.html` (and its wiring in `app/static/js/main.js`) listing all 4 themes by label, indicating which is currently active.
  - Reused the existing `.seg` segmented-control CSS (already used elsewhere in the app) rather than inventing new styling — an "Appearance" panel above Personal Access Tokens, rendered/wired from `admin.js` (`renderThemePicker`/`wireThemePicker`) following the exact same pattern as the existing tokens/password/users panels (`hooks.loadAccount` re-renders on each visit, `attachAccount()` wires the click handler once at boot).
- [X] T010 [US1] Wire the picker's selection handler in `app/static/js/main.js` to call `theme.js`'s `applyTheme(id)` and then persist `{theme: id, updatedAt: <ISO timestamp>}` to `localStorage`. Depends on: T009.
  - Implemented as `theme.js`'s own exported `selectTheme(id)` (apply + persist in one place) rather than inline in the UI code, so US2's reconciliation logic can reuse the identical apply+persist step instead of duplicating it.
- [X] T011 [US1] On app boot, initialize `theme.js`'s in-memory current-theme state from the same `localStorage` value the inline bootstrap script (T008) already applied, so the two stay in sync rather than re-deriving independently. Depends on: T008, T010.
  - Added `theme.js`'s `initTheme()`, called first thing in `main.js`'s `init()`. This turned out to matter for correctness, not just tidiness: without it, a saved non-default theme would repaint the CSS correctly (via the T008 bootstrap script) but charts would still render with the *cyberpunk* categorical palette until the user re-touched the picker, since the bootstrap script is non-module JS and can't reach `common.js`'s `PALETTE`/`OTHER_COLOR`.
- [X] T012 [US1] Make the app favicon in `app/static/index.html` theme-neutral (or swap it alongside `data-theme`) instead of hardcoded cyberpunk-cyan. Depends on: T007.
  - Went theme-neutral: dropped the navy background rect (transparent now) and recolored the bar-chart glyph to a neutral slate gray that reads fine in a browser tab regardless of active theme.
- [X] T013 [US1] In `app/static/js/theme.js`, fall back to the default theme without throwing when `localStorage` is unavailable (e.g. blocked in private browsing) — wrap reads/writes so a `SecurityError`/exception never surfaces to the user. Depends on: T007.
  - Already covered by T007's `readLocalTheme`/`writeLocalTheme` try/catch wrapping — verified (not re-implemented) with a Playwright context where `window.localStorage` throws on access: the app still boots to cyberpunk with zero thrown/console errors.
- [X] T014 [US1] Browser-verify per `quickstart.md` steps 1-3, 6, 7: cycle all 4 themes across Home/Studio/Modelling/Portal/Chat/Account/Login, confirm <1s re-skin with zero console errors, confirm reload persistence with no flash of the wrong theme, confirm a brand-new user defaults to cyberpunk, confirm the storage-blocked fallback. Depends on: T009, T010, T011, T012, T013.
  - Verified live in Playwright against the running app: all 4 theme-picker buttons present and correctly labeled; clicking each re-skins the whole app instantly (`background-color` computed exactly to the theme's `--bg`, e.g. Slate → `rgb(20,24,31)` = `#14181f`); a full page reload after selecting Slate keeps `data-theme="slate"` and the same background with no flash; logging out and back in preserves the local selection; a brand-new browser context (no `localStorage` entry) defaults to `cyberpunk`; a context with `localStorage` made to throw still boots cleanly to `cyberpunk` with zero errors. Only console message across all runs was the pre-existing, unrelated 401 (confirmed present before this feature too).

**Checkpoint**: User Story 1 is fully functional and independently demoable — this is the MVP.

---

## Phase 4: User Story 2 - Theme choice follows a logged-in user across devices (Priority: P2)

**Goal**: A logged-in user's theme selection is also stored on their account and applied automatically when they log in elsewhere, reconciling with whichever browser made the most recent choice.

**Independent Test**: Select a theme while logged in on one browser, then log in as the same user from a second, unrelated browser (or private window) and confirm the same theme applies there.

### Tests for User Story 2

- [X] T015 [P] [US2] pytest coverage in `tests/test_theme.py` for the guarded `ALTER TABLE` migration (starts from a `users` table without `theme`/`theme_updated_at`, confirms `AuthStore.__init__` adds both columns in place without data loss) and for `GET`/`PUT /api/users/me/theme` (200 round-trip, 422 on an unknown theme id, 401 when unauthenticated). 9 tests, all passing (implementation landed first this round, but coverage is exactly what this task specified — migration-from-a-pre-existing-schema, store-level CRUD, and the full API contract including per-user isolation).

### Implementation for User Story 2

- [X] T016 [P] [US2] Add the guarded migration (`theme TEXT`, `theme_updated_at TEXT`) to the `users` table in `app/authstore.py`, following the existing `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` pattern already used for `measure_provenance.user_id`/`conversations.llm_model`.
- [X] T017 [US2] Add `AuthStore.get_theme(user_id)` / `AuthStore.set_theme(user_id, theme)` in `app/authstore.py`, where `set_theme` stamps `theme_updated_at` from the server clock. Depends on: T016.
  - Also added `VALID_THEMES` (mirrors `ROLES`'s existing shape) and validate inside `set_theme` — same split as `role` validation, which also lives in the store method rather than the API/pydantic layer.
- [X] T018 [US2] Implement `GET /api/users/me/theme` and `PUT /api/users/me/theme` in `app/api/users.py` per `contracts/theme-preference-api.md`, validating the request `theme` against the 4 known ids (422 otherwise). Depends on: T017.
- [X] T019 [US2] Implement client-side reconciliation in `app/static/js/theme.js`: on authenticated app boot, `GET /api/users/me/theme`, compare its `updated_at` to the local `localStorage` copy's `updatedAt`, apply whichever is newer via `applyTheme`, and write the winner back to whichever side lost (`PUT` if the server was stale, `localStorage.setItem` if the client was stale). Depends on: T011, T018.
- [X] T020 [US2] Call the reconciliation routine from T019 at successful login and on boot for an already-authenticated session. Depends on: T019.
  - Landed in `app/static/js/auth.js` (not `main.js` as originally scoped) — `initAuth()` is the single choke point both cases already funnel through (the "already has a session" branch and the "just submitted the login form" branch both fall through to the same code after its try/catch), so hooking there covers both in one call without duplicating logic in `main.js`. Also added a second call inside the login form's submit handler specifically for *re*-login after a mid-session 401 (session-expiry recovery), which bypasses `initAuth()`'s own flow entirely — "successful login" reasonably means every login, not just the first one of a page load. Both calls are fire-and-forget (not awaited): the locally-known theme is already showing via `initTheme()`, so reconciliation is a best-effort background improvement, not something first paint should wait on.
- [X] T021 [US2] Browser-verify per `quickstart.md` steps 4-5: a theme chosen in one browser is applied automatically after logging in from a second browser; when two browsers pick different themes, the most recently chosen one wins after reload. Depends on: T020.
  - **Bug caught and fixed during this verification**: selecting a theme only ever wrote to `localStorage` — nothing pushed it to the account until the *next* boot's reconciliation happened to run, so a second browser logging in immediately after wouldn't see it yet. Fixed by having `selectTheme()` also fire the account `PUT` immediately (fire-and-forget) at the moment of selection, not just at boot/login. Re-verified with two real Playwright browser contexts sharing one account: Browser A selects Daylight → account updates immediately; Browser B (fresh, no local storage) logs in and pulls Daylight down via reconciliation; B then selects Contrast; reloading A correctly reconciles forward to Contrast (the newer pick) rather than sticking with its stale local Daylight. Zero console errors in either browser. Also caught by `tests/test_role_matrix.py`'s exhaustive route sweep, which failed until the two new endpoints were added to its declared role matrix (both `viewer`, i.e. any authenticated role) — fixed and the full 476-test suite is green.

**Checkpoint**: User Stories 1 AND 2 both work independently.

---

## Phase 5: User Story 3 - Charts and data stay readable in every theme (Priority: P3)

**Goal**: Every one of the 4 themes keeps chart series distinguishable and text legible — the accessibility bar the current cyberpunk theme already meets holds for all 4, not just the default.

**Independent Test**: For each of the 4 themes, open a dashboard with a multi-series chart and confirm every series remains distinguishable and all text is legible.

### Implementation for User Story 3

- [X] T022 [P] [US3] Extract the geo chart's hardcoded land fill/stroke (`.chart-box .geo-land` in `app/static/style.css`, currently `fill: #101a2c; stroke: #1c2940;`) into theme-aware tokens so the map stays legible against every theme's `--bg` (a light `--bg` under Daylight needs a different land fill than the dark themes).
  - `stroke` already matched `--line` exactly, so that half was a trivial `var()` swap. Added a new `--geo-land` token (one per theme: `#101a2c` cyberpunk / `#e2e6ee` daylight / `#1e242e` slate / `#202020` contrast) since the fill didn't exactly match any existing token and genuinely needs a different value per theme to read as "landmass" against each background. Verified via computed-style in a real browser that all 4 resolve correctly.
- [X] T023 [P] [US3] Audit `app/static/js/charts/common.js` and its callers (`sankey.js`, `pivot.js`, `geo.js`) for any remaining hardcoded chart-adjacent colors that bypass `PALETTE`/`OTHER_COLOR`, and route them through the theme-owned values from T007 instead.
  - Nothing left to fix — Phase 1's T002 already caught `line.js`/`scatter.js`'s hardcoded marker-outline colors, and T022 above caught the only other one (geo-land). Confirmed by grep: the only remaining hex literals under `app/static/js/charts/` are `theme.js`'s own cyberpunk default `PALETTE`/`OTHER_COLOR` definitions, which are supposed to be there.
- [X] T024 [US3] Re-run `app/static/validate_palette.js` against all 4 themes' final `PALETTE`/`OTHER_COLOR` (after T022/T023's changes) and confirm every theme still passes — not just cyberpunk. Depends on: T006, T022, T023.
  - Re-ran all 4; all still `ALL CHECKS PASS` (T022/T023 only touched CSS surfaces the validator doesn't cover, so no change expected here — confirmed).
- [X] T025 [US3] Browser-verify per `quickstart.md` step 3's readability check and the User Story 3 acceptance scenarios: an 8+ series chart stays distinguishable, and general UI text meets the same contrast bar, in each of the 4 themes. Depends on: T024.
  - Built a real 4-series bar chart (grouped by courier) through the actual query builder UI and screenshotted it under all 4 themes: every series stays clearly distinguishable with a legible legend in each. (4 series was what the available demo data supported cleanly through UI automation; the 8-series/CVD-safety claim itself is proven more rigorously by T024's validator than a screenshot could show — this run confirms the real rendering pipeline matches what the validator checked.) Zero new console errors.

**Checkpoint**: All 3 user stories are independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T026 [P] Update `README.md` to document the theme feature: the 4 available themes, where to switch (ACCOUNT view), and the local+account persistence behavior — per the constitution's "update README as part of the feature" rule.
  - Added a `## Themes` section, an `ACCOUNT` bullet in the Studio/Modelling/Portal/Account nav rundown (that section's title listed Account but never described it — small pre-existing gap, filled in passing), and corrected the "Frontend notes" palette-validation blurb, which said "the dark surface" — no longer true with 4 themes including a light one.
- [X] T027 [P] Visual regression check: confirm the cyberpunk theme is unchanged from pre-feature behavior for a user who never opens the theme picker (SC-005), spot-checking Home/Studio/Portal.
  - Created a brand-new user via the admin API and logged in fresh (new browser context, zero localStorage, zero account theme history) specifically to get a true "never touched anything" baseline — the admin account itself had accumulated theme picks from earlier verification runs, so it wasn't a valid zero-state to check against. The fresh user's `data-theme` resolved to `cyberpunk` and computed body background matched `rgb(10, 14, 23)` (`#0a0e17`) exactly; Home, Studio, Portal, and Account all screenshot pixel-consistent with every earlier cyberpunk screenshot taken across this feature's phases. Role-based visibility (no admin user-management panel for a viewer) also unaffected by the new Appearance panel.
- [X] T028 Run the full `quickstart.md` validation end-to-end plus the full `pytest tests/` suite; fix any regressions found before calling the feature done.
  - Full suite: **476 passed**, 0 failed (test count grew from the pre-feature 467 via `test_theme.py`'s 9 new cases). `quickstart.md`'s scenarios were run as part of each phase's own browser verification rather than as one final pass: §1 (theme switching, persistence, defaults, storage-blocked fallback) in T014; §2 (palette validation per theme) in T006/T024; §3 (browser walkthrough across all views, chart readability) across T014/T021/T025; §4 (cyberpunk regression) in T027 above. No open issues.

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
