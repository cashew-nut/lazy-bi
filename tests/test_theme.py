"""Theme preference suite (spec 013): AuthStore migration/CRUD + the
GET/PUT /api/users/me/theme endpoints."""
import sqlite3

import pytest

from app.authstore import AuthStore


@pytest.fixture()
def fresh_store(tmp_path):
    return AuthStore(tmp_path / "theme-unit.db", idle_days=7, max_days=30)


# ── AuthStore: migration ──────────────────────────────────────

def test_theme_columns_migrate_onto_an_existing_database(tmp_path):
    """A database created before this feature (no theme/theme_updated_at
    columns) upgrades in place on the next AuthStore(...) open, without
    losing the row already there."""
    db_path = tmp_path / "pre-existing.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, "
        "display_name TEXT NOT NULL, role TEXT NOT NULL, password_hash TEXT NOT NULL, "
        "is_active INTEGER NOT NULL DEFAULT 1, failed_attempts INTEGER NOT NULL DEFAULT 0, "
        "locked_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO users (username, display_name, role, password_hash, created_at, updated_at) "
        "VALUES ('legacy', 'Legacy', 'admin', 'h', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = AuthStore(db_path)  # triggers the guarded ALTER TABLE
    user = store.get_user_by_username("legacy")
    assert user is not None and user["username"] == "legacy"  # pre-existing row intact
    assert user["theme"] is None and user["theme_updated_at"] is None  # new columns, unset

    # idempotent: opening again against the now-migrated db doesn't error
    AuthStore(db_path)


# ── AuthStore: get_theme / set_theme ──────────────────────────

def test_get_theme_unset_by_default(fresh_store):
    u = fresh_store.create_user("themeless", "T", "viewer", "h")
    assert fresh_store.get_theme(u["id"]) == {"theme": None, "updated_at": None}


def test_set_theme_roundtrip_and_stamps_timestamp(fresh_store):
    u = fresh_store.create_user("themed", "T", "viewer", "h")
    result = fresh_store.set_theme(u["id"], "daylight")
    assert result["theme"] == "daylight"
    assert result["updated_at"]  # server-stamped, non-empty
    assert fresh_store.get_theme(u["id"]) == result

    # setting again updates both fields
    second = fresh_store.set_theme(u["id"], "slate")
    assert second["theme"] == "slate"
    assert fresh_store.get_theme(u["id"]) == second


def test_set_theme_rejects_unknown_id(fresh_store):
    u = fresh_store.create_user("badtheme", "T", "viewer", "h")
    with pytest.raises(ValueError):
        fresh_store.set_theme(u["id"], "not-a-real-theme")
    assert fresh_store.get_theme(u["id"]) == {"theme": None, "updated_at": None}  # unchanged


# ── API: GET/PUT /api/users/me/theme ──────────────────────────

def test_theme_endpoint_requires_auth(anon_client):
    assert anon_client.get("/api/users/me/theme").status_code == 401
    assert anon_client.put("/api/users/me/theme", json={"theme": "slate"}).status_code == 401


def test_theme_endpoint_default_is_unset(viewer_client):
    r = viewer_client.get("/api/users/me/theme")
    assert r.status_code == 200
    body = r.json()
    assert body["theme"] is None
    assert body["updated_at"] is None


def test_theme_endpoint_roundtrip(viewer_client):
    r = viewer_client.put("/api/users/me/theme", json={"theme": "contrast"})
    assert r.status_code == 200
    body = r.json()
    assert body["theme"] == "contrast"
    assert body["updated_at"]

    again = viewer_client.get("/api/users/me/theme")
    assert again.status_code == 200
    assert again.json() == body


def test_theme_endpoint_rejects_unknown_theme(viewer_client):
    r = viewer_client.put("/api/users/me/theme", json={"theme": "solarized"})
    assert r.status_code == 422


def test_theme_endpoint_is_self_service_only(viewer_client, author_client):
    """Each account's theme is independent — setting one user's theme
    never touches another's, and there is no cross-user parameter."""
    viewer_client.put("/api/users/me/theme", json={"theme": "daylight"})
    author_client.put("/api/users/me/theme", json={"theme": "slate"})
    assert viewer_client.get("/api/users/me/theme").json()["theme"] == "daylight"
    assert author_client.get("/api/users/me/theme").json()["theme"] == "slate"
