# Research: Session Authentication & Role-Based Authorization

All Technical Context unknowns resolved. Each decision below records what
was chosen, why, and what was rejected.

## R1 — Password hashing: `argon2-cffi` (Argon2id)

**Decision**: Add `argon2-cffi` and use `PasswordHasher` defaults
(Argon2id, RFC 9106-aligned parameters). Store the full encoded hash
string (parameters + salt embedded), enabling transparent parameter
upgrades via `check_needs_rehash` on successful login.

**Rationale**: Argon2id is the Password Hashing Competition winner and the
current OWASP first choice; `argon2-cffi` is the maintained canonical
binding with zero transitive Python deps. FR-003 demands a
current-generation memory-hard scheme.

**Alternatives considered**:
- *stdlib `hashlib.scrypt`* — zero new dependency and memory-hard, but
  hand-rolling salt/parameter encoding and upgrade logic is exactly the
  kind of crypto plumbing worth outsourcing; rejected for maintainability.
- *passlib* — effectively unmaintained (no release since 2020, warns on
  Python 3.13); rejected.
- *bcrypt* — not memory-hard, 72-byte truncation footgun; rejected.

## R2 — Session mechanism: opaque server-side sessions, HttpOnly cookie

**Decision**: Session ID = `secrets.token_urlsafe(32)`; browser stores it
in an `HttpOnly; SameSite=Lax; Path=/` cookie (`Secure` when
`CI_COOKIE_SECURE=1`). Server stores **SHA-256 of the ID** in a `sessions`
row (user, created_at, last_seen, expiry inputs) — a DB leak exposes no
usable session credentials. Idle (7d) and absolute (30d) expiry checked on
every lookup; `last_seen` writes throttled to once per 60s to keep the
single-writer SQLite quiet on hot query loops.

**Rationale**: FR-004 requires script-unreadable, server-revocable
sessions — opaque server-side rows give instant revocation (sign-out,
deactivation, password change, role change takes effect on next request via
the session→user join). Same-origin no-build SPA makes cookies strictly
simpler and safer than tokens held in JS.

**Alternatives considered**:
- *JWT / signed stateless cookies* — no per-request DB read and trivially
  multi-worker, but revocation and role changes need denylists or short
  TTL+refresh machinery; violates FR-004's "revocable with effect on next
  request" cleanly. Rejected.
- *Starlette SessionMiddleware (signed cookie)* — same revocation problem,
  plus data lives client-side. Rejected.

## R3 — Enforcement: default-deny middleware + per-route role dependencies

**Decision**: Two layers. (1) An ASGI middleware authenticates every
`/api/*` request (cookie or bearer token) and returns 401 unless the path
is on an explicit public allowlist: `POST /api/auth/login`,
`GET /api/health`. It attaches the resolved `User` principal to
`request.state`. (2) Authorization is per-route:
`Depends(require_role("author"))` / `"admin"`, with plain authenticated
access (viewer) as the default floor. `GET /` and `/static/*` stay public
(code, not data); the SPA renders its login view when `/api/auth/me`
returns 401.

**Rationale**: SC-001 demands 100% of routes fail closed — a forgotten
`Depends` must not create a hole, so authentication cannot rely on
route-by-route opt-in. Role gates differ per route, so they stay
declarative at the route. A `tests/test_role_matrix.py` sweep walks
`app.routes` and asserts the expected verdict for every (route, role)
pair, so the matrix is enforced by test, not by review.

**Alternatives considered**:
- *Dependencies only, no middleware* — a missed dependency silently ships
  an open route; rejected as the exact failure mode this feature exists to
  close (the spec-008 YAML-route gap).
- *Middleware does roles too (path→role table)* — centralizes everything
  but duplicates FastAPI's routing (path params, methods) in fragile
  string matching; rejected.

## R4 — CSRF: SameSite=Lax + required custom header on mutations

**Decision**: Cookie-authenticated non-GET `/api` requests must carry
`X-Requested-With: fetch` or be rejected 403 by the middleware.
Token-authenticated (Authorization header) requests are exempt — attackers
cannot set that header cross-site. The frontend adds the header in the
single `api()` helper in `lib.js`.

**Rationale**: `SameSite=Lax` already blocks cross-site POSTs from modern
browsers; the custom-header requirement adds defense-in-depth (covers
old-browser edge cases and any future GET-with-side-effects mistake path)
at the cost of one line in one shared helper. No token generation, storage,
or rotation machinery.

**Alternatives considered**: synchronizer/double-submit CSRF tokens —
strictly more moving parts for no additional protection given same-origin
fetch + custom header; rejected.

## R5 — Personal access tokens: prefixed opaque secrets, SHA-256 at rest

**Decision**: Token = `cipat_` + `secrets.token_urlsafe(32)`, shown once.
Store SHA-256 digest with owner, name, created_at, last_used_at,
revoked_at; lookup is a single indexed read of the digest. Presented as
`Authorization: Bearer <token>`; resolves to the owner's `User` principal
through the same middleware. Precedence per spec edge case: if both a
bearer token and a session cookie arrive, the token wins.

**Rationale**: FR-013 (unrecoverable at rest, revocable individually,
owner's role). SHA-256 (not Argon2) is correct here: token secrets are
high-entropy random strings, so brute-force resistance comes from entropy,
and hashing must be cheap enough for every API call. The prefix makes
tokens recognizable in secret scanners.

**Alternatives considered**: reusing session rows for tokens — conflates
lifetimes (sessions expire, tokens live until revoked) and audit
semantics; rejected.

## R6 — Session/identity persistence seam: `AuthStore` on the same SQLite file

**Decision**: New `app/authstore.py` `AuthStore` class owning `users`,
`sessions`, `api_tokens`, `audit_events` tables against the existing
`config.DB_PATH`, mirroring `VisualStore`'s idiom (executescript schema,
`IF NOT EXISTS`, row→dict mappers). Session methods
(`create_session/get_session/touch/revoke/revoke_all_for_user`) form the
documented swap seam for a future Redis/Postgres-backed store when the app
scales past one process. Registry exposes `registry.auth_store`.

**Rationale**: single-writer constraint says same DB file; separation from
`VisualStore` keeps content persistence and identity persistence
independently evolvable, which is precisely the seam FR-017 and the
scale-out assumption require.

**Alternatives considered**: growing `VisualStore` — fewer files but welds
identity to content storage, making the later swap a surgery; rejected.

## R7 — Login backend seam for future OIDC

**Decision**: `app/auth.py` exposes `establish_session(user_id) ->
(cookie_value, session)` used by the password login route. A future OIDC
backend is a new router (`/api/auth/oidc/*`) that resolves/provisions a
user and calls the same function. Config selects enabled backends. Nothing
downstream of session creation changes. Documented in code and README; no
OIDC code ships now (FR-017).

## R8 — Login rate limiting: per-account counters in the users table

**Decision**: `failed_attempts` + `locked_until` columns. 5 consecutive
failures → 60s lock, doubling per subsequent failure, capped at 15 min;
any success resets. Responses for bad-username and bad-password are
identical 401s with identical timing shape (always run one hash
verification), satisfying FR-014's no-username-oracle clause.

**Alternatives considered**: in-memory dict (lost on restart, wrong once
multi-process); IP-based limits (NAT-unfair, spoofable behind proxies).
Rejected.

## R9 — Bootstrap admin & migration

**Decision**: In lifespan, after stores init: if `users` is empty, create
`admin` with `secrets.token_urlsafe(12)` password, print prominently
(mirrors the seed-bucket pattern). Never re-runs once any account exists
(spec edge case). Existing DBs upgrade via `IF NOT EXISTS` tables plus one
guarded `ALTER TABLE measure_provenance ADD COLUMN user_id INTEGER`
(nullable; legacy rows stay NULL → rendered as "legacy, self-declared").
`CI_API_KEY` and `X-Author` handling deleted; requests presenting only
them get the middleware's plain 401.

## R10 — Frontend integration

**Decision**: `main.js` boots by calling `/api/auth/me`; 401 renders the
login view (new `auth.js` module) instead of the app shell. `lib.js
api()` gains the CSRF header and a global 401 handler that swaps to the
login view while preserving in-memory drafts (spec edge case: re-auth must
not destroy unsaved work — the draft lives in JS state, login happens in
the same page context, no navigation). Role helpers (`canAuthor()`,
`isAdmin()`) hide/disable mutating UI; the server remains the enforcer
(FR-008). New `admin.js` renders user management and personal-token
views, following the existing hand-rolled module/view pattern.
