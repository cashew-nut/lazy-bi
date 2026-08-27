# Auth & Security

**Source:** `app/auth.py` (191 lines) · `app/authstore.py` (364 lines) ·
`AuthMiddleware` in `app/main.py`

Everything requires a signed-in identity — there is no anonymous mode, even
in the zero-config demo, so the demo exercises the exact same auth path a
real deployment does. This page covers identity, sessions, roles and CSRF;
for the separate question of what an *authored SQL construct* is allowed to
reach regardless of role, see
[the trust boundary](#the-trust-boundary-principle-vi) below and
[Query Engine → The SQL grammar](query-engine.md#the-sql-grammar).

## Identity model

`app/auth.py` is the identity core — a login backend (today: username/
password) *establishes* a session, and everything downstream only ever
*consumes* it; routes never know how a user signed in. A future SSO/OIDC
backend would be a sibling that resolves a user and calls the same
`establish_session()` — nothing else changes.

```python
@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str          # "viewer" | "author" | "admin"
    is_active: bool = True

    def has_role(self, role: str) -> bool:
        return ROLE_ORDER[self.role] >= ROLE_ORDER[role]
```

**Three nested roles** (`ROLE_ORDER = {"viewer": 0, "author": 1, "admin": 2}`):

| Role | Grants |
|---|---|
| `viewer` | Query + read everything: run queries, browse saved visuals/dashboards, ask chat questions, read pipeline/sandbox definitions and run history. |
| `author` | Everything a viewer can, plus: save visuals/dashboards/notebooks, save model measures (scalar `expr:`/`from:`), compose notebook pages, pin a chat answer as a visual. |
| `admin` | Everything an author can, plus: raw model/pipeline/dimension-bundle YAML, user management, model memory curation, and — the one place role tracks *reach* rather than *authoring convenience* — creating, editing, deleting **and running** a pipeline or a sandbox notebook. |

## Secrets at rest

- **Passwords**: Argon2id-encoded hashes (`argon2-cffi`). `hash_password()`
  enforces an 8-character minimum; `password_needs_rehash()` triggers a
  transparent rehash on next successful login if the hasher's parameters
  have since been strengthened.
- **Session cookies and personal-access-token secrets**: high-entropy random
  strings (`secrets.token_urlsafe(32)`), stored only as a **SHA-256 digest**
  (`_digest()`). Entropy is the defense here, not slow hashing — these
  aren't human-guessable passwords, so a fast hash is the right trade
  (constant-time digest lookup on every request, vs. Argon2's deliberate
  slowness which only matters for a *low-entropy* secret).

## Sessions

`establish_session(store, user_id)` mints an opaque cookie value, hashes it,
and inserts a `sessions` row; `resolve_session()` looks a presented cookie
up by digest and enforces revocation, idle timeout, absolute lifetime, and
that the underlying account is still active — all four checked on every
request, in one query (`AuthStore.get_session_user`).

| Setting | Env var | Default |
|---|---|---|
| Idle timeout | `CI_SESSION_IDLE_DAYS` | 7 days |
| Absolute lifetime | `CI_SESSION_MAX_DAYS` | 30 days |
| `Secure` cookie flag | `CI_COOKIE_SECURE` | off (the demo runs on plain HTTP; set `1` behind TLS) |

Cookies are `httponly`, `samesite=lax`. `last_seen` is touched at most once
per `TOUCH_INTERVAL` (60s) rather than on every request, to keep the write
rate down. Changing a password revokes every *other* session for the
account (`revoke_sessions_for_user(..., except_session_id=keep)`); the
session making the change stays alive.

## Personal access tokens

For scripts and the MCP server — anything that isn't a browser holding a
cookie. `mint_token()` returns the secret (prefixed `cipat_…`) **exactly
once**, at creation; only its hash is ever stored. `resolve_token()` mirrors
`resolve_session()` (active user, not revoked) but has no idle/absolute
expiry of its own — a token is revoked explicitly (`DELETE /api/tokens/{id}`)
rather than aging out. Sent as `Authorization: Bearer cipat_…`.

## Login lockout

Per-account, persisted (survives a restart — it's a column on `users`, not
in-memory state): `LOCK_THRESHOLD = 5` consecutive failures locks the
account for `LOCK_BASE = 60` seconds, **doubling per further failure** up to
`LOCK_CAP = 900` seconds. `burn_a_hash_check()` runs a real Argon2 verify
against a dummy hash even for an *unknown* username, so the 401 path costs
the same wall-clock time whether or not the account exists — no username
oracle via timing.

## `AuthMiddleware`: default-deny on `/api` and `/mcp`

```python
PUBLIC_API = {("POST", "/api/auth/login"), ("GET", "/api/health")}
```

Every other route under `/api` or `/mcp` requires a resolved identity —
**a route cannot opt out by forgetting a dependency**, because the check
happens once, centrally, in middleware, before any router code runs.
`/mcp` has no public entry at all, not even the MCP `initialize` handshake
(see [Agents & MCP](agents-and-mcp.md)). Static assets and the SPA shell
(`/`, `/static/*`) stay public — they're code, not data; the client renders
its own login overlay when `GET /api/auth/me` comes back 401.

**Credential precedence**: an `Authorization: Bearer` header, when present,
is used **exclusively** — no cookie fallback, so a request is never
authenticated as a blend of two identities. Otherwise the session cookie is
tried.

**CSRF.** A cookie-authenticated **mutation** (any method but `GET`/`HEAD`)
must also carry `X-Requested-With: fetch`, or the middleware answers 403.
Bearer-token requests are exempt from this check — a cross-site page can't
set a custom `Authorization` header in the first place, so there's nothing
for the header to defend against there. The frontend's `api()` helper
(`app/static/js/lib.js`) sets this header on every request unconditionally.

Authorization on top of authentication is each route's own job:
`Depends(require_role("author"))` (a FastAPI dependency built by
`auth.require_role(role)`) raises 403 when `user.has_role(role)` is false.
The full route-by-route matrix is enforced by
`tests/test_role_matrix.py` against
`specs/011-session-auth-rbac/contracts/auth-api.md` — see
[API Layer](api-layer.md) for the summarized table.

## `AuthStore`: the persistence side

`app/authstore.py` owns four tables in `cash_intel.db`
(schema in the module's `SCHEMA` string, created on first connect):

- **`users`** — username (validated `^[a-z0-9_.-]{2,32}$`), display name,
  role, Argon2 hash, `is_active`, lockout counters, and (since spec 013) a
  `theme` preference.
- **`sessions`** — token hash, owning user, created/last-seen timestamps,
  nullable `revoked_at`.
- **`api_tokens`** — token hash, owning user, a human-readable name,
  last-used timestamp, nullable `revoked_at`.
- **`audit_events`** — append-only: actor (user id + a label that survives
  the user being deleted later), action, an optional free-text target,
  timestamp. No browsing API is exposed for this table on purpose — it's a
  record, not a feature.

`update_user()` refuses (`LastAdminError`) any change — a demotion or a
deactivation — that would leave the system with **zero active admins**; the
check and the write share one connection so the invariant holds under the
single-writer model. There's no self-registration and no hard delete —
`is_active=False` is how an account is retired, keeping every provenance
and audit row it ever produced attributable.

## The trust boundary (Principle VI)

The constitution's Principle VI is the security decision every module in
this codebase is built around, and it's worth stating precisely because
"admin-gated" means two *different* things depending on what's being
gated:

**For expressions** (a measure's `expr:`, a `from:` block, an inline
visual measure, a chat-proposed inline measure), the boundary is
**structural, not role-based**. Every one of them compiles through
`app/sqlgrammar.py`'s allowlisting AST compiler — the same one, regardless
of who authored the text or whether it's saved or query-time-only. That
compiler is *structurally incapable* of running arbitrary code (no `eval`,
`exec` or `compile`, and every table function is excluded by construction —
see [Query Engine](query-engine.md#the-sql-grammar)). Because of that, an
inline measure from an unauthenticated visual is exactly as safe as a
saved model measure — saving one to a model grants **governance**
(provenance tracking, a role gate on who may add it to the shared catalog)
but not **extra language power**. This is why measure authoring only needs
the `author` role, not `admin`.

**For pipelines and sandbox notebooks**, the boundary is genuinely about
reach, and role really is the gate. Their SQL keeps the table functions a
measure may never name (`read_parquet`, `delta_scan`, `COPY … TO`,
`ATTACH`), so it can read and write **arbitrary bucket paths** — that's
application-code-level I/O reach, not something SQL's grammar can fence off
the way it does for a measure. Creating, editing, deleting, **and running**
either one therefore requires the `admin` role — running is held to the
same bar as authoring, since running is what actually exercises the reach.
Process isolation (each run in its own killable subprocess — see
[Pipelines](pipelines.md) and [Sandbox](sandbox.md)) is a crash-safety and
timeout mechanism layered on top of this, not the trust boundary itself.

**Model/pipeline/dimension-bundle *structural* YAML** (sources, joins,
target paths, materialization policy) stays trusted, developer/admin-
authored configuration — the same level application code sits at — gated
by the `admin` role on the raw-YAML routes specifically because a
`source: path:` or a pipeline `target:` can point anywhere in the bucket.

Any future change that widens who may reach a table function, or that
introduces a new construct evaluating authored text in any language, has to
re-open this principle explicitly rather than drift into it — see
`.specify/memory/constitution.md` for the full amendment history, which
records exactly this happening twice already (the spec-008
safe-measure-compilation rewrite, and the spec-018 DuckDB-SQL-engine
rewrite that removed the last `eval`-based construct, `frame:`, entirely).

## MCP authentication

The MCP server mounted at `/mcp` (see [Agents & MCP](agents-and-mcp.md))
authenticates through the exact same `AuthMiddleware` — a session cookie or
a bearer token, no anonymous handshake, and `tools/list` only ever returns
the skills a connection's authenticated role can actually invoke (a viewer
never sees an admin-only tool listed, let alone gets refused calling one).
