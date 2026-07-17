# Feature Specification: Pre-Packed Theme Selector

**Feature Branch**: `013-theme-presets`

**Created**: 2026-07-17

**Status**: Draft

**Input**: User description: "Ship 4 pre-packed visual themes for the app (including the existing cyberpunk theme as one option) that users can swap between in settings. Componentize the current visual design system so theme values (colors, fonts, etc.) are centralized and swappable rather than hardcoded. Users pick from the 4 built-in themes; explicitly out of scope for this feature: custom theme editing/creation or uploading their own themes (may come later)."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Switch the app's look in one action (Priority: P1)

A user who is tired of (or loves) the neon cyberpunk look opens a theme picker, selects a different pre-packed theme, and immediately sees the entire app — navigation, panels, tables, forms, dialogs, and charts — reflect the new look. The next time they open the app in the same browser, their choice is still applied.

**Why this priority**: This is the entire point of the feature. Without it, nothing else matters. It's also the smallest slice that delivers real user value on its own (no account sync required).

**Independent Test**: Load the app, open the theme picker, select each of the 4 themes in turn, and confirm every screen updates instantly and consistently. Reload the browser and confirm the last-chosen theme is still active.

**Acceptance Scenarios**:

1. **Given** the app is open with the default theme active, **When** the user selects a different pre-packed theme from the picker, **Then** all visible UI (not just isolated elements) updates to the new theme's colors and fonts within one second, with no page reload.
2. **Given** a user has selected a non-default theme, **When** they close and reopen the app in the same browser, **Then** the previously selected theme is applied automatically without them needing to reselect it.
3. **Given** the theme picker is open, **When** the user views it, **Then** all 4 available themes are visibly distinguishable (e.g., via preview/label) before selection.

---

### User Story 2 - Theme choice follows a logged-in user across devices (Priority: P2)

A user picks a theme on their laptop, then later logs in on their phone or a different browser. Their chosen theme is applied there too, without having to reselect it.

**Why this priority**: Valuable for a consistent personal experience, but the app is still fully usable and the core feature still delivers value without it (User Story 1 covers the primary need).

**Independent Test**: Select a non-default theme while logged in on one browser session, then log in as the same user from a second, unrelated browser (or a private/incognito window) and confirm the same theme is applied there.

**Acceptance Scenarios**:

1. **Given** a logged-in user selects a theme, **When** they log in from a different browser or device, **Then** that same theme is applied automatically.
2. **Given** a logged-in user has different theme selections remembered locally on two different browsers, **When** they load the app in either browser, **Then** the system resolves to a single, deterministic theme (the most recently chosen one) rather than flickering between or randomly picking one.
3. **Given** the device is offline or account sync otherwise fails, **When** the user switches themes, **Then** the theme still switches immediately on that device, and the account-level sync is retried later without blocking or erroring visibly to the user.

---

### User Story 3 - Charts and data stay readable in every theme (Priority: P3)

A user viewing dashboards with multiple data series (charts, categories, legends) switches themes and can still clearly distinguish every series and read every value — the theme swap never makes data harder to interpret or breaks accessibility.

**Why this priority**: Protects the core analytical value of a BI tool. Lower priority than P1/P2 only because it's a quality bar on top of the switching mechanism itself, not a separate capability — but it is independently verifiable and must hold for every shipped theme.

**Independent Test**: For each of the 4 themes, open a dashboard containing a multi-series chart and confirm all series remain visually distinguishable and all text meets standard contrast/legibility expectations.

**Acceptance Scenarios**:

1. **Given** any of the 4 pre-packed themes is active, **When** a user views a chart with multiple data categories, **Then** every category's color remains distinguishable from every other (including for common forms of color vision deficiency) and from the background.
2. **Given** any of the 4 pre-packed themes is active, **When** a user reads any text in the app, **Then** text-to-background contrast meets the same accessibility bar the current default theme meets today.

---

### Edge Cases

- What happens when a first-time user (no local selection, no account preference) opens the app? They see the existing Cyberpunk look by default, unchanged from today's experience.
- What happens when browser storage is unavailable or blocked (e.g., strict private-browsing mode)? The app falls back to the default theme silently, without showing an error, and theme choice simply doesn't persist for that session.
- What happens if a user switches themes mid-session while a chart is actively rendering or updating? The chart redraws using the newly selected theme's palette without requiring a manual refresh.
- What happens if the account-level preference and the local browser preference disagree (e.g., user selected theme A on a device that was offline, then theme B elsewhere while online)? The most recently made selection wins.
- What happens if a future logged-out/guest session is used on a shared machine after another user selected a theme there? The locally remembered theme still applies (it's tied to the browser, not verified identity) until a different user logs in with their own account-level preference, which then takes over.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST ship exactly 4 pre-packaged visual themes, one of which is the existing "Cyberpunk" look, preserved as-is as an available option.
- **FR-002**: Users MUST be able to switch the active theme from within the app via a discoverable control, with the change applied immediately (no page reload required).
- **FR-003**: The selected theme MUST apply consistently across all screens and UI regions — navigation, panels, forms, tables, dialogs, and charts — not just isolated components.
- **FR-004**: System MUST remember a user's theme selection in their browser so it persists across page reloads and future visits in that same browser.
- **FR-005**: For authenticated users, System MUST also persist the theme selection to their account, so it is applied automatically when they log in from a different browser or device.
- **FR-006**: When a browser-local theme selection and an account-stored theme selection differ, System MUST resolve them deterministically by applying whichever was selected most recently.
- **FR-007**: System MUST default any user with no existing local or account theme selection to the Cyberpunk theme, preserving the app's current out-of-the-box visual identity.
- **FR-008**: Each of the 4 themes' data-visualization (chart) color palettes MUST independently meet the same accessibility bar (text/background contrast, colorblind-safe series separation) that the current Cyberpunk chart palette meets today.
- **FR-009**: The visual design system (colors, fonts, and other theme-relevant styling values) MUST be centralized into swappable, theme-scoped definitions rather than hardcoded per screen or component, such that introducing or adjusting a theme touches one centralized definition rather than requiring changes throughout the codebase.
- **FR-010**: System MUST NOT expose any capability to create, edit, customize, or upload user-defined themes as part of this feature — only selection among the 4 pre-packed themes is in scope.
- **FR-011**: If browser-local storage is unavailable, System MUST fall back to the default theme without displaying an error to the user.
- **FR-012**: If account-level sync of a theme selection fails (e.g., the user is offline), the locally selected theme MUST still apply immediately, and the system SHOULD retry the account sync later without blocking or interrupting the user.

### Key Entities

- **Theme**: A named, pre-packaged bundle of visual values (colors, fonts, and chart/data-visualization palette) with a unique identifier. Exactly 4 exist in this feature, one being the existing Cyberpunk theme.
- **Theme Preference**: A user's currently selected Theme. Exists in two forms that must reconcile: a browser-local copy (tied to a specific browser, always present once a selection is made) and an account-level copy (tied to the authenticated user, present only for logged-in users), each carrying enough information (e.g., a timestamp) to determine which was chosen most recently.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Selecting a different theme visibly re-skins the entire application in under 1 second, with no page reload.
- **SC-002**: A user's theme selection persists across 100% of subsequent visits in the same browser.
- **SC-003**: A logged-in user's theme selection is applied automatically the next time they log in from a different browser or device, without manual reselection.
- **SC-004**: All 4 shipped themes meet the same text-contrast and colorblind-safe chart-palette bar the current Cyberpunk theme meets today — verified for every theme, not just the default.
- **SC-005**: Existing users experience zero unintended visual change — the app still looks exactly as it does today (Cyberpunk) unless a user deliberately selects a different theme.
- **SC-006**: A new pre-packed theme can be introduced in the future by defining a single new theme entry, without needing to modify individual screens or components.

## Assumptions

- The 4 shipped themes are: the existing Cyberpunk theme (dark neon, unchanged) plus 3 new curated alternatives designed to appeal to users who prefer a different aesthetic (e.g., a light/clean theme, a muted professional dark theme, and a high-contrast/accessibility-focused theme). Exact names and palettes for the 3 new themes will be finalized during planning/design, not in this spec.
- Where the theme picker control lives (e.g., a settings area, a header toggle) is a design/planning decision; this spec only requires that switching themes is discoverable and takes one simple action, not a specific placement.
- Reconciliation between local and account-level theme preferences uses last-write-wins by timestamp, applied silently — no user-facing conflict prompt is needed.
- No real-time push sync across simultaneously open sessions on multiple devices is required; an account-level preference change is picked up the next time the app loads elsewhere, not instantly pushed to other open sessions.
- Persisting the account-level preference only requires a small addition to the existing authenticated user record (e.g., one new field), not a general-purpose user-preferences system.
- Any new fonts introduced by the 3 new themes are freely licensed and require no procurement/licensing process.
