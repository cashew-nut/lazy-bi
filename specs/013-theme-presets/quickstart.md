# Quickstart Validation: Pre-Packed Theme Selector

Runnable scenarios proving the feature end-to-end. Contracts:
[contracts/theme-preference-api.md](contracts/theme-preference-api.md);
entities: [data-model.md](data-model.md).

## Prerequisites

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

## 1. Automated suites (fast signal)

```bash
.venv/bin/python -m pytest tests/                                  # full suite
.venv/bin/python -m pytest tests/test_theme.py -v                  # new endpoint + migration coverage
```

Expected: green, including a guarded-migration test that starts from a
pre-existing `users` table without the `theme`/`theme_updated_at` columns
and confirms `AuthStore.__init__` adds them in place without data loss
(mirrors the existing `measure_provenance.user_id` migration test if one
exists — check `tests/test_store.py` for the pattern to follow).

## 2. Palette validation for all 4 themes (FR-008, SC-004)

```bash
node app/static/validate_palette.js   # or however it's currently invoked —
                                       # run once per theme's --bg + PALETTE,
                                       # not just the default
```

Expected: every one of the 4 themes' `chartPalette` + `chartOtherColor`
passes the existing OKLCH lightness/chroma, CVD-separation, and WCAG
contrast checks against its own `--bg` — not just Cyberpunk's.

## 3. Browser walkthrough (Constitution Principle IV — required, not optional)

```bash
./run.sh   # or however the dev server is normally started
```

1. Log in. Go to **ACCOUNT**. Confirm a theme picker is present showing all
   4 themes, with the currently-active one indicated.
2. Select each of the 4 themes in turn. For each:
   - Confirm the whole app re-skins within ~1 second, no page reload
     (SC-001) — check HOME, STUDIO, MODELLING, PORTAL, CHAT, ACCOUNT, and
     the login screen (log out and back in without losing the selection).
   - Open a chart with 8+ categories (e.g. a bar/line chart with many
     series). Confirm every series remains visually distinguishable and
     legible (User Story 3 / SC-004) — this is the case most likely to
     regress silently if the palette-mutation approach in research.md §2
     is implemented incorrectly.
   - Check the browser console: zero errors on theme switch.
3. **Local persistence (FR-004, SC-002)**: with a non-default theme
   selected, hard-reload the page. Confirm the same theme is still active
   without a visible flash of the default theme first (research.md §3).
4. **Account sync (FR-005, SC-003)**: while logged in, select a theme.
   Open a second, unrelated browser (or a private window), log in as the
   same user. Confirm the same theme is applied automatically there.
5. **Reconciliation (FR-006)**: in browser A, select theme X. In browser B
   (already logged in, previously synced to something else), select theme
   Y a few seconds later. Reload browser A. Confirm browser A converges to
   theme Y (the most recently chosen one), not theme X.
6. **New-user default (FR-007, SC-005)**: create a brand-new user (or
   clear `localStorage` + use an account that has never set a theme).
   Confirm they see Cyberpunk, unchanged from today's experience, with no
   forced onboarding/prompt.
7. **Storage-unavailable fallback (edge case)**: in a private/incognito
   window with storage blocked (or via devtools storage override),
   confirm the app falls back to the default theme silently — no visible
   error.

## 4. Regression check on existing behavior

Confirm nothing in the existing Cyberpunk experience visibly changed for a
user who never touches the theme picker — SC-005 exists specifically to
catch accidental drift introduced while extracting hardcoded colors into
tokens. A pixel-level or close visual diff of a few representative screens
before/after this feature, still on the `cyberpunk` theme, is the
lightest-weight way to catch this.
