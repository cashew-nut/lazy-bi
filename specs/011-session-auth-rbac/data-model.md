# Data Model: Session Authentication & Role-Based Authorization

All tables live in the existing SQLite database (`config.DB_PATH`), owned
by the new `AuthStore` (`app/authstore.py`) except the amended
`measure_provenance`, which stays in `VisualStore`. Schema creation follows
the existing idiom: idempotent `CREATE TABLE IF NOT EXISTS` executescript;
the one column addition uses a guarded `ALTER TABLE`.

**Persistence intent (Principle V)**: users, sessions, tokens, and audit
events are deliberately persisted; login-form state and unsaved editor
drafts across a re-authentication are deliberately ephemeral (JS memory).

## users

| Column | Type | Constraints | Notes |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | referenced by sessions, tokens, audit, provenance |
| username | TEXT | NOT NULL UNIQUE COLLATE NOCASE | login identifier; case-insensitive uniqueness |
| display_name | TEXT | NOT NULL | shown in UI and provenance |
| role | TEXT | NOT NULL CHECK IN ('viewer','author','admin') | strictly nested capabilities |
| password_hash | TEXT | NOT NULL | full Argon2id encoded string (params+salt embedded) |
| is_active | INTEGER | NOT NULL DEFAULT 1 | deactivate-only lifecycle — **rows are never deleted** |
| failed_attempts | INTEGER | NOT NULL DEFAULT 0 | consecutive login failures (R8) |
| locked_until | TEXT | NULL | ISO timestamp; lockout window end |
| created_at / updated_at | TEXT | NOT NULL | ISO UTC, `timespec="seconds"` (house style) |

Validation: username `^[a-z0-9_.-]{2,32}$` (lowercased on write); role must
be one of the three; password min length 8 (server-enforced).

State transitions: active ⇄ deactivated (admin only); role changes admin
only; **invariant guarded in the store**: an UPDATE that would leave zero
active admins is refused (FR-011). Deactivation and password change revoke
all sessions and (deactivation only) implicitly dead-ends tokens via the
active check.

## sessions

| Column | Type | Constraints | Notes |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| token_hash | TEXT | NOT NULL UNIQUE | SHA-256 hex of the opaque cookie value — raw value never stored (R2) |
| user_id | INTEGER | NOT NULL REFERENCES users(id) | |
| created_at | TEXT | NOT NULL | absolute-lifetime anchor (30d default) |
| last_seen | TEXT | NOT NULL | idle-timeout anchor (7d default); writes throttled to 60s |
| revoked_at | TEXT | NULL | set on logout / revoke-all; row kept for audit |

Lookup path (every authenticated request): `SELECT s.*, u.*` join on
`token_hash` index → check revoked/expiry/user-active → principal. Role is
read from `users` each request, so role changes bind existing sessions
immediately (US3 scenario 2).

Lifecycle: created on login → touched on use → ends by logout (revoked),
idle expiry, absolute expiry, password change (revoke all for user), or
deactivation (revoke all for user).

## api_tokens

| Column | Type | Constraints | Notes |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | shown in listings |
| token_hash | TEXT | NOT NULL UNIQUE | SHA-256 hex of `cipat_…` secret; secret shown exactly once (R5) |
| user_id | INTEGER | NOT NULL REFERENCES users(id) | token acts as this user, this user's role |
| name | TEXT | NOT NULL | user-chosen label |
| created_at | TEXT | NOT NULL | |
| last_used_at | TEXT | NULL | updated on use (60s throttle) |
| revoked_at | TEXT | NULL | individually revocable; row kept |

No expiry column in v1 (revocation is the lifecycle); owner deactivation
makes all their tokens unusable via the same user-active check.

## audit_events

Append-only; no browsing API/UI in this feature (clarified) — documented
shape for direct inspection.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| actor_user_id | INTEGER | NULL REFERENCES users(id) | NULL for failed logins to unknown usernames |
| actor_label | TEXT | NOT NULL | username as presented (survives even for unknown users) |
| action | TEXT | NOT NULL | enum: `login`, `login_failed`, `logout`, `lockout`, `user_created`, `user_role_changed`, `user_deactivated`, `user_reactivated`, `password_reset`, `password_changed`, `token_created`, `token_revoked`, `bootstrap_admin_created` |
| target | TEXT | NULL | e.g. affected username or token name/id |
| created_at | TEXT | NOT NULL | |

## measure_provenance (amended, in VisualStore)

Guarded migration: `ALTER TABLE measure_provenance ADD COLUMN user_id
INTEGER` (nullable, references users(id) informally — SQLite won't enforce
on ALTER).

- New rows: `author` = the account's display name (kept for readable
  history), `user_id` = verified account id.
- Legacy rows: `user_id IS NULL` → presented as "legacy (self-declared)"
  (FR-009, US2 scenario 3). No rewriting of existing rows.

## Entity relationships

```text
users 1 ─── * sessions        (revoked by logout / password change / deactivation)
users 1 ─── * api_tokens      (revoked individually; dead with deactivation)
users 1 ─── * audit_events    (as actor; nullable for unknown-user failures)
users 1 ─── * measure_provenance (new rows only; legacy rows have NULL user_id)
```

## Principal (in-memory, `app/auth.py`)

`User` dataclass: `id, username, display_name, role, is_active` — resolved
once per request by the middleware from a session cookie **or** bearer
token (token wins if both present), stashed on `request.state.user`,
consumed by `get_current_user` / `require_role(role)` dependencies. Role
comparison is ordinal: viewer < author < admin.

## Configuration additions (`app/config.py`)

| Var | Default | Meaning |
|---|---|---|
| `CI_SESSION_IDLE_DAYS` | `7` | idle timeout (FR-005) |
| `CI_SESSION_MAX_DAYS` | `30` | absolute lifetime (FR-005) |
| `CI_COOKIE_SECURE` | `0` | set `1` behind TLS; demo runs plain HTTP |
| `CI_API_KEY` | **removed** | retired with `X-Author` (FR-009) |
