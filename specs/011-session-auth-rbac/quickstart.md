# Quickstart Validation: Session Authentication & RBAC

Runnable scenarios proving the feature end-to-end. Contracts:
[contracts/auth-api.md](contracts/auth-api.md); entities:
[data-model.md](data-model.md).

## Prerequisites

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

## 1. Automated suites (fast signal)

```bash
.venv/bin/python -m pytest tests/                    # full suite
.venv/bin/python -m pytest tests/test_auth.py tests/test_role_matrix.py -v
```

Expected: green, including the exhaustive route sweep (every route ×
{anonymous, viewer, author, admin} → exactly the verdicts in the contract
matrix — SC-001/SC-002).

## 2. Fresh-start bootstrap (SC-003, FR-012)

```bash
rm -f cash_intel.db && ./run.sh
```

Expected in startup log: a prominent block announcing the bootstrap
`admin` account with a **random** password, printed once. Restart the app:
the block must NOT reappear and the old password must still work
(bootstrap never re-runs).

## 3. API walk-through (curl)

```bash
# Anonymous is refused everywhere except login/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/models          # 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/health          # 200

# Login (password from the startup log), keep the cookie jar
curl -s -c /tmp/cj -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<printed>"}' http://127.0.0.1:8080/api/auth/login

# Session works; CSRF header required on mutations
curl -s -b /tmp/cj http://127.0.0.1:8080/api/auth/me                               # 200 admin
curl -s -b /tmp/cj -X POST http://127.0.0.1:8080/api/models/reload \
  -o /dev/null -w '%{http_code}\n'                                                 # 403 (no CSRF header)
curl -s -b /tmp/cj -X POST -H 'X-Requested-With: fetch' \
  http://127.0.0.1:8080/api/models/reload -o /dev/null -w '%{http_code}\n'         # 200

# Retired spec-008 headers grant nothing
curl -s -X POST -H 'X-API-Key: anything' -H 'X-Author: mallory' \
  -H 'Content-Type: application/json' -d '{"expr":"sum(fare)"}' \
  http://127.0.0.1:8080/api/models/nyc_taxi/measures -o /dev/null -w '%{http_code}\n'  # 401
```

Role matrix spot-checks (create users via `POST /api/users` as admin, then
log in as each):

- viewer → `POST /api/query` 200; `POST /api/visuals` 403.
- author → `POST /api/visuals` 201; `PUT /api/models/nyc_taxi/yaml` 403;
  measure save with a `frame` payload 403.
- admin → all of the above 2xx.

Provenance: save a measure as author, then
`GET /api/models/<m>/measures/<name>/history` shows the account identity
with `"verified": true`; pre-upgrade rows show `"verified": false`.

Tokens: `POST /api/tokens` as author → secret shown once; a bearer-token
measure save succeeds **without** CSRF header and attributes provenance to
the owner; `DELETE /api/tokens/{id}` → next bearer call 401 (SC-005).

Lockout: 5 wrong passwords → 423 with retry window; correct password after
window → 200 (FR-014). `sqlite3 cash_intel.db 'SELECT action, actor_label
FROM audit_events'` shows the trail (FR-015).

## 4. Browser golden path (Principle IV — required before "done")

1. Open `http://127.0.0.1:8080` in a fresh profile → login view renders
   (no app shell, no data fetched). Zero console errors.
2. Sign in as admin → app shell renders; user badge shows name and role.
3. Run a query in the builder; save a visual; create a dashboard —
   unchanged behavior (SC-007: no perceptible slowdown).
4. **Cold reload** → still signed in (session cookie survives).
5. As a viewer account (created via admin UI): mutating controls (save
   visual, model editing, measure lab save) hidden/disabled; direct API
   attempt from devtools returns 403.
6. Admin UI: create user, change role (verify it binds on the *open*
   session of that user in a second window), deactivate → that window's
   next request bounces to login.
7. Account panel: create a token (secret shown once), revoke it.
8. Sign out → back to login; cold reload stays logged out.
9. Attempt to demote/deactivate the last active admin → clear refusal in
   UI (409 surfaced).

## 5. Upgrade-in-place check (FR-016)

Run against a pre-feature `cash_intel.db` (or the `app-data` Docker
volume): app starts, tables auto-create, `measure_provenance` gains
`user_id`, existing visuals/dashboards/publications intact, bootstrap
admin seeded **only if** the users table is empty, and every route demands
sign-in immediately.

## 6. Docker demo (SC-003)

```bash
docker compose up   # http://127.0.0.1:8080
```

Zero-to-signed-in under two minutes using only the password printed in
`docker compose` output. `CI_COOKIE_SECURE` stays unset (plain-HTTP demo);
README documents setting it to `1` behind TLS.
