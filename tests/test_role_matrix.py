"""Exhaustive role-matrix sweep (spec 011, SC-001/SC-002).

Walks the live FastAPI route table and asserts, for every (route, method) ×
{anonymous, viewer, author, admin}, exactly the verdict in
specs/011-session-auth-rbac/contracts/auth-api.md. A route that isn't in
the MATRIX below fails the test — new endpoints must declare their minimum
role here, so a forgotten gate can't ship silently.
"""
import re

import pytest

# (method, path-template) -> minimum role. "public" = no auth at all.
MATRIX = {
    ("POST", "/api/auth/login"): "public",
    ("GET", "/api/health"): "public",

    ("POST", "/api/auth/logout"): "viewer",
    ("GET", "/api/auth/me"): "viewer",
    ("POST", "/api/auth/password"): "viewer",
    ("GET", "/api/tokens"): "viewer",
    ("POST", "/api/tokens"): "viewer",
    ("DELETE", "/api/tokens/{token_id}"): "viewer",

    ("GET", "/api/models"): "viewer",
    ("POST", "/api/models/validate"): "viewer",
    ("GET", "/api/models/{name}/spec"): "viewer",
    ("GET", "/api/models/{name}/yaml"): "viewer",
    ("GET", "/api/models/{name}/schema"): "viewer",
    ("POST", "/api/measures/check"): "viewer",
    ("GET", "/api/models/{name}/measures/{measure_name}/history"): "viewer",
    ("GET", "/api/models/{name}/dimensions/{dimension}/values"): "viewer",
    ("GET", "/api/models/{name}/memories"): "viewer",
    ("GET", "/api/dimensions"): "viewer",
    ("POST", "/api/dimensions/validate"): "viewer",
    ("GET", "/api/dimensions/{name}/spec"): "viewer",
    ("GET", "/api/dimensions/{name}/yaml"): "viewer",
    ("POST", "/api/query"): "viewer",
    ("GET", "/api/visuals"): "viewer",
    ("GET", "/api/dashboards"): "viewer",
    ("GET", "/api/dashboards/{dash_id}"): "viewer",
    ("GET", "/api/portal"): "viewer",
    ("GET", "/api/explorer"): "viewer",
    ("GET", "/api/datasets"): "viewer",
    ("GET", "/api/datasets/schema"): "viewer",
    ("GET", "/api/conversations"): "viewer",
    ("POST", "/api/conversations"): "viewer",
    ("GET", "/api/conversations/{conversation_id}"): "viewer",
    ("PATCH", "/api/conversations/{conversation_id}"): "viewer",
    ("DELETE", "/api/conversations/{conversation_id}"): "viewer",
    ("POST", "/api/conversations/{conversation_id}/ask"): "viewer",
    ("POST", "/api/conversations/{conversation_id}/ask/stream"): "viewer",

    ("POST", "/api/models/generate"): "author",
    ("POST", "/api/dimensions/generate"): "author",
    ("POST", "/api/models/{name}/measures"): "author",
    ("PUT", "/api/models/{name}/measures/{measure_name}"): "author",
    ("DELETE", "/api/models/{name}/measures/{measure_name}"): "author",
    ("POST", "/api/conversations/{conversation_id}/messages/{message_id}/pin"): "author",
    ("POST", "/api/visuals"): "author",
    ("PUT", "/api/visuals/{visual_id}"): "author",
    ("DELETE", "/api/visuals/{visual_id}"): "author",
    ("POST", "/api/dashboards"): "author",
    ("PUT", "/api/dashboards/{dash_id}"): "author",
    ("DELETE", "/api/dashboards/{dash_id}"): "author",
    ("POST", "/api/publish"): "author",
    ("DELETE", "/api/publish/{dashboard_id}"): "author",

    ("GET", "/api/users"): "admin",
    ("POST", "/api/users"): "admin",
    ("PATCH", "/api/users/{user_id}"): "admin",
    ("POST", "/api/models"): "admin",
    ("POST", "/api/models/reload"): "admin",
    ("PUT", "/api/models/{name}/yaml"): "admin",
    ("DELETE", "/api/models/{name}"): "admin",
    ("POST", "/api/models/{name}/memories"): "admin",
    ("PATCH", "/api/models/{name}/memories/{memory_id}"): "admin",
    ("DELETE", "/api/models/{name}/memories/{memory_id}"): "admin",
    ("POST", "/api/dimensions"): "admin",
    ("POST", "/api/dimensions/reload"): "admin",
    ("PUT", "/api/dimensions/{name}/yaml"): "admin",
    ("DELETE", "/api/dimensions/{name}"): "admin",
}

ROLE_ORDER = {"viewer": 0, "author": 1, "admin": 2}
METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

# Requests that would mutate the *authenticated sweep client's own* state
# (logout revokes the very session the sweep runs on). Anonymous coverage
# still applies; the authed behavior is covered in tests/test_auth.py.
SKIP_AUTHED_SWEEP = {("POST", "/api/auth/logout"), ("POST", "/api/auth/password")}


def _api_routes():
    """Every (method, path) the app serves under /api, from the OpenAPI
    schema — the same source of truth the docs use, so nothing hides."""
    from app.main import app

    out = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        for method in ops:
            if method.upper() in METHODS:
                out.append((method.upper(), path))
    assert out, "no /api routes discovered"
    return out


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "999999", path)


def _call(client, method: str, path: str):
    body = {} if method in ("POST", "PUT", "PATCH") else None
    return client.request(method, _fill(path), json=body)


def test_matrix_covers_every_route():
    """Every discovered route must have a declared minimum role."""
    missing = [r for r in _api_routes() if r not in MATRIX]
    assert not missing, f"routes without a declared role: {missing}"


def test_anonymous_is_refused_everywhere_except_allowlist(anon_client):
    for method, path in _api_routes():
        res = _call(anon_client, method, path)
        if MATRIX[(method, path)] == "public":
            assert res.status_code != 401, f"{method} {path} should not demand auth"
        else:
            assert res.status_code == 401, (
                f"{method} {path} answered {res.status_code} to an anonymous request")


@pytest.mark.parametrize("role", ["viewer", "author", "admin"])
def test_role_matrix_holds(role, viewer_client, author_client, admin_client):
    clients = {"viewer": viewer_client, "author": author_client, "admin": admin_client}
    client = clients[role]
    for method, path in _api_routes():
        required = MATRIX[(method, path)]
        if required == "public" or (method, path) in SKIP_AUTHED_SWEEP:
            continue
        res = _call(client, method, path)
        if ROLE_ORDER[role] >= ROLE_ORDER[required]:
            assert res.status_code not in (401, 403), (
                f"{method} {path} refused role '{role}' ({res.status_code}) "
                f"but requires only '{required}'")
        else:
            assert res.status_code == 403, (
                f"{method} {path} answered {res.status_code} to role '{role}' "
                f"(requires '{required}', expected 403)")
