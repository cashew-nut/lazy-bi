# Tasks: Session Authentication & Role-Based Authorization

**Input**: Design documents from `specs/011-session-auth-rbac/`

**Prerequisites**: plan.md, spec.md (clarified), research.md, data-model.md, contracts/auth-api.md, quickstart.md

**Tests**: included — constitution Principle III makes tests mandatory for every feature.

**Organization**: grouped by user story; each story phase is an independently testable increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 from spec.md

---

## Phase 1: Setup

- [ ] T001 Add `argon2-cffi` to `requirements.txt` (pinned) and install into `.venv`; verify `from argon2 import PasswordHasher` imports

---

## Phase 2: Foundational (blocking prerequisites for all stories)

**⚠️ CRITICAL**: no user-story work until this phase completes.

- [ ] T002 [P] Add auth config to `app/config.py`: `CI_SESSION_IDLE_DAYS` (7), `CI_SESSION_MAX_DAYS` (30), `CI_COOKIE_SECURE` (0); delete `API_KEY`/`CI_API_KEY` (data-model.md "Configuration additions")
- [ ] T003 [P] Create `app/authstore.py` `AuthStore`: schema for `users`, `sessions`, `api_tokens`, `audit_events` per data-model.md (idempotent executescript, VisualStore idiom); user CRUD with username validation + case-insensitive uniqueness; last-active-admin invariant on role/active updates; session create/get(join users)/touch(60s throttle)/revoke/revoke_all_for_user with idle+absolute expiry; token create/list/revoke/lookup-by-hash; `record_audit()`; lockout fields update helpers
- [ ] T004 [P] Add guarded migration in `app/store.py`: `ALTER TABLE measure_provenance ADD COLUMN user_id INTEGER` (wrap in try/except or PRAGMA table_info check); extend `record_measure_provenance(user_id=None)` and `_provenance_to_dict` to expose `user_id` and `verified` (user_id is not None)
- [ ] T005 Rewrite `app/auth.py`: `User` dataclass (id, username, display_name, role, is_active); ordinal role compare (viewer<author<admin); Argon2id hash/verify with `check_needs_rehash`; `establish_session(user_id)` → (cookie value, row) storing SHA-256 (research R2); token verification (`cipat_` SHA-256 lookup, R5); login attempt gate (5 fails → 60s doubling to 15min cap, always-hash timing, R8); FastAPI deps `get_current_user` (reads `request.state.user`) and `require_role(role)`; delete `require_measure_author`
- [ ] T006 Add `AuthMiddleware` to `app/main.py` + wire `registry.auth_store` in `app/registry.py`: default-deny all `/api/*` except `POST /api/auth/login` and `GET /api/health` (401); resolve principal from Bearer token first, else `ci_session` cookie (R3/R5 precedence); on cookie-authenticated non-GET require `X-Requested-With: fetch` else 403 (R4); attach principal to `request.state`; init AuthStore in lifespan
- [ ] T007 Rework `tests/conftest.py`: app/client fixtures create viewer, author, admin accounts via AuthStore and expose logged-in `TestClient`s per role (cookie jar + CSRF header baked in) plus an anonymous client; keep existing moto/bucket fixtures working
- [ ] T008 Foundational tests in `tests/test_auth.py`: AuthStore CRUD + expiry math + last-admin invariant; hash/verify round-trip; session issue/revoke; token hash lookup; middleware 401 default-deny and CSRF 403 (spot routes)

**Checkpoint**: foundation ready — user stories can start.

---

## Phase 3: User Story 1 — Sign in once, then use the app under your role (P1) 🎯 MVP

**Goal**: every route requires identity; three roles enforced server-side; bootstrap admin keeps demo zero-config; SPA gains a login view.

**Independent test** (spec US1): fresh DB → bootstrap admin announced; every route refuses anonymous; role matrix holds for all three roles; session survives reload; sign-out ends it.

- [ ] T009 [US1] Create `app/api/auth.py` router per contracts/auth-api.md: `POST /api/auth/login` (423 lockout, identical 401s, Set-Cookie, audit `login`/`login_failed`/`lockout`), `POST /api/auth/logout` (revoke + clear cookie, audit), `GET /api/auth/me`, `POST /api/auth/password` (verify current, rehash, revoke other sessions, audit); register in `app/api/__init__.py`
- [ ] T010 [US1] Add `seed_bootstrap_admin()` to `app/seed.py` and call from lifespan in `app/main.py`: only when users table empty; username `admin`, `secrets.token_urlsafe(12)` password, prominent boxed log print, audit `bootstrap_admin_created` (research R9)
- [ ] T011 [P] [US1] Gate `app/api/models.py` per contract matrix: admin on `POST /models`, `PUT /models/{name}/yaml`, `DELETE /models/{name}`, `POST /models/reload`; author on `POST /models/generate` and measure save/delete routes (frame-payload check → admin, Principle VI); viewer floor elsewhere; measure endpoints take `user: User = Depends(require_role("author"))` and pass `user.display_name`/`user.id` to provenance
- [ ] T012 [P] [US1] Gate `app/api/dimensions.py`: admin on create/delete/`PUT yaml`/reload; author on `POST /dimensions/generate`; viewer floor on GETs + validate
- [ ] T013 [P] [US1] Gate `app/api/visuals.py` and `app/api/dashboards.py`: author on all POST/PUT/DELETE incl. `/publish`; viewer floor on GETs incl. `/portal`
- [ ] T014 [US1] Create `tests/test_role_matrix.py`: walk `app.routes`, assert every (route, method) × {anonymous, viewer, author, admin} verdict matches the contract matrix exactly — unknown/unlisted routes fail the test (SC-001/SC-002)
- [ ] T015 [US1] Extend `tests/test_auth.py` for US1: login/logout lifecycle, cookie flags (HttpOnly/SameSite=Lax), idle+absolute expiry, bootstrap admin created once and never re-seeded, lockout backoff + no username oracle, retired `X-API-Key`/`X-Author` requests → 401, audit rows written
- [ ] T016 [US1] Migrate existing suites to authed fixtures: `tests/test_api.py`, `tests/test_datasets.py`, `tests/test_measure_lab.py`, `tests/test_model_form.py`, `tests/test_static.py` (static stays public), `tests/test_store.py` (provenance signature) — full suite green
- [ ] T017 [US1] Frontend login: new `app/static/js/auth.js` (login view, `me` cache, logout, `canAuthor()`/`isAdmin()` helpers); `app/static/js/main.js` boots via `GET /api/auth/me` → login view on 401; `app/static/js/lib.js` `api()` adds `X-Requested-With: fetch` and global 401 → login view preserving JS state (research R10); user badge + logout in `app/static/index.html`; login styling in `app/static/style.css`
- [ ] T018 [US1] Hide/disable mutating UI outside role in existing modules (`builder.js` save, `dashboard.js` edit/publish, `measurelab.js` save, `modelling.js`/`editor.js`/`dimlab.js` admin-only): gate with auth.js helpers — server remains enforcer (FR-008)
- [ ] T019 [US1] Browser-verify US1 per quickstart.md §4 steps 1–5 (login view, sign-in, query loop unchanged, cold-reload persistence, viewer gating, sign-out) with screenshots + zero console errors

**Checkpoint**: US1 alone is a shippable MVP — the app is fully fail-closed.

---

## Phase 4: User Story 2 — Verified provenance for semantic-layer changes (P2)

**Goal**: provenance rows carry the verified account; legacy rows distinguishable; shared-secret path fully retired.

**Independent test** (spec US2): authed measure save with no legacy headers succeeds and records the account; header-only request refused; old rows readable and flagged.

- [ ] T020 [US2] Finish provenance plumbing in `app/api/models.py`: measure create/update/delete record `user_id=user.id, author=user.display_name`; `GET /models/{name}/measures/{m}/history` returns `verified` per row (store change landed in T004)
- [ ] T021 [P] [US2] Surface authorship in `app/static/js/measurelab.js`: history panel shows account display name with a "legacy (self-declared)" marker for unverified rows; remove any X-Author input remnants from the UI
- [ ] T022 [US2] Tests in `tests/test_auth.py` / `tests/test_store.py`: new rows have `user_id` + `verified:true`; pre-migration rows (insert with NULL) read back `verified:false`; version counter continuity across the upgrade; deactivated author's token/session can no longer save (ties to US3/US4 checks)

**Checkpoint**: attribution is real; spec-008 placeholder fully gone.

---

## Phase 5: User Story 3 — Admins manage accounts and roles (P3)

**Goal**: day-two operations — onboard, change role, deactivate, reset password — with the last-admin guard.

**Independent test** (spec US3): admin creates one user per role and each behaves per matrix; role change binds live sessions; deactivation kills sessions/tokens; non-admin refused; last admin protected.

- [ ] T023 [US3] Create `app/api/users.py` router per contract: `GET /api/users`, `POST /api/users` (409 duplicate, 422 shape), `PATCH /api/users/{id}` (subset updates; deactivate/password → revoke sessions; 409 on last-active-admin demote/deactivate; no DELETE route); admin-only via `require_role("admin")`; audit every action; register in `app/api/__init__.py`
- [ ] T024 [US3] Tests in `tests/test_auth.py`: full US3 acceptance — create per role + sign-in; live-session role change takes effect next request; deactivation 401s existing session and blocks sign-in; last-admin 409 (incl. self-demotion); author/viewer get 403 on all `/api/users` routes
- [ ] T025 [US3] Frontend admin panel: new `app/static/js/admin.js` (user list, create form, role select, activate/deactivate toggle, password reset) reachable from an admin-only nav entry in `app/static/index.html`; wire into `main.js` routing; style in `app/static/style.css`
- [ ] T026 [US3] Browser-verify US3 per quickstart.md §4 step 6 + step 9 (two-window role-change/deactivation check, last-admin refusal surfaced in UI)

**Checkpoint**: a team can actually be onboarded.

---

## Phase 6: User Story 4 — Personal access tokens for scripts (P4)

**Goal**: programmatic access re-established as per-user revocable tokens; shared key never returns.

**Independent test** (spec US4): author token saves a measure with correct attribution; revocation stops it next request; viewer token cannot mutate; secret shown once.

- [ ] T027 [US4] Add token endpoints to `app/api/users.py` per contract: `GET /api/tokens` (own, no secret), `POST /api/tokens` (secret in response only once), `DELETE /api/tokens/{id}` (own only, 404 otherwise); audit `token_created`/`token_revoked` (bearer resolution already lives in middleware from T006)
- [ ] T028 [US4] Tests in `tests/test_auth.py`: bearer auth on API routes; CSRF exemption for bearer; token-vs-cookie precedence (token wins); revoked token 401; owner deactivation kills tokens; viewer token 403 on mutations; provenance attributed to token owner (SC-005)
- [ ] T029 [US4] Frontend account panel in `app/static/js/admin.js` (or `auth.js`): "My tokens" list/create/revoke with one-time secret display + copy button; reachable for every role
- [ ] T030 [US4] Browser-verify US4 per quickstart.md §4 step 7 (create token, secret shown once, revoke)

**Checkpoint**: all four stories delivered.

---

## Phase 7: Polish & Cross-Cutting

- [ ] T031 [P] Update `README.md` as part of the feature (constitution workflow rule): auth section — roles, first-run bootstrap login, env vars (`CI_SESSION_*`, `CI_COOKIE_SECURE` behind TLS), personal access tokens for scripts, retired `CI_API_KEY`; refresh the architecture diagram line for auth
- [ ] T032 [P] Amend `.specify/memory/constitution.md` Principle VI: `frame:` path and raw YAML routes now gated by the admin role of a real identity system (supersedes `X-API-Key`+`X-Author` wording); bump version + amendment date (spec "Constitution Impact")
- [ ] T033 [P] Verify `docker-compose.yml` demo path: no new env required, bootstrap password visible in `docker compose up` output; add `CI_COOKIE_SECURE` passthrough comment
- [ ] T034 Run quickstart.md end-to-end: full pytest suite, curl walk-through (§3), upgrade-in-place on a pre-feature DB copy (§5), SC-007 spot-check that query latency is unchanged; fix anything found
- [ ] T035 Final browser golden path (quickstart.md §4 complete, both roles' UIs, zero console errors) — feature "done" gate per Principle IV

---

## Dependencies & Execution Order

```text
Phase 1 (T001)
  └─ Phase 2 (T002–T004 [P] → T005 → T006 → T007 → T008)
       └─ Phase 3 / US1 (T009–T010 → T011–T013 [P] → T014–T016 → T017–T018 → T019)  🎯 MVP
            ├─ Phase 4 / US2 (T020 → T021–T022)         — needs US1's gates on measure routes
            ├─ Phase 5 / US3 (T023 → T024–T026)         — needs US1's auth core only
            └─ Phase 6 / US4 (T027 → T028–T030)         — needs US1; T029 easier after T025's panel
                 └─ Phase 7 (T031–T033 [P] → T034 → T035)
```

- US2, US3, US4 are mutually independent once US1 lands (US4's UI task T029
  shares `admin.js` with US3's T025 — coordinate or sequence those two).
- Within Phase 2: T002/T003/T004 touch different files → parallel; T005
  needs T003; T006 needs T005; tests last.
- Within US1: T011/T012/T013 are different routers → parallel.

## Parallel Execution Examples

- **Foundational**: T002 (config) + T003 (authstore) + T004 (store migration) together.
- **US1 gating**: T011 (models) + T012 (dimensions) + T013 (visuals+dashboards) together after T009.
- **Polish**: T031 (README) + T032 (constitution) + T033 (compose) together.

## Implementation Strategy

**MVP = Phase 1 + Phase 2 + Phase 3 (US1)**: the app becomes fully
fail-closed with roles, bootstrap admin, and a working login UI — shippable
and independently valuable. Then US2 (provenance) is a small delta, US3
(user management) makes it team-ready, US4 (tokens) restores scripting.
Stop-and-ship is safe at every checkpoint; each story phase ends with its
own tests plus browser verification, so `main` never inherits a
half-enforced auth surface.
