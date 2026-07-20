# Feature Specification: Notebook Composer

**Feature Branch**: `claude/session-8zaqeq`
**Created**: 2026-07-20
**Status**: Implemented
**Input**: "Users chat a beautiful narrative page (html) into existence, including
live visuals created in the studio. The user can suggest tab vs collapsible
layouts, define explainer windows, and generally tinker with the design. A
composer UI lets users select a template, input narratives, select visuals —
then let AI do the rest. All designs utilise the hallmark skill."

## User Scenarios

- **US1 — Compose a page**: An author opens the Composer (home → notebooks
  panel → *+ compose a page*), picks a template, pastes a narrative, ticks the
  saved visuals/dashboards the page should build around, and issues an
  instruction. The AI drafts a complete notebook page; the right-hand proof
  pane renders it live (real queries, real charts) as it streams.
- **US2 — Tinker conversationally**: With a draft on screen, the author issues
  refinement instructions — "make the funnel section tabs instead of a
  collapsible", "add an explainer window beside the trend chart", "swap the
  split row's sides". Each turn returns the whole revised page; everything not
  asked about is preserved.
- **US3 — Save & revisit**: SAVE persists the draft through the existing
  notebooks CRUD (new page, or update when editing). Any notebook can be
  reopened in the composer later (✎ COMPOSER on the notebook view) and revised
  with full conversational context restored from the page itself.
- **US4 — Trust the output**: A viewer opening the published page sees only
  vetted markup: the platform re-validates every composed page server-side
  before it is ever handed back as a result.

## Functional Requirements

- **FR-001**: The composer is author-gated end to end (composing exists to
  author pages); it returns 503 when `CI_LLM_API_KEY` is unset, mirroring
  conversational analytics — no key, no third-party call, no feature.
- **FR-002**: `GET /api/composer/context` returns the template list and the
  embeddable catalog (saved visuals with their chart type/dimensions/measures,
  dashboards with their saved views) — the same catalog text shown to the LLM,
  so the picker and the prompt cannot disagree.
- **FR-003**: `POST /api/composer/compose/stream` performs one composition turn
  as SSE: display-only `thinking`/`html` events (the accumulating page, for the
  live typing preview), then a terminal `response` event carrying either the
  sanitized page (`outcome: composed` — name, html, summary, stripped) or
  `outcome: error` with a plain-language message. Turns are ephemeral: no
  conversation rows; the client re-sends `current_html` + bounded history.
- **FR-004**: The LLM is forced through a single typed tool call
  (`compose_page`), and its html is *unvalidated* until
  `sanitize_notebook_html` re-checks it: tag/attribute whitelist (no scripts,
  event handlers, inline styles, ids, or external resources — violations are
  stripped and reported in `stripped`), nb-tabs button/panel names must match,
  and every `data-visual-id` / `data-dashboard-id` must exist in the live
  registry **at compose time** — phantom ids fail the turn; nothing unchecked
  can be returned.
- **FR-005**: The page vocabulary is shared between hand-authors, the LLM, and
  the client hydrator: `nb-visual` (+`compact`), `nb-dashboard` (+`data-view`),
  `nb-tabs`, `nb-collapsible`, and two primitives added by this feature —
  `nb-explainer` (explainer windows: `data-title`, `data-tone` info/method/
  warn) and `nb-split` (claim | proof diptych rows). The seeded sample
  notebook demonstrates all of them, and a test asserts the sample passes the
  sanitizer clean so seed and gate can never drift apart.
- **FR-006**: Saving is exclusively via the existing notebooks CRUD — the
  composer endpoint never writes a notebook; drafts live client-side until the
  author explicitly saves. Leaving the composer with an unsaved draft prompts.
- **FR-007**: The composer prompt encodes the design rules distilled from the
  project's hallmark skill: structure serves the story (tabs for parallel
  threads, collapsibles for depth-on-demand, splits for claim+proof,
  explainers beside the chart they decode), macrostructure varies between
  pages, explicit layout requests are honored exactly, and **honest copy** —
  the narrative never states figures or trend directions the user didn't
  supply; the live charts carry the numbers.
- **FR-008**: Data egress per turn (documented in the README): instruction/
  narrative/history text, catalog names + declared query fields, and the
  current draft html. Result rows are never sent to the third party by the
  composer.

## Design

Hallmark run recorded in `.hallmark/log.json`; macrostructure **Split
Studio** (script | proof diptych), differing from the previous stamp
(Index-First). House tokens only — no new colors or fonts; the stamp lives
at the composer block in `app/static/style.css`.

## Testing

`tests/test_composer.py`: sanitizer unit contract (subtree drops, void-tag
regression, attribute stripping, phantom-id rejection, tab-name matching,
seeded-sample round-trip), prompt assembly, and the HTTP surface with a
scripted FakeComposer (role gates, 503-when-unconfigured, SSE event shape,
sanitize-before-return, save-through-CRUD). Role matrix extended with the
two new routes. UI verified end-to-end with Playwright against the live app
(compose → live preview hydration → save → reopen → seeded sample).
