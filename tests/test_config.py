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
    monkeypatch.setattr(config, "AWS_SESSION_TOKEN", "")
    assert "aws_session_token" not in config.storage_options()
    assert "s3.session-token" not in config.iceberg_storage_options()
    assert "AWS_SESSION_TOKEN" not in config.delta_write_options()


def test_session_token_included_when_set(monkeypatch):
    monkeypatch.setattr(config, "AWS_SESSION_TOKEN", "temp-token")
    assert config.storage_options()["aws_session_token"] == "temp-token"
    assert config.iceberg_storage_options()["s3.session-token"] == "temp-token"
    assert config.delta_write_options()["AWS_SESSION_TOKEN"] == "temp-token"


def test_resolve_credentials_uses_static_keys_by_default(monkeypatch):
    monkeypatch.setattr(config, "AWS_PROFILE", "")
    monkeypatch.setattr(config, "AWS_ACCESS_KEY_ID", "static-key")
    monkeypatch.setattr(config, "AWS_SECRET_ACCESS_KEY", "static-secret")
    monkeypatch.setattr(config, "AWS_SESSION_TOKEN", "")
    assert config.resolve_credentials() == ("static-key", "static-secret", None)


def test_resolve_credentials_prefers_profile_when_set(monkeypatch):
    calls = []

    class FakeSession:
        def __init__(self, profile_name=None):
            calls.append(profile_name)

        def get_credentials(self):
            frozen = SimpleNamespace(
                access_key="profile-key", secret_key="profile-secret", token="profile-token")
            return SimpleNamespace(get_frozen_credentials=lambda: frozen)

    monkeypatch.setattr(config, "AWS_PROFILE", "my-sso-profile")
    # ignored: AWS_PROFILE takes priority even though a static key is also set
    monkeypatch.setattr(config, "AWS_ACCESS_KEY_ID", "should-be-ignored")
    monkeypatch.setattr(boto3, "Session", FakeSession)

    assert config.resolve_credentials() == ("profile-key", "profile-secret", "profile-token")
    assert calls == ["my-sso-profile"]


def test_resolve_credentials_raises_a_clear_error_when_profile_has_no_credentials(monkeypatch):
    class FakeSession:
        def __init__(self, profile_name=None):
            pass

        def get_credentials(self):
            return None

    monkeypatch.setattr(config, "AWS_PROFILE", "empty-profile")
    monkeypatch.setattr(boto3, "Session", FakeSession)

    with pytest.raises(RuntimeError, match="empty-profile"):
        config.resolve_credentials()
