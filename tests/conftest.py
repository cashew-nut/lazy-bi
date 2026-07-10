"""Test fixtures: a dedicated moto S3 server + seeded demo bucket + API client.

Environment overrides happen at conftest import time, before any app module
reads app.config.
"""
import os
import tempfile
from pathlib import Path

TEST_ENDPOINT = "http://127.0.0.1:9700"
_tmpdir = tempfile.mkdtemp(prefix="cash_intel_test_")
os.environ["CI_S3_ENDPOINT"] = TEST_ENDPOINT          # also disables the embedded emulator
os.environ["CI_DB_PATH"] = str(Path(_tmpdir) / "test.db")

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
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:   # context manager runs the lifespan
        yield c
