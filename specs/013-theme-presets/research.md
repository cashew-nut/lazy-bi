# Phase 0 Research: Pre-Packed Theme Selector

All items below were resolved by directly inspecting the current codebase
(`app/static/style.css`, `app/static/js/charts/common.js`,
`app/static/validate_palette.js`, `app/authstore.py`, `app/store.py`,
`app/conversationstore.py`, `app/api/users.py`, `app/static/index.html`) —
no items were left as `NEEDS CLARIFICATION` in the plan's Technical Context.

## 1. How to scope theme tokens across 4 themes

**Decision**: Keep the existing `:root { --bg: ...; --panel: ...; }` block
as the **Cyberpunk** (default) token set, and add three sibling blocks
selected by an attribute selector on `<html>`: `[data-theme="daylight"] { --bg: ...; }`,
`[data-theme="slate"] { ... }`, `[data-theme="contrast"] { ... }`. All
existing `var(--token)` usage (363 references) needs no changes at all.

**Rationale**: `style.css` already centralized 9 of its ~30 color values
into custom properties — this is the natural extension of a pattern
already in place, requires zero JS for 90%+ of the visual surface, and the
browser repaints on attribute change with no flash beyond what a single
class/attribute mutation costs.

**Alternatives considered**:
- *Separate `<link>` stylesheet per theme, swapped via `href`* — rejected:
  adds a network request and a guaranteed flash-of-unstyled-content on
  every switch; also means 4x duplication of the ~1050 non-color CSS rules
  instead of ~30 token values.
- *CSS-in-JS / a styling library* — rejected outright: the constitution's
  Technology Constraints section is explicit that "no bundler, no
  framework" is a deliberate choice for this frontend, not an oversight.

## 2. How to re-theme chart data colors without touching 6 chart files

**Decision**: `PALETTE` and `OTHER_COLOR` in `charts/common.js` stay
exported the same way, but a new `theme.js` module mutates their **contents
in place** (`PALETTE.splice(0, PALETTE.length, ...newColors)`) rather than
consumers importing a "current theme" accessor.

**Rationale**: Traced every import of `PALETTE` (`main.js`, `geo.js`,
`pivot.js`, `sankey.js`, `scatter.js`, `line.js`, `dashboard.js`) — all six
chart-rendering consumers index into it at render/call time (e.g.
`PALETTE[0]`, loop over `PALETTE`), never destructure or cache a copy at
module-load time. Because `const PALETTE = [...]` is a constant *binding*
to a mutable array, replacing the array's contents is visible to every
importer automatically — this is the single biggest scope-reducer in the
plan, since it avoids an invasive rewrite of 6 chart-rendering files' call
sites.

**Alternatives considered**:
- *`getCurrentPalette()` accessor function, rewrite all 6 call sites* —
  rejected as unnecessary invasiveness and regression risk for a first
  pass; revisit only if a future feature needs per-call palette overrides.
- *Separate palette module per theme, conditionally imported* — rejected:
  dynamic `import()` adds async complexity to synchronous chart-render
  paths for no benefit over in-place mutation.

## 3. How to avoid a flash of the wrong theme on load

**Decision**: A small, synchronous, non-module `<script>` inline in
`index.html`'s `<head>` (before `style.css`'s effect would otherwise be the
only thing painted) reads the resolved theme (see reconciliation, below)
from `localStorage` and sets `document.documentElement.dataset.theme`
before first paint.

**Rationale**: `main.js` loads as `<script type="module">`, which is
deferred by spec until after the document is parsed — waiting for it would
paint the default (Cyberpunk) theme first, then visibly snap to the user's
actual choice on every load. A blocking inline script avoids this.

**Alternatives considered**:
- *Set `data-theme` from `main.js` after DOM ready* — rejected: causes the
  exact flash this decision exists to prevent.
- *Server-rendered `data-theme` from a cookie* — rejected: `index.html` is
  currently served as a static file with no per-request templating;
  introducing that machinery for one attribute is disproportionate scope
  for a first pass, and unauthenticated/first-load users have no server
  record to render from anyway.

## 4. Where the account-level preference lives

**Decision**: Two new nullable columns directly on the existing `users`
table in `app/authstore.py`: `theme TEXT` and `theme_updated_at TEXT`
(ISO-8601, UTC — matching every other timestamp column in this schema).
Added via a guarded `ALTER TABLE` in `AuthStore.__init__`, e.g.:

```python
cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
if "theme" not in cols:
    conn.execute("ALTER TABLE users ADD COLUMN theme TEXT")
    conn.execute("ALTER TABLE users ADD COLUMN theme_updated_at TEXT")
```

**Rationale**: This exact guarded-migration shape already exists twice in
this codebase (`VisualStore`'s `measure_provenance.user_id` in
`app/store.py`, `ConversationStore`'s `conversations.llm_model` in
`app/conversationstore.py`) — following it keeps the migration
unsurprising and consistent. A single scalar preference does not warrant a
new table or a generic key-value preferences store (that would be
premature generalization for exactly one setting).

**Alternatives considered**:
- *Generic `user_preferences(user_id, key, value)` table* — rejected: no
  second preference exists yet to justify the generality; add it when a
  second one actually shows up.
- *New dedicated `theme_preferences` table* — rejected: unnecessary join
  for a 1:1 scalar that belongs on the user row, same reasoning the
  existing schema already applies to `role`/`is_active`/etc.

## 5. Reconciling local vs. account preference (FR-006)

**Decision**: Reconciliation happens client-side, in `theme.js`, on
successful login / app boot for an authenticated user: fetch
`GET /api/users/me/theme` → `{theme, updated_at}`, compare `updated_at`
against the localStorage copy's own `updatedAt`, apply whichever is
newer, then write the winner back to whichever side lost (`PUT` if the
server was stale, `localStorage.setItem` if the client was stale) so both
converge. The server always stamps `theme_updated_at` with its own clock
on `PUT` (not a client-supplied timestamp) to keep the account record
trustworthy; the localStorage copy is client-clock-stamped, so cross-device
comparison has best-effort (not cryptographically synced) clock accuracy —
acceptable given this is a cosmetic, low-stakes preference and the spec's
own Assumptions section already calls out "no user-facing conflict
prompt."

**Rationale**: Matches FR-006 (most-recent-selection wins) directly, keeps
the server simple and stateless per request (it never needs to "merge"
anything, just read/write one row), and requires no new sync protocol.

**Alternatives considered**:
- *Account always wins* — rejected: explicitly contradicts FR-006 and
  would silently discard a same-session local change for any logged-in
  user, which is worse UX than the feature is trying to deliver.
- *Server-side conflict resolution with request-time server timestamps
  for both sides* — rejected: would require the client to round-trip its
  local pick through the server before it could even compare, adding
  latency to something that today is a synchronous localStorage read.

## 6. Designing and validating the 3 new theme palettes

**Decision**: Reuse `app/static/validate_palette.js` unmodified — it
already checks OKLCH lightness band, chroma floor, colorblind-safe (CVD)
separation, and WCAG contrast for a categorical palette against a given
`--bg`. Each of the 3 new themes gets its own 8-color `PALETTE` +
`OTHER_COLOR` run through this same script against its own `--bg` before
being accepted, exactly as the existing Cyberpunk palette already is
(comment at `style.css:6` ties it to this validation).

**Rationale**: This is the exact tool this codebase already built for this
exact problem (FR-008/SC-004) — reusing it is both less work and the only
way to credibly claim the same accessibility bar the spec requires.

**Alternatives considered**: None seriously considered — building a new
validator would duplicate existing, working logic for no reason.

## 7. Proposed identity of the 3 new themes

**Decision**: Ship these three alongside the unchanged **Cyberpunk**
(default) theme — final hex values authored during implementation, run
through `validate_palette.js` before acceptance, not finalized here:

- **Daylight** — light background, dark text; for users in bright
  environments or who simply don't want a dark UI.
- **Slate** — a muted, low-glow dark theme (no neon glow/scanline
  decoration); for users who want dark-mode without the neon aesthetic
  (e.g. screen-sharing, boardroom displays).
- **Contrast** — a high-contrast theme tuned toward accessibility (larger
  effective contrast ratios, no decorative glow/blur, stronger focus
  rings); for users who need it, not a cosmetic option.

**Rationale**: Directly answers the feature's own motivation ("I love
cyberpunk but others may not") with genuinely distinct alternatives (light,
muted-dark, accessibility-first) rather than three cosmetic palette swaps
of the same dark-neon idea.

**Alternatives considered**: Naming/exact count wasn't otherwise
constrained by the spec (see spec.md Assumptions) — three variations on a
dark theme were considered and rejected in favor of covering genuinely
different user needs (light vs. dark vs. accessibility) with only 3 slots
available.
