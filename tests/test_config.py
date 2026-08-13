"""app/config.py's S3 credential option builders — in particular that
AWS_SESSION_TOKEN (temporary/STS credentials) is only ever added when set,
since object_store/pyiceberg/deltalake treat an *empty* token as a real
(invalid) one rather than "absent"."""
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
