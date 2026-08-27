"""app/config.py's S3 credential resolution.

Two things matter: AWS_SESSION_TOKEN (temporary/STS credentials pasted in
directly) is only ever added to an options dict when set, since
object_store/pyiceberg/deltalake treat an *empty* token as a real (invalid)
one rather than "absent" — and AWS_PROFILE, when set, always wins over the
static keys, resolved fresh through boto3 on every call so an SSO-derived
credential gets a chance to refresh instead of going stale."""
from types import SimpleNamespace

import boto3
import pytest

from app import config


def test_session_token_omitted_by_default(monkeypatch):
    """object_store and boto3 both read an *empty* session token as a real
    (invalid) one rather than as "absent", which would break the far more
    common no-token case."""
    monkeypatch.setattr(config, "AWS_SESSION_TOKEN", "")
    assert "s3.session-token" not in config.iceberg_storage_options()
    assert "AWS_SESSION_TOKEN" not in config.delta_write_options()


def test_session_token_included_when_set(monkeypatch):
    monkeypatch.setattr(config, "AWS_SESSION_TOKEN", "temp-token")
    assert config.iceberg_storage_options()["s3.session-token"] == "temp-token"
    assert config.delta_write_options()["AWS_SESSION_TOKEN"] == "temp-token"


def test_the_duckdb_secret_carries_the_session_token(monkeypatch):
    """Every read goes through DuckDB's own S3 secret, so that is where a
    temporary credential has to land."""
    from app import duck

    monkeypatch.setattr(config, "AWS_SESSION_TOKEN", "temp-token")
    duck._store_secrets.clear()                   # force a rewrite
    connection = duck.connection()
    # the resolved credential it wrote
    assert duck._store_secrets["cash_intel_s3"][2][2] == "temp-token"
    secret = connection.execute(
        "SELECT secret_string FROM duckdb_secrets() WHERE name = 'cash_intel_s3'").fetchone()
    # DuckDB redacts the values, which is the right posture for a catalog view
    # — what this can check is that the field is there at all
    assert secret and "session_token=" in secret[0]
    duck._store_secrets.clear()                   # and put the real one back
    duck.connection()
