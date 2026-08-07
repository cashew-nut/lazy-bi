# Design — CASH_INTELLIGENCE

A locked design system for this app. Every module redesign reads this file
before emitting code. Do not regenerate per module — extend or amend this file
when the system needs to grow.

Produced by `hallmark redesign` (multi-module scope). The diversification rule
is **inverted** here: modules must share the system, not differ from each other.
Variety lives in macrostructure choice, never in theme, type, or surface voice.

---

## Genre

**modern-minimal.**

CASH_INTELLIGENCE is an operator console for a semantic layer over S3 — dense,
technical, used all day by people who already know the domain. That routes
modern-minimal: restraint, precise hierarchy, minimal motion, function carries
the page.

The *surface* stays atmospheric (dark neon, faint corner washes, scanlines) —
that is the house theme and it is not up for redesign. The genre governs
**structure and restraint**, not palette.

What the genre buys us here, specifically:

- Motion is minimal. The page is composed, not revealed.
- Depth comes from **lightness and weight**, never from coloured glow.
- One accent per surface. No chromatic floods.
- Borders are thin but *meaningful* — never decoration.

---

## The surface-tier rule (the core move)

The app's defining flaw before this redesign: **every container wore the same
costume** — `1px solid var(--line)` + 3px radius + `var(--panel)` fill. Fourteen
different classes, one look, nested up to four deep. That is `card-in-card`,
`the identical feature grid`, and `shadow-glow on dark` shipping together.

From here, containment is **earned, not decorative**. Three tiers, and only
three:

| Tier | Name | Border | Fill | Shadow | What it's for |
| --- | --- | --- | --- | --- | --- |
| 0 | **Ground** | none | none | none | Lists, rows, sections, message turns, the default for everything |
| 1 | **Field** | none | `--panel` / `--panel-2` | none | Genuinely interactive containers: inputs, editors, cells, evidence blocks |
| 2 | **Float** | `1px --line` | `--popover` | yes | Only things that leave the flow: popovers, menus, modals, tooltips |

**The nesting law.** A tier may never contain a tier of equal or higher number.
Ground holds Ground. Field holds Ground. Float holds Field and Ground. Nothing
holds a Float except the viewport. This one rule kills card-in-card everywhere
it currently lives.

**Separation without boxes.** Ground siblings are separated by a single
`--rule-hair` bottom border and honest spacing — not by giving each one a box.
A list of ten things is a *ledger*, not ten cards.

**Selection without glow.** A selected/active Ground row is marked with
`box-shadow: inset 2px 0 0 var(--neon)` (an inset rule, no outer halo) plus a
`--panel` fill lift. Never an outer `box-shadow` on a dark surface — that is the
shadow-glow tell.

---

## Macrostructure families

Three page types. Within a family, modules share the shape and vary only in
component archetype.

- **Console pages** (Home · Portal · Modelling · Account) → **Index-First**.
  The page IS a list of destinations or inventory. Numbered or hairline-ruled
  rows, left-set, no hero, no card grid. Navigation as design.

- **Workbench pages** (Studio · Model editor · Model/bundle form · Sandbox ·
  Dashboards · Notebook reader) → **Workbench**. Rails and panes framing a live
  canvas. The canvas is the content; chrome recedes to hairlines and labels.

- **Dialogue pages** (Chat · Composer · panel chat · sandbox agent) →
  **Split Studio**. Diptych: the conversation on one side, the evidence it
  produced on the other. Every claim earns visible proof.

The `.chat-thread` / `.chat-msg` classes are shared by all four dialogue
surfaces — redesigning them once redesigns all four. That is intentional.

---

## Theme

**Locked house tokens.** Four themes, ids frozen (they live in localStorage
payloads, `authstore.VALID_THEMES`, `theme.js`'s `THEMES`, and `index.html`'s
pre-paint boot script). Values are hex, not OKLCH, because
`validate_palette.js` checks each theme's categorical chart palette against its
own `--bg` — converting the surfaces would invalidate every validated palette.

The token *names* are the contract. Never inline a colour; if a value is needed
that has no token, add the token first.

| Token | Role |
| --- | --- |
| `--bg` | app + chart surface |
| `--panel` | Field fill (tier 1) |
| `--panel-2` | Field fill, one step lifted |
| `--card-top` | gradient top stop for the canvas frame |
| `--popover` | Float fill (tier 2) |
| `--line` | hairline rule + Float border |
| `--neon` | primary chrome accent — **not** a data colour |
| `--neon-dim` | `--neon` at 40 % alpha |
| `--pink` | secondary chrome accent — **not** a data colour |
| `--ink` / `--ink-2` / `--ink-3` | primary / secondary / muted text |
| `--ok` / `--warn` / `--bad` | state |
| `--scrim` | modal backdrop |
| `--geo-land` | geo landmass fill |

Themes: `cyberpunk` (default) · `daylight` (Paddock Light) · `slate` (Canopy) ·
`contrast` (Paddock). Every rule below must hold in all four.

**Accent discipline.** `--neon` is the app's single signal colour: it marks the
*active* thing and the *focused* thing, nothing else. `--pink` marks the
*authored* thing (models, measures, saved artefacts). Neither is ever used as
decoration, and neither ever floods a surface — cap chromatic fill at
`color-mix(… 8 %)`.

---

## Typography

Single-family, by design. A terminal-aesthetic console is monospace-only on
purpose — this is the allowed single-font case, not `Inter-everywhere`.

- **Display / body / mono** — `var(--mono)`: `ui-monospace, "SF Mono",
  "Cascadia Mono", Menlo, Consolas, monospace`
- **Weights** — 400 body, 700 for the one-per-view emphasis only
- **Headings are roman.** No italic display, ever. Italic survives only as
  body-copy emphasis and in code comment tokens.
- **Numbers** — `font-variant-numeric: tabular-nums` on every column of figures
  (tables, stat tiles, counts, row counts, byte sizes).

### Type scale

Before this redesign the app used **17 distinct pixel font sizes**. Now: seven
steps, named by role. Nothing else.

```css
--text-2xs: 9px;    /* tags, counts, keycaps — uppercase only */
--text-xs:  10px;   /* section labels, meta, hints */
--text-sm:  11px;   /* dense UI: rows, chips, buttons */
--text-md:  12px;   /* body, inputs, table cells */
--text-lg:  13px;   /* names, titles in chrome */
--text-xl:  15px;   /* view titles, chat questions */
--text-2xl: 19px;   /* wordmark, the one thing per view that is big */
--text-stat: clamp(30px, 4vw, 44px);  /* stat tiles only */
```

### Tracking

Letter-spacing is the app's hierarchy device (mono has no weight range worth
using). Three steps, tied to role:

```css
--track-label: 0.22em;   /* uppercase section labels, nav */
--track-name:  0.08em;   /* names, titles, buttons */
--track-body:  0;        /* prose, code, data */
```

Uppercase is reserved for **labels and actions** — never for content.

---

## Spacing

4-point named scale. Modules must use named tokens; never raw px.

```css
--space-3xs: 2px;   --space-2xs: 4px;   --space-xs:  6px;
--space-sm:  8px;   --space-md:  12px;  --space-lg:  16px;
--space-xl:  24px;  --space-2xl: 36px;  --space-3xl: 56px;
```

**Varied rhythm is mandatory.** If a view's card padding equals its section
padding equals its page padding, the rhythm is flat. Pane padding runs larger
than row padding by at least two steps.

### Radius

Eight ad-hoc radii collapse to three, tied to tier:

```css
--radius-none: 0;    /* Ground — rows, rules, ledger lines */
--radius-sm:   4px;  /* Field — inputs, cells, evidence blocks, buttons */
--radius-pill: 999px;/* pills only: query-strip tokens, status dots */
```

### Rules

```css
--rule-hair: 1px solid var(--line);
--rule-mark: 2px;    /* the inset selection rule */
```

### Z-index — six named levels

Replaces the ad-hoc `999` / `1000` / `700` / `600` / `50` freestyle.

```css
--z-base: 1;  --z-raised: 10;  --z-dropdown: 100;
--z-sticky: 200;  --z-modal: 400;  --z-toast: 500;  --z-tooltip: 600;
```

---

## Motion

Motion-cut project. No library, and none is being added.

```css
--ease-out:    cubic-bezier(0.16, 1, 0.3, 1);
--ease-in:     cubic-bezier(0.7, 0, 0.84, 0);
--ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
--dur-instant: 80ms;
--dur-short:   140ms;
--dur-mid:     240ms;
```

- Animate `transform` and `opacity` only. Never layout properties.
- Never the browser default `ease`. Never bounce or overshoot on UI state.
- **Never `transition: all`.** Name the properties.
- **Focus rings appear instantly** — never transitioned.
- Reveal pattern: **none.** Content is just there.
- `prefers-reduced-motion: reduce` collapses every spatial move to a ≤150 ms
  opacity change, and stops the home cursor blink and the thinking pulse.

---

## Microinteractions stance

- **Silent success.** No celebratory toasts. A save that the user can see
  succeeded needs no announcement — the status text is enough.
- **Optimistic + Undo** over confirmation dialogs for reversible actions.
  Confirm only what cannot be undone.
- **Tooltips** — hover delay 800 ms, focus delay 0 ms. Different intents,
  different timing.
- **One signal per element.** A row that shifts its fill does not also glow,
  scale, and underline.
- **No hover-only affordances.** Every hover reveal has a `:focus-visible`
  equivalent and stays reachable on coarse pointers.
- **Spinners** — delay-show 150 ms, or use a status line instead. Never flash.

---

## The eight states

Every interactive element ships all eight: default · hover · `:focus-visible` ·
active · disabled · loading · error · success.

`:focus-visible` is `outline: 2px solid var(--neon); outline-offset: 2px` —
instant, never animated, ≥3:1 against every one of the four themes' surfaces.

---

## CTA voice

- **Primary** (`.btn`) — `--neon` hairline border, `--neon` label, transparent
  fill; hover raises fill to `color-mix(--neon 12 %)`. **No outer glow.**
- **Authoring** (`.btn.alt`) — same shape, `--pink`. Marks the things that write.
- **Quiet** (`.btn.plain`) — `--line` border, `--ink-2` label. The default for
  anything that isn't the view's main action.
- **Ghost** (`.ghost`) — dashed `--line`, full-width, for "+ add another".
- Labels are **uppercase, tracked `--track-name`, and never wrap** — one line,
  always. Shorten the label before you let it wrap.

Radius `--radius-sm` on all four. One button per view carries `--neon`; the rest
are quiet. If two buttons in a row both shout, one of them is wrong.

---

## Per-module allowances

- **Console pages** MUST use the ledger voice — hairline-ruled rows, no card
  grids, no `repeat(auto-fill, minmax(…))` tile walls.
- **Workbench pages** MAY use one Field-tier canvas frame per view (the chart /
  editor / notebook surface). Rails are Ground on a `--panel` wash, separated
  from the canvas by a single hairline, never by a border-box.
- **Dialogue pages** MUST render assistant turns as documents on Ground, not as
  bubbles. The user's question is a heading, not a chat bubble.
- **No module** gets enrichment. No illustration, no hero art, no decorative
  SVG. Function carries every page in this app.

---

## What modules MUST share

- The wordmark (`CASH_INTELLIGENCE`, `_` in `--pink`) and the mono voice.
- The surface-tier rule and its nesting law.
- The accent placement rule (`--neon` = active/focused, `--pink` = authored).
- The CTA voice — button shape, radius, tracking, one-shout-per-view.
- Section-label rhythm: `--text-xs`, uppercase, `--track-label`, `--neon`, with
  the trailing gradient rule.
- Row rhythm: `--space-xs` vertical padding, `--rule-hair` separator, inset
  selection mark.

## What modules MAY differ on

- Macrostructure, within the family declared above.
- Pane split ratio and rail width.
- Which of `--neon` / `--pink` / `--ok` / `--warn` / `--bad` marks the row
  (Home's per-destination accent is a deliberate, sanctioned exception).

---

## Bans (enforced by the slop test at every module handoff)

1. **Card-in-card** — a bordered box inside a bordered box. Gone.
2. **The identical tile grid** — `repeat(auto-fill, minmax(240px, 1fr))` of
   same-shaped bordered cards. Replaced by ledgers.
3. **Shadow-glow on dark** — outer `box-shadow` in an accent colour. Replaced
   by lightness lift + inset rule.
4. **Section eyebrows as decoration** — an uppercase label above every section.
   Labels only where the section is genuinely a named region of a workspace.
5. **Tag-left / heading-right two-column section heads.** Vertical stack only.
6. **Italic headings.**
7. **`transition: all`**, bouncy easings, animated focus rings.
8. **Raw px** for colour, space, size, radius, or z-index. Tokens only.
9. **Two-line button labels.**
10. **Celebratory toasts.**

---

## Exports

### tokens.css

Lives at `app/static/tokens.css`, imported first by `style.css`. Colour tokens
are per-theme (four blocks); the scale tokens below are theme-invariant and
declared once on `:root`.

```css
:root {
  /* type */
  --text-2xs: 9px;  --text-xs: 10px;  --text-sm: 11px;  --text-md: 12px;
  --text-lg: 13px;  --text-xl: 15px;  --text-2xl: 19px;
  --text-stat: clamp(30px, 4vw, 44px);
  --track-label: 0.22em;  --track-name: 0.08em;  --track-body: 0;

  /* space */
  --space-3xs: 2px;  --space-2xs: 4px;  --space-xs: 6px;   --space-sm: 8px;
  --space-md: 12px;  --space-lg: 16px;  --space-xl: 24px;  --space-2xl: 36px;
  --space-3xl: 56px;

  /* shape */
  --radius-none: 0;  --radius-sm: 4px;  --radius-pill: 999px;
  --rule-hair: 1px solid var(--line);  --rule-mark: 2px;

  /* motion */
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in: cubic-bezier(0.7, 0, 0.84, 0);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --dur-instant: 80ms;  --dur-short: 140ms;  --dur-mid: 240ms;

  /* depth */
  --z-base: 1;  --z-raised: 10;  --z-dropdown: 100;  --z-sticky: 200;
  --z-modal: 400;  --z-toast: 500;  --z-tooltip: 600;
  --shadow-float: 0 10px 30px rgba(0, 0, 0, 0.45);
}
```
