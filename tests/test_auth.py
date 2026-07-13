"""Auth suite (spec 011): store primitives, sessions, middleware, login
flow, lockout, bootstrap admin, user management, tokens, provenance."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import auth
from app.authstore import AuthStore, LastAdminError


def _iso(dt):
    return dt.isoformat(timespec="seconds")


@pytest.fixture()
def fresh_store(tmp_path):
    return AuthStore(tmp_path / "auth-unit.db", idle_days=7, max_days=30)


# ── AuthStore primitives ─────────────────────────────────────

def test_user_crud_and_username_rules(fresh_store):
    u = fresh_store.create_user("Ada.Lovelace", "Ada", "author", auth.hash_password("s3cretpw"))
    assert u["username"] == "ada.lovelace"          # lowercased on write
    assert u["role"] == "author" and u["is_active"]
    assert fresh_store.get_user_by_username("ADA.lovelace")["id"] == u["id"]
    with pytest.raises(ValueError):
        fresh_store.create_user("x", "too short", "viewer", "h")
    with pytest.raises(ValueError):
        fresh_store.create_user("has space", "bad", "viewer", "h")
    with pytest.raises(ValueError):
        fresh_store.create_user("okname", "bad role", "root", "h")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):    # duplicate (case-insensitive)
        fresh_store.create_user("ada.lovelace", "dupe", "viewer", "h")


def test_last_admin_invariant(fresh_store):
    admin = fresh_store.create_user("root-a", "A", "admin", "h")
    with pytest.raises(LastAdminError):
        fresh_store.update_user(admin["id"], role="viewer")
    with pytest.raises(LastAdminError):
        fresh_store.update_user(admin["id"], is_active=False)
    fresh_store.create_user("root-b", "B", "admin", "h")
    assert fresh_store.update_user(admin["id"], role="viewer")["role"] == "viewer"
    # and now root-b is the last one again
    b = fresh_store.get_user_by_username("root-b")
    with pytest.raises(LastAdminError):
        fresh_store.update_user(b["id"], is_active=False)


def test_password_hash_roundtrip():
    h = auth.hash_password("hunter22")
    assert h.startswith("$argon2id$")
    assert auth.verify_password(h, "hunter22")
    assert not auth.verify_password(h, "hunter23")
    with pytest.raises(ValueError):
        auth.hash_password("short")


def test_session_issue_resolve_revoke(fresh_store):
    u = fresh_store.create_user("sess-user", "S", "viewer", "h")
    cookie = auth.establish_session(fresh_store, u["id"])
    principal = auth.resolve_session(fresh_store, cookie)
    assert principal.username == "sess-user" and principal.role == "viewer"
    assert auth.revoke_session(fresh_store, cookie)
    assert auth.resolve_session(fresh_store, cookie) is None
    assert not auth.revoke_session(fresh_store, cookie)  # already revoked


def test_session_expiry_windows(fresh_store):
    u = fresh_store.create_user("exp-user", "E", "viewer", "h")
    cookie = auth.establish_session(fresh_store, u["id"])
    now = datetime.now(timezone.utc)
    with fresh_store._conn() as conn:
        conn.execute("UPDATE sessions SET last_seen = ?", (_iso(now - timedelta(days=8)),))
    assert auth.resolve_session(fresh_store, cookie) is None      # idle timeout
    cookie2 = auth.establish_session(fresh_store, u["id"])
    with fresh_store._conn() as conn:
        conn.execute(
            "UPDATE sessions SET created_at = ?, last_seen = ? WHERE revoked_at IS NULL",
            (_iso(now - timedelta(days=31)), _iso(now)),
        )
    assert auth.resolve_session(fresh_store, cookie2) is None     # absolute lifetime


def test_session_dies_with_deactivation(fresh_store):
    fresh_store.create_user("keeper", "K", "admin", "h")
    u = fresh_store.create_user("leaver", "L", "author", "h")
    cookie = auth.establish_session(fresh_store, u["id"])
    assert auth.resolve_session(fresh_store, cookie)
    fresh_store.update_user(u["id"], is_active=False)
    assert auth.resolve_session(fresh_store, cookie) is None


def test_token_mint_resolve_revoke(fresh_store):
    u = fresh_store.create_user("tok-user", "T", "author", "h")
    secret, row = auth.mint_token(fresh_store, u["id"], "ci script")
    assert secret.startswith("cipat_")
    principal = auth.resolve_token(fresh_store, secret)
    assert principal.id == u["id"] and principal.role == "author"
    listing = fresh_store.list_tokens(u["id"])
    assert listing[0]["name"] == "ci script" and "token_hash" not in listing[0]
    assert fresh_store.revoke_token(row["id"], u["id"])
    assert auth.resolve_token(fresh_store, secret) is None
    assert auth.resolve_token(fresh_store, "cipat_bogus") is None
    assert auth.resolve_token(fresh_store, "not-a-token") is None


def test_lockout_policy(fresh_store):
    u = fresh_store.create_user("bruteforced", "B", "viewer", "h")
    for _ in range(4):
        auth.register_login_failure(fresh_store, fresh_store.get_user(u["id"]))
    assert auth.lockout_remaining(fresh_store.get_user(u["id"])) == 0
    auth.register_login_failure(fresh_store, fresh_store.get_user(u["id"]))  # 5th
    row = fresh_store.get_user(u["id"])
    assert 0 < auth.lockout_remaining(row) <= 60
    auth.register_login_failure(fresh_store, row)                            # 6th → doubles
    assert 60 < auth.lockout_remaining(fresh_store.get_user(u["id"])) <= 120
    auth.register_login_success(fresh_store, fresh_store.get_user(u["id"]))
    row = fresh_store.get_user(u["id"])
    assert row["failed_attempts"] == 0 and row["locked_until"] is None


def test_role_ordering():
    admin = auth.User(1, "a", "A", "admin")
    author = auth.User(2, "b", "B", "author")
    viewer = auth.User(3, "c", "C", "viewer")
    assert admin.has_role("viewer") and admin.has_role("admin")
    assert author.has_role("viewer") and not author.has_role("admin")
    assert viewer.has_role("viewer") and not viewer.has_role("author")


def test_audit_log_shape(fresh_store):
    fresh_store.record_audit("login_failed", "ghost", target="ghost")
    u = fresh_store.create_user("auditor", "A", "admin", "h")
    fresh_store.record_audit("login", "auditor", actor_user_id=u["id"])
    events = fresh_store.audit_events()
    assert [e["action"] for e in events] == ["login_failed", "login"]
    assert events[0]["actor_user_id"] is None
    assert events[1]["actor_user_id"] == u["id"]
    assert all(e["created_at"] for e in events)


# ── middleware: default-deny + CSRF ─────────────────────────

def test_api_default_deny_for_anonymous(anon_client):
    assert anon_client.get("/api/models").status_code == 401
    assert anon_client.post("/api/query", json={}).status_code == 401
    assert anon_client.get("/api/dashboards").status_code == 401
    # allowlist survives
    assert anon_client.get("/api/health").status_code == 200


def test_static_and_index_stay_public(anon_client):
    assert anon_client.get("/").status_code == 200
    assert anon_client.get("/static/js/lib.js").status_code == 200


def test_cookie_mutation_requires_csrf_header(admin_client):
    r = admin_client.post("/api/models/reload", headers={"X-Requested-With": ""})
    assert r.status_code == 403
    assert "X-Requested-With" in r.json()["detail"]
    assert admin_client.post("/api/models/reload").status_code == 200  # header from fixture


# ── US1: login/logout lifecycle over the API ─────────────────

def _fresh_client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, headers={"X-Requested-With": "fetch"})


def test_login_logout_lifecycle(anon_client, test_users):
    c = _fresh_client()
    creds = test_users["viewer"]
    r = c.post("/api/auth/login", json={"username": creds["username"],
                                        "password": creds["password"]})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "viewer"
    set_cookie = r.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie and "Path=/" in set_cookie
    assert "Secure" not in set_cookie          # CI_COOKIE_SECURE unset in tests

    me = c.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["username"] == creds["username"]

    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/auth/me").status_code == 401   # revoked server-side


def test_login_no_username_oracle(anon_client, test_users):
    c = _fresh_client()
    unknown = c.post("/api/auth/login", json={"username": "no-such-user",
                                              "password": "whatever123"})
    wrong = c.post("/api/auth/login", json={"username": test_users["viewer"]["username"],
                                            "password": "wrong-password"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()      # byte-identical bodies


def test_login_lockout_via_api(anon_client):
    from app import auth
    from app.registry import registry

    registry.auth_store.create_user("locked-out", "L", "viewer",
                                    auth.hash_password("correct-pw-1"))
    c = _fresh_client()
    for _ in range(5):
        r = c.post("/api/auth/login", json={"username": "locked-out",
                                            "password": "wrong"})
        assert r.status_code == 401
    r = c.post("/api/auth/login", json={"username": "locked-out",
                                        "password": "correct-pw-1"})
    assert r.status_code == 423
    assert "locked" in r.json()["detail"]


def test_password_change_revokes_other_sessions(anon_client, test_users):
    from app import auth
    from app.registry import registry

    registry.auth_store.create_user("pw-changer", "P", "viewer",
                                    auth.hash_password("first-pw-1"))
    c1, c2 = _fresh_client(), _fresh_client()
    for c in (c1, c2):
        assert c.post("/api/auth/login", json={"username": "pw-changer",
                                               "password": "first-pw-1"}).status_code == 200
    r = c1.post("/api/auth/password", json={"current_password": "first-pw-1",
                                            "new_password": "second-pw-2"})
    assert r.status_code == 204
    assert c1.get("/api/auth/me").status_code == 200    # the changing session survives
    assert c2.get("/api/auth/me").status_code == 401    # the other one dies
    # wrong current password refused
    assert c1.post("/api/auth/password", json={"current_password": "nope",
                                               "new_password": "third-pw-3"}).status_code == 401


def test_bootstrap_admin_seeds_once(tmp_path, capsys):
    from app import auth, seed
    from app.authstore import AuthStore
    from app.registry import registry

    original = registry.auth_store
    registry.auth_store = AuthStore(tmp_path / "boot.db")
    try:
        assert seed.seed_bootstrap_admin() is True
        out = capsys.readouterr().out
        assert "BOOTSTRAP ADMIN CREATED" in out and "username: admin" in out
        password = next(line.split("password:")[1].strip()
                        for line in out.splitlines() if "password:" in line)
        row = registry.auth_store.get_user_by_username("admin")
        assert row["role"] == "admin"
        assert auth.verify_password(row["password_hash"], password)
        # never re-runs once any account exists
        assert seed.seed_bootstrap_admin() is False
        assert "BOOTSTRAP" not in capsys.readouterr().out
        assert [e["action"] for e in registry.auth_store.audit_events()] == [
            "bootstrap_admin_created"]
    finally:
        registry.auth_store = original


def test_auth_flow_writes_audit_trail(anon_client, test_users):
    from app.registry import registry

    c = _fresh_client()
    c.post("/api/auth/login", json={"username": "audit-ghost", "password": "whatever123"})
    creds = test_users["author"]
    c.post("/api/auth/login", json={"username": creds["username"],
                                    "password": creds["password"]})
    c.post("/api/auth/logout")
    actions = [(e["action"], e["actor_label"]) for e in registry.auth_store.audit_events()]
    assert ("login_failed", "audit-ghost") in actions
    assert ("login", creds["username"]) in actions
    assert ("logout", creds["username"]) in actions
