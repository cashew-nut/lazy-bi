# API Contract: Theme Preference

New endpoints under `/api`, same conventions as the rest of this API
(see `specs/011-session-auth-rbac/contracts/auth-api.md`): JSON, errors as
`{"detail": "..."}`, auth via session cookie or personal access token.
Both endpoints require an authenticated user — there is no account-level
concept of an anonymous theme preference; anonymous/logged-out users only
ever have the browser-local (`localStorage`) copy, which never touches the
server.

## New endpoints (`app/api/users.py`, alongside the existing `/users/*` self-service token routes — not the admin-only ones)

### GET /api/users/me/theme — any authenticated user

Returns the caller's own account-level theme preference.

- **200**: `{"theme": "cyberpunk" | "daylight" | "slate" | "contrast" | null, "updated_at": string | null}`
  - `theme: null` means the account has no recorded preference yet (new
    user, or one who has only ever used the local copy) — the client
    treats this the same as `"cyberpunk"` per FR-007, but the raw `null`
    is returned so the client can tell "unset" apart from "explicitly set
    to cyberpunk" for reconciliation purposes (research.md §5).

### PUT /api/users/me/theme — any authenticated user

Sets the caller's own account-level theme preference. This is the only
write path — there is no admin override of another user's theme (out of
scope; a user's theme is exactly as self-service as their own PAT
management already is).

Request: `{"theme": "cyberpunk" | "daylight" | "slate" | "contrast"}`

- **200**: `{"theme": string, "updated_at": string}` — `updated_at` is
  always stamped from the **server's** clock at write time, never accepted
  from the client (research.md §5), so the response is the authoritative
  value the client should store as its reconciliation baseline.
- **422**: `{"detail": "..."}` — `theme` missing or not one of the 4 known
  ids. No other validation applies; this is a low-stakes cosmetic field,
  not a security-relevant one (Constitution Principle VI: N/A, confirmed
  in plan.md).

No `DELETE` — "reset to default" is expressed by `PUT {"theme": "cyberpunk"}`,
consistent with FR-007's default.

## Explicitly not part of this contract

- No endpoint lists or describes the 4 themes themselves (their token
  values, labels, palettes) — those are static frontend assets
  (`style.css` + `theme.js`), not server-rendered data, so there's nothing
  for the API to serve.
- No bulk/admin endpoint to set another user's theme or to see the
  distribution of theme choices across users — out of scope for this
  feature.
