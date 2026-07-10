# Feature Specification: Studio/Portal Publishing & Data Explorer

**Feature Branch**: none — predates this repository's git history; delivered pre-`git init` and landed whole in `f905a73` ("initial commit")

**Created**: 2026-07-09 (retroactively documented 2026-07-10)

**Status**: Implemented (retroactive spec, written from project history after the fact)

**Input**: Verbatim user request: *"I really love this. I think as a next
step it would be good to separate out this developer view from a dashboard
consumption type view. The former can stay largely as is. The latter would
be somewhere to publish the dashboards to when ready (and so this option
would need to be implemented in the developer view). I'm imagining you'd
have something like a nested folder structure so that you could publish a
dashboard to a folder, or subfolder, which users could navigate to. When you
finish that, can we also build a 'data explorer view' component so that you
can see which data files are available, and which models they belong to."*

## Provenance

Builds on dashboards from [001](../001-core-bi-platform/spec.md), views from
[002](../002-time-spine-dashboard-views/spec.md), and the interactive tile
behaviors from [003](../003-advanced-visuals-cross-filtering/spec.md) — the
portal is explicitly a *read-only consumption* wrapper around all of that,
not a reimplementation. The user's two asks are sequenced ("when you finish
that...") and are kept as separate priorities below in the same order.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Publish a dashboard into a navigable folder structure (Priority: P1)

From the studio, a dashboard's toolbar gets a PUBLISH action that prompts
for a slash-separated folder path (e.g. `ops/street`). Publishing shows a
live badge on the dashboard; republishing to a new path moves it;
unpublishing (or deleting the dashboard) removes it from the portal.

**Why this priority**: Nothing is consumable in the portal until something
is published — this is the enabling action for everything else in this
spec.

**Independent Test**: Publish a dashboard to a nested path, confirm it shows
a live badge with the path, republish it to a different path, confirm the
badge updates, then unpublish and confirm the badge disappears — all
independent of whether anyone views the portal.

**Acceptance Scenarios**:

1. **Given** an unpublished dashboard, **When** PUBLISH is used with a
   slash-separated path, **Then** the dashboard is marked live at that path
   with a visible badge, and any intermediate folder segments become
   navigable without a separate folder-creation step.
2. **Given** a published dashboard, **When** it is republished to a
   different path, **Then** it becomes reachable at the new path and is no
   longer reachable at the old one.
3. **Given** a published dashboard, **When** its live badge's unpublish
   control is used, **Then** it is removed from the portal while the
   dashboard itself (and its tiles/views) remains fully intact in the
   studio.
4. **Given** a published dashboard, **When** it is deleted from the studio,
   **Then** its publication is removed along with it — no orphaned portal
   entry remains.
5. **Given** any dashboard's presence in the sidebar list, **When** it is
   currently published, **Then** the list visibly marks it as such.

---

### User Story 2 - Browse and consume published dashboards read-only (Priority: P1)

A separate PORTAL surface — no builder chrome — lets a user navigate the
folder tree of published dashboards via breadcrumbs and open one. Inside, all
*editing* is disabled (no adding/removing tiles, no editing/persisting
filters, no rename/delete), while all *consumption* interactions (switching
saved views, the grain override, cross-filtering, tile focus/expand) keep
working exactly as in the studio — none of it persists.

**Why this priority**: This is the actual point of publishing — a
consumer needs to be able to find and use what was published without any
risk of altering it.

**Independent Test**: Seed a published dashboard directly, open the portal,
navigate to it via breadcrumbs, confirm no editing controls are present,
switch its saved view and apply a cross-filter, confirm both work visually,
then inspect the underlying store directly and confirm the dashboard's
saved `active_view` and filters are byte-for-byte unchanged from before the
portal visit.

**Acceptance Scenarios**:

1. **Given** the portal's folder browser, **When** a user navigates via
   breadcrumbs, **Then** they see only published dashboards, organized by
   the folder paths they were published to.
2. **Given** a dashboard opened in the portal, **When** the view renders,
   **Then** no add/remove-tile, filter-edit, rename, or delete controls are
   present, and each view's filters render as fixed (non-editable) chips.
3. **Given** a dashboard opened in the portal, **When** the user switches
   between its saved views, uses the grain override, clicks a mark to
   cross-filter, or expands a tile into focus mode, **Then** each behaves
   identically to the studio.
4. **Given** any consumption interaction performed in the portal, **When**
   the underlying dashboard/view record in storage is inspected afterward,
   **Then** it is unchanged — a portal visit can never drift a dashboard
   away from what was published.

---

### User Story 3 - Data explorer: map bucket files to models (Priority: P2)

A DATA surface lists every object in the configured bucket (size, modified
date) and matches each one against every model's source and join globs —
including Delta table internals mapping to their model as a unit — so a
developer can see at a glance what data exists and whether it's used.

**Why this priority**: Valuable for spotting orphaned data and confirming a
model's glob matches what's actually in the bucket, but it's a
developer-facing diagnostic, not something end users depend on the way they
depend on publishing.

**Independent Test**: Point the explorer at a bucket containing files used
by multiple models plus at least one file no model references, and confirm
the used files show their owning model(s) while the unused file is flagged
unmapped.

**Acceptance Scenarios**:

1. **Given** the DATA surface, **When** it loads, **Then** it lists every
   object in the bucket with its size and modified date, and a total
   object count/byte size for the bucket.
2. **Given** a model's `source` and `joins` globs, **When** an object
   matches one, **Then** that object is shown associated with that model
   (join-sourced files are visibly distinguished from base-source files).
3. **Given** a Delta-backed model, **When** its transaction log and
   part-files are listed, **Then** they are attributed to that model as a
   unit rather than appearing as a pile of unrelated unmapped files.
4. **Given** an object that matches no model's glob, **When** the explorer
   renders it, **Then** it is visibly flagged as unmapped.
5. **Given** a model card or a file's model chip, **When** it is clicked,
   **Then** the user is taken to the builder with that model selected.

### Edge Cases

- What happens when a dashboard is published to a path that collides with
  an existing published dashboard's exact path? Behavior must be defined
  (e.g. last-published-wins or a rejection) rather than silently showing
  two dashboards ambiguously at one path.
- What happens when a dashboard is republished to the *same* path it's
  already at? This must be a no-op, not an error.
- What happens when a folder path segment is empty (e.g. `ops//street`) or
  contains only whitespace? Must be rejected or normalized, not produce a
  broken breadcrumb.
- What happens when a portal user's browser session ends mid-interaction
  (cross-filter active, focus mode open)? On next load, the portal must
  show the dashboard exactly as published, with no leaked ephemeral state
  from the prior session.
- What happens when a bucket object's key matches more than one model's
  glob? The explorer must show it under every matching model, not just the
  first.
- What happens when the bucket is empty or unreachable? The explorer must
  show that state clearly rather than an empty list indistinguishable from
  "no unmapped files."

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a PUBLISH action on a dashboard that
  accepts a slash-separated folder path and marks the dashboard live at
  that path.
- **FR-002**: Folders MUST exist implicitly through published paths — there
  is no separate folder create/manage step.
- **FR-003**: A published dashboard MUST display a live badge indicating
  its path, with a control to unpublish it.
- **FR-004**: Republishing MUST move a dashboard from its old path to a new
  one; unpublishing or deleting the dashboard MUST remove it from the
  portal (deleting MUST also remove the publication record).
- **FR-005**: The system MUST provide a PORTAL surface, distinct from the
  studio, offering only folder/breadcrumb navigation of published
  dashboards — no model-editing or dashboard-editing chrome.
- **FR-006**: Opening a published dashboard in the portal MUST render it
  strictly read-only: no tile add/remove, no filter editing or renaming, no
  delete; each active view's filters MUST render as fixed, non-editable
  chips.
- **FR-007**: Consumption interactions available in the studio — saved-view
  switching, grain override, cross-filtering, and tile focus/expand — MUST
  also work in the portal.
- **FR-008**: No interaction performed within the portal MUST persist to
  the underlying dashboard, view, or visual records.
- **FR-009**: The system MUST provide a DATA surface listing every object
  in the configured bucket with size and modified date.
- **FR-010**: Each bucket object MUST be matched against every model's
  source and join globs, with Delta table internals (transaction log +
  part files) attributed to their owning model as a unit rather than as
  individual files.
- **FR-011**: Model cards in the explorer MUST show the model's glob(s),
  join sources, matched file count, and total bytes.
- **FR-012**: An object matching no model's glob MUST be visibly flagged as
  unmapped.
- **FR-013**: Clicking a model card or a file's model chip MUST navigate to
  that model pre-selected in the builder.
- **FR-014**: The application's primary navigation MUST expose STUDIO,
  PORTAL, and DATA as distinct top-level surfaces.

### Key Entities

- **Publication**: A {dashboard, folder path} record marking a dashboard
  live in the portal; removed on unpublish or dashboard delete.
- **Portal folder**: An implicit grouping derived from publication paths —
  not a stored entity of its own.
- **Bucket object**: A file in the configured S3-compatible bucket, with
  size, modified date, and zero or more matching models (by glob).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A dashboard published to a nested path (e.g. `ops/street`) is
  reachable via portal breadcrumb navigation at exactly that path, with no
  manual folder setup.
- **SC-002**: Republishing moves a dashboard's portal location with zero
  manual cleanup of the old path.
- **SC-003**: After any combination of view-switching, cross-filtering, and
  grain override inside the portal, direct inspection of the stored
  dashboard/view record shows it byte-for-byte unchanged from before the
  visit.
- **SC-004**: Every object in the bucket is accounted for as either mapped
  to at least one model or explicitly flagged unmapped — none are silently
  omitted from the explorer.
- **SC-005**: A developer can go from "I see a file in the explorer" to
  "I'm looking at its model in the builder" in a single click.

## Assumptions

- Publishing is a developer/studio-side action; there is no separate
  approval or review workflow before a dashboard goes live in the portal.
- The portal has no authentication/authorization boundary of its own in
  this spec — "read-only" is enforced by UI/API design, not by a user
  permission system (multi-tenant access control remains explicitly out of
  scope, consistent with [001](../001-core-bi-platform/spec.md)'s
  assumptions).
- Portal URLs are in-app navigation state, not necessarily deep-linkable
  from outside the app in this iteration.
