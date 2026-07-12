# Feature Specification: Session Authentication & Role-Based Authorization

**Feature Branch**: `claude/app-auth-design-9q8m9i`

**Created**: 2026-07-12

**Status**: Draft

**Input**: User description: "Session-based authentication and role-based authorization for the app, replacing the placeholder shared-secret API key. Local username/password accounts as the demo-default login backend, architected behind a single identity seam so an OIDC login backend can be added later as a config change. Three roles mapped to the existing API surface (viewer / author / admin). API keys demoted to per-user personal access tokens resolving to the same identity. Demo mode stays frictionless via a seeded bootstrap admin. Also closes the existing gap where model/dimension YAML mutation routes bypass the measure-authoring gate from spec 008."

## Constitution Impact — Principle VI Re-Opened

Principle VI (Trusted-Config Security Boundary) states that any change to who
may reach the `frame:` escape hatch must re-open the principle explicitly.
This feature does so, deliberately:

- **Before**: the `frame:` path and model-measure saves are reachable by
  anyone presenting the single shared secret (`X-API-Key`) plus a
  self-declared, unverified `X-Author` label. Every other mutating route —
  including raw model/dimension YAML writes, which can themselves introduce
  `frame:` blocks — is reachable by **anyone who can reach the app at all**.
- **After**: every route requires an authenticated account. Raw model and
  dimension YAML authoring (the routes that can carry `frame:` blocks) and
  the model-measure `frame` construct require the **admin** role. Scalar
  model-measure authoring requires the **author** role. Provenance records a
  verified account identity, not a self-declared label.

The trust boundary is therefore *narrowed*, not widened: the set of actors
who can influence eval-capable configuration shrinks from "holder of one
shared secret" (frame saves) and "anyone" (raw YAML) to "accounts an admin
has explicitly granted the admin role." The constitution should be amended
alongside implementation to describe the role-based gate.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Sign in once, then use the app under your role (Priority: P1)

A person opens the app and, instead of landing directly in the query builder,
is asked to sign in with a username and password. After signing in they use
the app exactly as before — building queries, browsing dashboards — without
re-authenticating on every action, across page reloads, until they sign out
or the session expires. Every capability in the app is governed by their
assigned role: **viewers** can query and read; **authors** can additionally
save visuals, dashboards, and model measures; **admins** can additionally
edit raw model/dimension definitions and manage accounts. Anything outside
the user's role is refused by the server and hidden or disabled in the UI.

On a fresh install with no accounts, the system creates a bootstrap admin
account automatically and announces its credentials loudly in the startup
log, so the local demo keeps its zero-configuration start while exercising
the same sign-in flow as a real deployment.

**Why this priority**: This is the core exposure today — every mutating
route except three measure endpoints, and the entire query/read surface, is
open to anyone who can reach the app. Notably the raw YAML editing routes
bypass the spec-008 measure gate entirely. No other story in this feature is
meaningful until requests carry an identity and the server enforces roles.

**Independent Test**: Start with a fresh database; confirm a bootstrap admin
is created and announced; confirm every API route (read and write) refuses
unauthenticated requests; sign in and confirm the full role matrix — a
viewer account is refused on every mutating route, an author account is
refused on model/dimension YAML routes and user management, an admin
succeeds everywhere. Confirm the session survives a page reload and ends on
sign-out.

**Acceptance Scenarios**:

1. **Given** a running app and no session, **When** the browser requests any
   page or API route, **Then** the user is directed to sign in and no data
   or state is returned.
2. **Given** valid credentials, **When** the user signs in, **Then** they
   land in the app, their identity and role are visible in the UI, and
   subsequent requests succeed without re-entering credentials.
3. **Given** a signed-in viewer, **When** they attempt any state-changing
   action (directly against the API, bypassing the UI), **Then** the server
   refuses it with a clear authorization error and nothing is persisted.
4. **Given** a signed-in author, **When** they save a visual, dashboard, or
   scalar model measure, **Then** the save succeeds; **When** they attempt a
   raw model/dimension YAML write or a `frame` measure save, **Then** the
   server refuses it.
5. **Given** a fresh install with zero accounts, **When** the app starts,
   **Then** a bootstrap admin account exists, its credentials are printed
   prominently in the startup log with a warning to change them, and signing
   in with it works.
6. **Given** a signed-in user, **When** they sign out, **Then** the session
   is ended server-side and subsequent requests with the old session are
   refused.
7. **Given** a session that has expired, **When** the user makes their next
   request, **Then** they are asked to sign in again and, after doing so,
   can continue working.
8. **Given** a malicious page on another site attempting to trigger a
   state-changing request using a signed-in user's browser, **When** the
   request arrives, **Then** the server refuses it.

---

### User Story 2 - Semantic-layer changes are attributed to a verified identity (Priority: P2)

An author saves or edits a named model measure. The system records *who*
made the change from their signed-in identity — the self-declared
`X-Author` label and the shared `CI_API_KEY` secret are retired. The
existing provenance history (one row per save with author, expression,
timestamp, version) continues uninterrupted, but the author field is now a
verified account rather than free text.

**Why this priority**: Spec 008 built provenance on a placeholder identity
and documented it as such. Once real accounts exist (US1), keeping the
self-declared label would be a false claim of attribution. This is the
governance half of the feature.

**Independent Test**: With accounts in place, save a model measure as a
signed-in author without any `X-API-Key`/`X-Author` headers; confirm the
save succeeds and the provenance log records the account's identity.
Confirm a request presenting only the old shared-secret headers (no
session) is refused.

**Acceptance Scenarios**:

1. **Given** a signed-in author, **When** they save a model measure,
   **Then** no extra headers are required and the provenance row records
   their account identity and bumps the version, as today.
2. **Given** the old shared-secret headers and no session, **When** a
   measure save is attempted, **Then** it is refused — the shared secret no
   longer grants anything.
3. **Given** provenance history written before this feature (self-declared
   labels), **When** history is viewed, **Then** old rows remain readable
   and are distinguishable from verified-identity rows.

---

### User Story 3 - Admins manage accounts and roles (Priority: P3)

An admin creates accounts for teammates, assigns each a role, changes roles
as responsibilities change, deactivates accounts for people who leave, and
resets passwords. There is no self-service sign-up — this is an
internal-tool trust model where access is granted, not requested.

**Why this priority**: US1 requires accounts to exist, and the bootstrap
admin covers the demo. But a real deployment needs day-two operations —
onboarding, role changes, offboarding — before the feature is usable by a
team rather than a single person.

**Independent Test**: As admin, create a user with each role; confirm each
new user can sign in and their role matrix holds. Change a user's role and
confirm the change takes effect. Deactivate a user and confirm they can no
longer sign in and their existing sessions stop working. Confirm a
non-admin cannot reach any user-management capability.

**Acceptance Scenarios**:

1. **Given** a signed-in admin, **When** they create an account with a role
   and initial password, **Then** that person can sign in and operates
   under exactly that role.
2. **Given** an existing account, **When** an admin changes its role,
   **Then** the new role governs all subsequent requests, including
   requests on sessions that were already open.
3. **Given** an existing account, **When** an admin deactivates it, **Then**
   sign-in is refused and any live sessions or tokens for that account stop
   working promptly.
4. **Given** the system has exactly one active admin, **When** an attempt is
   made to delete, deactivate, or demote that account, **Then** the system
   refuses — the system can never be left with no active admin.
5. **Given** a signed-in author or viewer, **When** they attempt any
   user-management action, **Then** the server refuses it.

---

### User Story 4 - Scripts authenticate with personal access tokens (Priority: P4)

A user who automates against the API (seeding models, CI checks, saved-query
scripts) creates a personal access token from their account. The token
authenticates requests as that user with that user's role, can be revoked
individually, and replaces the retired shared `CI_API_KEY`. Tokens are shown
once at creation and stored only in unrecoverable (hashed) form.

**Why this priority**: Programmatic access exists today (the measure
endpoints are designed for it) and must not regress — but it's only needed
once interactive auth (US1) and identity (US2) are in place, and the local
demo doesn't depend on it.

**Independent Test**: Create a token as an author; call a measure-save
endpoint with only that token; confirm success and correct provenance
attribution. Revoke the token and confirm the same call is refused. Confirm
a viewer's token cannot mutate anything.

**Acceptance Scenarios**:

1. **Given** a signed-in user, **When** they create a personal access token,
   **Then** the secret is displayed exactly once and cannot be retrieved
   again afterwards.
2. **Given** a valid token presented on an API request with no session,
   **Then** the request is authenticated as the token's owner with the
   owner's role, and provenance attribution uses the owner's identity.
3. **Given** a revoked token or a token whose owner was deactivated,
   **When** it is presented, **Then** the request is refused.
4. **Given** a user with multiple tokens, **When** one is revoked, **Then**
   the others keep working.

---

### Edge Cases

- A user's session expires (or their account is deactivated) while they have
  unsaved work in the measure lab or model editor: the next save is refused
  with a clear re-authentication message, and re-signing-in must not
  destroy the in-browser draft.
- The last active admin cannot be deleted, deactivated, or demoted (US3
  scenario 4); the bootstrap admin counts as an admin for this rule.
- The bootstrap admin is only created when zero accounts exist — it must
  never be re-created (or its password reset) on later restarts, otherwise
  a leaked well-known credential reappears in production.
- Repeated failed sign-in attempts for one account are slowed or temporarily
  locked out, so the sign-in endpoint is not a free password-guessing oracle.
- Two sessions for the same account sign out or change password: ending one
  session must not end the other unless the password changed (password
  change ends all other sessions and revokes nothing else).
- Existing databases upgrade in place: pre-existing provenance rows,
  visuals, and dashboards survive; the app fails safe (everything requires
  sign-in) from the first post-upgrade start.
- A request carrying both a session cookie and an access token: exactly one
  well-defined precedence applies (token wins for API calls), never a merge
  of two identities.
- Health/liveness endpoint (if any) and static assets needed to render the
  sign-in page itself remain reachable without a session, and nothing else.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Every API route and app page MUST require an authenticated
  identity, with exactly two exceptions: the sign-in flow itself (and the
  static assets required to render it), and any operational liveness probe.
  Unauthenticated API requests are refused with an authentication error;
  unauthenticated page loads land on sign-in.
- **FR-002**: Users MUST be able to sign in with a username and password,
  remain signed in across page reloads and browser restarts within the
  session lifetime, and sign out, which ends the session server-side.
- **FR-003**: Passwords MUST be stored only in a form from which the
  original cannot be recovered, using a current-generation memory-hard
  password-hashing scheme.
- **FR-004**: Sessions MUST be represented in the browser only by an opaque
  identifier unreadable by page scripts, and MUST be revocable server-side
  (sign-out, deactivation, password change) with effect on the next request.
- **FR-005**: Sessions MUST expire after a configurable idle timeout
  (default 7 days) and a configurable absolute lifetime (default 30 days).
- **FR-006**: State-changing requests MUST be protected against cross-site
  request forgery; a state-changing request triggerable by a third-party
  site from a signed-in browser MUST be refused.
- **FR-007**: The system MUST support exactly three roles — viewer, author,
  admin — where each role includes everything the previous one can do:
  - **viewer**: run queries; read models, dimensions, visuals, dashboards;
    view published dashboards; validate (dry-run) measures and models.
  - **author**: create/update/delete visuals and dashboards;
    publish/unpublish dashboards; create/update/delete scalar model
    measures; generate model/dimension drafts (validation-only paths).
  - **admin**: create/update/delete models and dimensions including raw
    YAML writes; save `frame:` measures; trigger model/dimension reloads;
    manage users and roles.
- **FR-008**: The server MUST enforce the role matrix on every route
  independently of the UI; the UI MUST additionally hide or disable actions
  outside the signed-in user's role.
- **FR-009**: Model-measure provenance MUST record the authenticated
  identity of the saving account; the self-declared author label and the
  shared-secret credential MUST be retired (requests presenting only them
  are refused). Pre-existing provenance rows remain readable and
  distinguishable from verified rows.
- **FR-010**: Admins MUST be able to create accounts (username, display
  name, role, initial password), change roles, deactivate/reactivate
  accounts, and reset passwords. There is no self-service registration.
- **FR-011**: The system MUST refuse any operation that would leave zero
  active admin accounts.
- **FR-012**: On startup with zero accounts, the system MUST create a
  bootstrap admin and print its credentials with a prominent warning; it
  MUST NOT recreate or reset this account when any account already exists.
- **FR-013**: Users MUST be able to create named personal access tokens,
  see the secret exactly once, list their tokens (name, created, last used),
  and revoke them individually. Tokens are stored only in unrecoverable
  form, authenticate API requests as their owner with the owner's role, and
  die with the owner's deactivation.
- **FR-014**: Repeated failed sign-in attempts against an account MUST be
  rate-limited or temporarily locked out, and failed attempts MUST NOT
  reveal whether the username exists.
- **FR-015**: Sign-ins, sign-outs, failed sign-in attempts, account
  management actions, and token creation/revocation MUST be recorded in a
  reviewable audit log.
- **FR-016**: Existing databases MUST upgrade in place with all existing
  visuals, dashboards, publications, and provenance intact, and the
  upgraded system fails safe (all access requires sign-in) immediately.
- **FR-017**: The identity layer MUST be structured so an additional
  external sign-in method (e.g. organization single sign-on) can be added
  later purely as a new way of *establishing* a session, without changing
  how any route *consumes* identity or roles. Local passwords remain the
  default and the demo path.

### Key Entities

- **User account**: a person's identity — username, display name, role
  (viewer/author/admin), active/deactivated flag, password (unrecoverable
  form). Created and governed by admins; one special bootstrap admin is
  system-created on first run.
- **Session**: a server-tracked sign-in — owning user, created/last-seen
  timestamps, expiry. Ends by sign-out, expiry, deactivation, or password
  change. The browser holds only an opaque reference.
- **Personal access token**: a named, per-user API credential — owner,
  name, secret (unrecoverable form), created/last-used timestamps, revoked
  flag. Grants exactly the owner's role.
- **Role**: one of viewer/author/admin; strictly nested capability sets
  mapped over the existing API surface (see FR-007).
- **Provenance record** *(existing, amended)*: per-save history of model
  measures; the author field becomes a reference to a verified user
  account, with legacy self-declared rows preserved and flagged as such.
- **Audit event**: security-relevant occurrences (sign-in/out, failures,
  account/token management) — actor, action, target, timestamp.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of API routes (read and write) refuse unauthenticated
  requests, with the sole exceptions of the sign-in flow and liveness probe
  — verified by an exhaustive route sweep in the test suite.
- **SC-002**: The role matrix holds with zero exceptions: an automated
  sweep exercising every mutating route as viewer, author, and admin
  produces exactly the refusals and successes FR-007 prescribes.
- **SC-003**: A fresh demo still starts with zero configuration: from
  `docker compose up` (or `./run.sh`) to signed-in-and-querying in under
  two minutes using only what the startup log prints.
- **SC-004**: Every semantic-layer measure change made after upgrade is
  attributable to a verified account — zero new provenance rows with
  free-text authors.
- **SC-005**: A script holding a valid personal access token performs the
  same measure-authoring calls that worked under spec 008, with no session,
  and revoking the token stops it on the next request.
- **SC-006**: Deactivating an account renders its sessions and tokens
  unusable on their very next request.
- **SC-007**: Query latency is unchanged within measurement noise: the
  per-request identity check adds no user-perceivable delay to the demo's
  interactive query loop.

## Assumptions

- **Internal-tool trust model**: access is provisioned by admins; there is
  no self-service sign-up, email verification, or password-recovery flow
  (admins reset passwords). Reasonable for a BI tool deployed to a known
  team; consumer-style account lifecycle is out of scope.
- **No anonymous access at all in this feature**: published dashboards are
  visible to any *signed-in* user (viewer and up), not to the public.
  Anonymous/link-based public sharing is a possible future feature and is
  explicitly out of scope here.
- **Single sign-on is out of scope but designed-for**: this feature ships
  local username/password only; FR-017 requires the seam that makes an
  OIDC/SSO backend a later additive change.
- **Session lifetime defaults** (7-day idle, 30-day absolute) are sensible
  for an internal tool and configurable; no "remember me" checkbox — the
  defaults already behave that way.
- **Everyone signs in — including the demo**: demo mode gets a seeded
  bootstrap admin rather than an auth bypass, so the demo exercises the
  production code path (mirrors the project's embedded-S3-emulator
  pattern). A documented environment toggle to disable auth entirely is
  deliberately **not** provided, to avoid a footgun that ships to
  production.
- **Existing single-writer architecture is respected**: accounts, sessions,
  and tokens persist in the app's existing store alongside visuals and
  dashboards; nothing here requires a second writer or an external service.
- **Scale-out path**: when the app scales beyond one process against an
  external S3 endpoint, session storage must be shareable or swappable;
  the design must keep session persistence behind a narrow seam so this is
  a swap, not a rewrite (design detail for the plan phase).
- **MinIO/second-instance compose profile** keeps working: auth
  configuration is per-instance and self-contained.
