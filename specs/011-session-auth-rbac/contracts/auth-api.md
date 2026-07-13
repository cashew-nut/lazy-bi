# API Contract: Auth Endpoints & Role Matrix

All endpoints are JSON under `/api`. Errors use FastAPI's standard
`{"detail": "..."}` shape. Status conventions:

- **401** — no/invalid/expired credentials (middleware). Also the answer
  to retired `X-API-Key`/`X-Author`-only requests.
- **403** — authenticated but insufficient role (`require_role`), or a
  cookie-authenticated mutation missing the CSRF header
  (`X-Requested-With: fetch`).
- **409** — business-rule refusals (e.g. would leave zero active admins).

Authentication carriers, resolved by the middleware in this order:
1. `Authorization: Bearer cipat_…` (personal access token) — wins if both
   are present; CSRF-exempt.
2. Session cookie `ci_session` (HttpOnly, SameSite=Lax, Path=/,
   Secure when `CI_COOKIE_SECURE=1`).

Public allowlist (no auth): `POST /api/auth/login`, `GET /api/health`,
`GET /` and `/static/*` (application code, no data).

## New endpoints: session (`app/api/auth.py`)

### POST /api/auth/login  — public
Request: `{"username": str, "password": str}`
- 200: `{"user": UserOut}` + `Set-Cookie: ci_session=…` (new session).
- 401: `{"detail": "invalid credentials"}` — identical for unknown user vs
  wrong password (FR-014); always performs one hash verification.
- 423: `{"detail": "account temporarily locked"}` — lockout active (R8).
- Deactivated accounts: 401 (indistinguishable from bad credentials).
- Audit: `login` / `login_failed` / `lockout`.

### POST /api/auth/logout — any authenticated (cookie)
- 204; revokes the presented session server-side and clears the cookie.
- Audit: `logout`.

### GET /api/auth/me — any authenticated
- 200: `UserOut` — `{"id", "username", "display_name", "role"}`.
- Frontend boot probe: 401 → render login view.

### POST /api/auth/password — any authenticated (cookie)
Request: `{"current_password": str, "new_password": str}`
- 204; verifies current password, rehashes, revokes **all other**
  sessions for the user (keeps the current one). Audit: `password_changed`.

## New endpoints: user management (`app/api/users.py`) — admin only

### GET /api/users
- 200: `[UserAdminOut]` — UserOut + `is_active`, `created_at`.

### POST /api/users
Request: `{"username", "display_name", "role", "password"}`
- 201: `UserAdminOut`. 409 on duplicate username. 422 on bad
  username/role/password shape. Audit: `user_created`.

### PATCH /api/users/{id}
Request (any subset): `{"display_name"?, "role"?, "is_active"?, "password"?}`
- 200: `UserAdminOut`.
- Role/deactivation changes bind immediately (session→user join).
- `is_active: false` or `password` set → revoke all sessions for that user.
- 409: change would leave zero active admins (FR-011) — applies to
  demoting or deactivating the last active admin, including self.
- No DELETE route exists — accounts are deactivate-only (clarified).
- Audit: `user_role_changed` / `user_deactivated` / `user_reactivated` /
  `password_reset`.

## New endpoints: personal access tokens (`app/api/users.py`) — any authenticated, own tokens only

### GET /api/tokens
- 200: `[{"id", "name", "created_at", "last_used_at", "revoked_at"}]` —
  never the secret.

### POST /api/tokens
Request: `{"name": str}`
- 201: `{"id", "name", "token": "cipat_…"}` — **secret appears only in
  this response** (FR-013). Audit: `token_created`.

### DELETE /api/tokens/{id}
- 204 (own token); 404 for another user's token id. Audit: `token_revoked`.

## Changed contracts on existing endpoints

- `POST/PUT/DELETE /api/models/{name}/measures[/{measure}]`: `X-API-Key`
  and `X-Author` headers **retired** — identity comes from the principal.
  Role: **author**, except a payload containing `frame`/`frame_emits`
  requires **admin** (Principle VI). Provenance rows record
  `user_id` + display name.
- `GET /api/models/{name}/measures/{measure}/history`: rows gain
  `"verified": bool` (false for legacy NULL `user_id` rows).
- Every other route: unchanged request/response shapes; new auth
  requirement per the matrix below.

## Role matrix (complete, enforced by tests/test_role_matrix.py)

Roles are nested: admin ⊇ author ⊇ viewer. "viewer" below means any
authenticated user.

| Route | Method(s) | Minimum role |
|---|---|---|
| `/api/health` | GET | public |
| `/api/auth/login` | POST | public |
| `/api/auth/logout`, `/api/auth/me`, `/api/auth/password` | POST/GET/POST | viewer |
| `/api/tokens`, `/api/tokens/{id}` | GET/POST/DELETE | viewer (own only) |
| `/api/users`, `/api/users/{id}` | GET/POST/PATCH | admin |
| `/api/query` | POST | viewer |
| `/api/models`, `/api/models/{name}/spec`, `/schema`, `/dimensions/{dim}/values` | GET | viewer |
| `/api/models/{name}/yaml` | GET | viewer |
| `/api/models/validate`, `/api/measures/check` | POST | viewer |
| `/api/models/{name}/measures/{m}/history` | GET | viewer |
| `/api/dimensions`, `/api/dimensions/{name}/spec`, `/{name}/yaml` | GET | viewer |
| `/api/dimensions/validate` | POST | viewer |
| `/api/visuals`, `/api/dashboards`, `/api/portal` (+`/{id}` GETs) | GET | viewer |
| `/api/explorer`, `/api/datasets`, `/api/datasets/schema` | GET | viewer |
| `/api/visuals` (+`/{id}`) | POST/PUT/DELETE | author |
| `/api/dashboards` (+`/{id}`) | POST/PUT/DELETE | author |
| `/api/publish`, `/api/publish/{dashboard_id}` | POST/DELETE | author |
| `/api/models/generate`, `/api/dimensions/generate` | POST | author |
| `/api/models/{name}/measures[/{m}]` (scalar payload) | POST/PUT/DELETE | author |
| `/api/models/{name}/measures[/{m}]` (frame payload) | POST/PUT | **admin** |
| `/api/models`, `/api/models/{name}/yaml`, `/api/models/{name}` | POST/PUT/DELETE | **admin** |
| `/api/dimensions`, `/api/dimensions/{name}/yaml`, `/api/dimensions/{name}` | POST/PUT/DELETE | **admin** |
| `/api/models/reload`, `/api/dimensions/reload` | POST | **admin** |

CSRF: every non-GET row above, when cookie-authenticated, additionally
requires `X-Requested-With: fetch` → else 403. Bearer-token requests are
exempt.

## UserOut schema

```json
{"id": 1, "username": "admin", "display_name": "Admin", "role": "admin"}
```
`UserAdminOut` adds `"is_active": true, "created_at": "…"`.
