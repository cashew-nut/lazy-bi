# Phase 1 Data Model: Pre-Packed Theme Selector

## Theme (code-defined, not a database row)

A fixed catalog of exactly 4 entries, defined in `style.css` (token blocks)
and `theme.js` (the id list + matching chart palette). Not user-editable
(out of scope per spec) and not stored in the database — this is
application configuration, the same category as e.g. the fixed `ROLES`
tuple already in `app/authstore.py`.

| Field | Type | Notes |
|---|---|---|
| `id` | enum string | One of `cyberpunk` \| `daylight` \| `slate` \| `contrast`. `cyberpunk` is the system default. |
| `label` | string | Human-readable name shown in the theme picker. |
| `tokens` | CSS custom properties | `--bg`, `--panel`, `--panel-2`, `--line`, `--neon`, `--neon-dim`, `--pink`, `--ink`, `--ink-2`, `--ink-3`, `--bad`, `--ok`, `--warn`, `--mono` (the last two are new tokens extracted from today's raw hex literals — see research.md §1). |
| `chartPalette` | 8 hex strings | Categorical series colors, validated via `validate_palette.js` against this theme's `--bg`. |
| `chartOtherColor` | hex string | Neutral color for the folded "Other" series. |
| `decorativeEffects` | boolean | Whether the scanline/glow overlay (`body::before`/`body::after` in `style.css`) renders. `true` for Cyberpunk only in the initial set — Daylight/Slate/Contrast ship without it (research.md §7). |

**Validation rule**: every `chartPalette` + `chartOtherColor` pair MUST
pass `validate_palette.js` before a theme is considered shippable (FR-008).

**No state transitions** — themes themselves are static; only which theme
is *selected* changes (see Theme Preference, below).

## Theme Preference (per-user, two persisted copies)

Not a new entity type in the domain sense — it's one small addition to the
existing **User** entity, expressed in two places that must reconcile:

### Account copy — new columns on `users` (`app/authstore.py`)

| Column | Type | Notes |
|---|---|---|
| `theme` | `TEXT`, nullable | One of the 4 `Theme.id` values, or `NULL` meaning "no preference recorded yet → defaults to `cyberpunk`" (FR-007). |
| `theme_updated_at` | `TEXT`, nullable | ISO-8601 UTC timestamp, server-clock-stamped on every write. `NULL` iff `theme` is `NULL`. |

Validation: `theme`, when non-null, MUST be one of the 4 known ids —
enforced server-side in the API layer (reject anything else with 422), not
via a `CHECK` constraint, matching how `role` is validated at the
`AuthStore`/API boundary elsewhere in this file rather than solely by SQL.

### Local copy — `localStorage` (browser, not server)

Key: a single namespaced key (e.g. `ci_theme`). Value: a small JSON object.

| Field | Type | Notes |
|---|---|---|
| `theme` | string | One of the 4 `Theme.id` values. |
| `updatedAt` | string | ISO-8601 timestamp, client-clock-stamped at the moment of local selection. |

**Relationship / reconciliation**: These two copies are independent until
an authenticated session reconciles them (research.md §5): the newer
`updatedAt`/`theme_updated_at` wins and is written back to the other side.
Logged-out browsing only ever has the local copy; it's promoted to the
account copy the next time that browser's user logs in, if it's newer than
whatever (if anything) the account already has.

**Deliberately not modeled**: no history/audit trail of past theme
selections (unlike `audit_events`, which does track other user actions) —
a theme change is not a security-relevant event, and the spec's own
Assumptions rule out any user-facing conflict history.
