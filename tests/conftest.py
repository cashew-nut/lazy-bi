"""Test fixtures: a dedicated moto S3 server + seeded demo bucket + clients.

Environment overrides happen at conftest import time, before any app module
reads app.config.

Auth (spec 011): the lifespan seeds a bootstrap admin into the empty test
DB; `test_users` adds one known account per role, and the per-role client
fixtures are TestClients logged in via the real /api/auth/login flow with
the CSRF header baked in. `client` is the admin client — the strongest
role — so pre-auth suites keep exercising their endpoints unchanged;
`anon_client` carries no credentials at all.
"""
import os
import tempfile
from pathlib import Path

TEST_ENDPOINT = "http://127.0.0.1:9700"
_tmpdir = tempfile.mkdtemp(prefix="cash_intel_test_")
os.environ["CI_S3_ENDPOINT"] = TEST_ENDPOINT          # also disables the embedded emulator
os.environ["CI_DB_PATH"] = str(Path(_tmpdir) / "test.db")

PASSWORDS = {
    "viewer": "viewer-pass-123",
    "author": "author-pass-123",
    "admin": "admin-pass-1234",
}

import pytest  # noqa: E402
from moto.server import ThreadedMotoServer  # noqa: E402


@pytest.fixture(scope="session")
def moto_server():
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=9700, verbose=False)
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def seeded(moto_server):
    """Demo data in the bucket — without the (huge) local data_cache upload."""
    from app import seed

    original = seed._upload_local_cache
    seed._upload_local_cache = lambda client: None
    try:
        seed.seed_bucket()
    finally:
        seed._upload_local_cache = original
    return True


@pytest.fixture(scope="session")
def models(seeded):
    from app import config, semantic

    bundles = semantic.load_dimension_bundles(config.DIMENSIONS_DIR)
    loaded = semantic.load_models(config.MODELS_DIR)
    for model in loaded.values():
        semantic.resolve_imports(model, bundles)
    return loaded


@pytest.fixture(scope="session")
def anon_client(seeded):
    """Unauthenticated client; also owns the app lifespan for the session."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:   # context manager runs the lifespan
        yield c


@pytest.fixture(scope="session")
def test_users(anon_client):
    """One known account per role, created directly in the auth store."""
    from app import auth
    from app.registry import registry

    users = {}
    for role in ("viewer", "author", "admin"):
        username = f"{role}-tester"
        if not registry.auth_store.get_user_by_username(username):
            registry.auth_store.create_user(
                username, f"{role.title()} Tester", role,
                auth.hash_password(PASSWORDS[role]),
            )
        users[role] = {"username": username, "password": PASSWORDS[role]}
    return users


def _login_client(role: str, test_users) -> "TestClient":  # noqa: F821
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app, headers={"X-Requested-With": "fetch"})
    creds = test_users[role]
    r = c.post("/api/auth/login",
               json={"username": creds["username"], "password": creds["password"]})
    assert r.status_code == 200, f"login as {role} failed: {r.text}"
    return c


@pytest.fixture(scope="session")
def viewer_client(anon_client, test_users):
    return _login_client("viewer", test_users)


@pytest.fixture(scope="session")
def author_client(anon_client, test_users):
    return _login_client("author", test_users)


@pytest.fixture(scope="session")
def admin_client(anon_client, test_users):
    return _login_client("admin", test_users)


@pytest.fixture(scope="session")
def client(admin_client):
    """Default client for feature suites: the strongest role, so endpoint
    behavior (not authorization) is what those suites keep testing."""
    return admin_client
