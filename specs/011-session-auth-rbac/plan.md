# Implementation Plan: Session Authentication & Role-Based Authorization

**Branch**: `claude/app-auth-design-9q8m9i` | **Date**: 2026-07-12 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/011-session-auth-rbac/spec.md`

## Summary

Replace the spec-008 placeholder shared-secret gate with real identity:
local username/password accounts stored in the existing SQLite database
(Argon2id hashing), server-side sessions carried by an HttpOnly
`SameSite=Lax` cookie, and three nested roles (viewer / author / admin)
enforced centrally. Authentication is enforced by an ASGI middleware that
default-denies every `/api` route except a tiny allowlist (login, health);
authorization is per-route via `require_role` dependencies. Personal access
tokens (hashed at rest) give scripts the same identity seam. Provenance
records the verified account; `CI_API_KEY`/`X-Author` are retired. A
bootstrap admin with a random, once-printed password keeps the demo
zero-config. The design keeps a single narrow seam — "a login backend
establishes a session; everything else consumes sessions" — so OIDC can be
added later without touching any route.

## Technical Context

**Language/Version**: Python 3.10+ (Docker image: python:3.12-slim)

**Primary Dependencies**: FastAPI 0.139 / Starlette (middleware, cookies),
uvicorn; **new**: `argon2-cffi` for password hashing (see research.md R1).
Frontend remains vanilla ES modules, no build step.

**Storage**: existing SQLite database (`cash_intel.db`) — new tables
`users`, `sessions`, `api_tokens`, `audit_events`; guarded `ALTER TABLE` on
`measure_provenance` (add nullable `user_id`). Session persistence behind a
narrow `AuthStore` interface so a Redis/Postgres swap is localized (R6).

**Testing**: pytest + FastAPI TestClient (existing pattern in `tests/`);
new `tests/test_auth.py` plus an exhaustive route-table sweep asserting the
role matrix (SC-001/SC-002); existing suites updated to authenticate.

**Target Platform**: Linux server / Docker, single uvicorn worker (by
design), same-origin browser frontend.

**Project Type**: web service + static SPA (existing structure).

**Performance Goals**: SC-007 — no user-perceivable added latency on the
interactive query loop; per-request auth is one indexed SQLite read
(session+user join), with `last_seen` writes throttled to once/60s.

**Constraints**: single SQLite writer (no external session service);
zero-config demo start must survive (bootstrap admin, `Secure` cookie flag
off by default because the demo runs on plain HTTP); no bundler — login UI
is another vanilla ES module view inside the existing SPA.

**Scale/Scope**: internal-tool scale — tens of accounts, a handful of
concurrent sessions; ~9 new/changed backend modules, ~4 frontend modules,
2 new routers, role matrix over ~35 existing routes.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Status | Notes |
|---|-----------|--------|-------|
| I | Semantic layer is the only contract | PASS | No query-path or model-contract changes; auth wraps routes, never data access. |
| II | Lazy evaluation, pushdown | PASS | Data path untouched. Auth adds one indexed SQLite lookup per request, off the Polars path (SC-007 guards this). |
| III | Every feature ships with tests | PASS (planned) | New auth suite + route-matrix sweep; existing API tests gain an authenticated client fixture. |
| IV | Browser-verified before done | PASS (planned) | quickstart.md defines the browser golden path: login → query → role-gated UI → logout → cold reload. |
| V | Ephemeral vs persisted is deliberate | PASS | Persisted: users, sessions, tokens, audit events (SQLite). Ephemeral: login-form state, in-browser drafts across re-auth (spec edge case). Stated in data-model.md. |
| VI | Trusted-config boundary explicit | PASS — **explicitly re-opened by the spec** | Boundary narrows: `frame:` saves and raw YAML routes move from "shared secret"/"anyone" to the admin role. Constitution amendment ships with implementation (tracked as a task). |
| VII | Feature branch, PR merge | PASS | All work on the designated feature branch `claude/app-auth-design-9q8m9i`. |
| — | Technology constraints | PASS | One router per resource (`app/api/auth.py`, `app/api/users.py`); SQLite stays the persistence store; vanilla ES modules; single worker preserved. |

No violations → Complexity Tracking not required.

**Post-Phase-1 re-check (2026-07-12)**: design artifacts introduce no new
violations; middleware default-deny strengthens Principle VI rather than
widening it. Gate: PASS.

## Project Structure

### Documentation (this feature)

```text
specs/011-session-auth-rbac/
├── spec.md              # Feature spec (clarified)
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── auth-api.md      # Phase 1 output: endpoints + role matrix
├── checklists/
│   └── requirements.md  # Spec quality checklist (passing)
└── tasks.md             # Phase 2 output (/speckit-tasks — not this command)
```

### Source Code (repository root)

```text
app/
├── auth.py              # REWRITTEN: User principal, get_current_user,
│                        #   require_role, password hashing, session issue/
│                        #   revoke, token verification, login rate limiting
├── authstore.py         # NEW: AuthStore — users/sessions/tokens/audit
│                        #   tables on the same SQLite file; the session-
│                        #   persistence seam (R6)
├── config.py            # + session lifetimes, cookie-secure flag,
│                        #   bootstrap toggle; CI_API_KEY removed
├── main.py              # + AuthMiddleware (default-deny /api), bootstrap
│                        #   admin seeding in lifespan
├── registry.py          # + auth_store handle
├── store.py             # measure_provenance: guarded ALTER TABLE user_id
├── seed.py              # + seed_bootstrap_admin() (random pw, loud print)
└── api/
    ├── __init__.py      # + auth, users routers
    ├── auth.py          # NEW: login / logout / me
    ├── users.py         # NEW: admin user mgmt + own-token CRUD
    ├── models.py        # role gates; frame→admin; author from principal
    ├── dimensions.py    # role gates (admin mutations)
    ├── visuals.py       # role gates (author mutations)
    └── dashboards.py    # role gates (author mutations incl. publish)

app/static/
├── index.html           # + login view container, user badge, admin nav
├── js/
│   ├── lib.js           # api(): CSRF header on mutations, 401 → login
│   ├── auth.js          # NEW: login view, me-cache, logout, role helpers
│   ├── admin.js         # NEW: user management + token management views
│   └── main.js          # boot: check /api/auth/me before rendering app
└── style.css            # login + admin styling

tests/
├── conftest.py          # authed-client fixtures per role
├── test_auth.py         # NEW: login/logout/session/CSRF/lockout/tokens/
│                        #   bootstrap/audit
├── test_role_matrix.py  # NEW: exhaustive route-table sweep (SC-001/002)
└── test_api.py …        # updated to use authed fixtures
```

**Structure Decision**: stay inside the existing single-app layout — one
new store module, two new routers under `app/api/` (per-resource-router
rule), auth logic consolidated in the rewritten `app/auth.py`, and the
frontend grows two vanilla ES modules. No new packages, services, or
build steps.

## Complexity Tracking

No constitution violations to justify.
