"""The round-trip contract against a real object store.

Three behaviors carry the authoring loop's latency, and each burned once, so
each is pinned:

  - a model/bundle save must not drop the source-derived caches — clearing
    them made every keystroke and every query after a save run cold against
    the bucket (a YAML edit changes no byte in the object store);
  - AWS credentials are resolved through one held boto3 resolver, not a fresh
    Session per DuckDB cursor — per-cursor resolution put 150ms+ of SSO/
    profile-chain work under the connection lock, which multiplied into
    20-second saves (a reload compiles every measure, and every compile takes
    a cursor);
  - DuckDB's own caches stay trustworthy: no HTTP metadata cache (it has no
    TTL and no invalidation, so an overwritten object would stay stale — or
    worse, mix cached metadata with fresh bytes), and the byte cache is
    dropped by toggling it, which must always end re-enabled.
"""
import io

import pyarrow as pa
import pyarrow.parquet as pq

from app import config


# ── a save keeps the caches warm ─────────────────────────────────────────

def test_reload_all_keeps_source_derived_cache_entries(client):
    """A model save (reload_all) must not clear path-keyed cache entries:
    they are derived from bucket contents, and a YAML edit doesn't change
    bucket contents. This is what keeps the edit → save → query loop warm."""
    from app import cache
    from app.registry import registry

    sentinel = object()
    key = ("duck_objects", "s3://a-bucket/some/prefix/*.parquet")
    assert cache.get_or_set(key, 600.0, lambda: sentinel) is sentinel
    registry.reload_all()
    assert cache.get_or_set(key, 600.0, lambda: None) is sentinel, (
        "reload_all() dropped a source-derived cache entry — every save now "
        "re-lists and re-reads every source from the object store"
    )


def test_data_writes_still_drop_those_entries(client):
    """The counterpart: paths the platform *wrote to* (upload, delete,
    pipeline run) do invalidate — that call site keeps the full clear."""
    from app import cache
    from app.api.datasets import _invalidate_reads

    sentinel = object()
    key = ("duck_objects", "s3://a-bucket/some/prefix/*.parquet")
    cache.get_or_set(key, 600.0, lambda: sentinel)
    _invalidate_reads()
    assert cache.get_or_set(key, 600.0, lambda: None) is None


# ── one credential resolver per process ──────────────────────────────────

class _FakeFrozen:
    access_key = "AK"
    secret_key = "SK"
    token = None


class _FakeCreds:
    # deliberately no refresh_needed(): botocore's *static* credential shape,
    # the kind resolve_credentials re-resolves only after its slow TTL
    def get_frozen_credentials(self):
        return _FakeFrozen()


def _patch_profile(monkeypatch, calls):
    import boto3

    class FakeSession:
        def __init__(self, profile_name=None):
            calls.append(profile_name)

        def get_credentials(self):
            return _FakeCreds()

    monkeypatch.setattr(boto3, "Session", FakeSession)
    monkeypatch.setattr(config, "AWS_PROFILE", "corp-sso")
    monkeypatch.setattr(config, "AWS_ACCESS_KEY_ID", "")
    monkeypatch.setattr(config, "_boto_credentials", None)
    monkeypatch.setattr(config, "_boto_resolved_at", 0.0)


def test_resolve_credentials_builds_one_session_not_one_per_call(monkeypatch):
    calls = []
    _patch_profile(monkeypatch, calls)
    for _ in range(5):
        assert config.resolve_credentials() == ("AK", "SK", None)
    assert calls == ["corp-sso"], (
        "resolve_credentials built a boto3 Session per call — that walk of the "
        "credential chain costs 150ms+ per cursor on a profile/SSO setup"
    )


def test_a_static_credential_is_rechecked_after_its_ttl(monkeypatch):
    import time

    calls = []
    _patch_profile(monkeypatch, calls)
    config.resolve_credentials()
    assert len(calls) == 1
    # age the resolution past the static TTL: the file behind a static
    # credential can be rotated, so it must eventually be re-read
    monkeypatch.setattr(
        config, "_boto_resolved_at",
        time.monotonic() - (config._BOTO_STATIC_TTL + 1))
    config.resolve_credentials()
    assert len(calls) == 2


# ── DuckDB's caches stay trustworthy ─────────────────────────────────────

def test_the_http_metadata_cache_stays_out_of_the_settings():
    """`enable_http_metadata_cache` caches HEAD responses with no TTL and no
    invalidation. Byte-cache validation trusts it, so with it on, an object
    overwritten in the bucket keeps serving its old rows for the life of the
    process — and a partially evicted byte cache can pair stale metadata with
    fresh bytes into a corrupt read. Freshness comes from DuckDB validating
    against the object's real version on each open, which needs that HEAD to
    actually happen."""
    from app import duck

    assert "enable_http_metadata_cache" not in duck._SETTINGS


def test_invalidate_always_leaves_the_byte_cache_enabled(client):
    """invalidate() clears DuckDB's byte cache by toggling it off and on; a
    failure path that left it off would quietly turn every query cold for the
    rest of the process."""
    from app import duck

    duck.invalidate()
    value = duck.cursor().execute(
        "SELECT current_setting('enable_external_file_cache')").fetchone()[0]
    assert value in (True, "true", 1)


def test_model_cache_keys_are_never_recycled_across_objects(models):
    """Two parses of the same YAML are two identities: schema cached for one
    must never serve the other (the guided form re-parses per keystroke), and
    — unlike id() — a token can't be recycled onto a new object after the old
    one is garbage-collected, which is what lets reload_all() skip clearing."""
    from app import engine, semantic

    text = """
name: token_probe
source: { format: parquet, path: s3://nowhere/at/all.parquet }
dimensions: [ { name: a } ]
measures: [ { name: n, expr: 'COUNT(*)' } ]
"""
    one = semantic.parse_model_text(text)
    two = semantic.parse_model_text(text)
    assert engine._model_cache_key(one) != engine._model_cache_key(two)
    assert engine._model_cache_key(one) == engine._model_cache_key(one)


def test_an_overwritten_object_is_fresh_on_the_next_query(client, monkeypatch):
    """Overwrite a source object *behind the app's back* (no upload endpoint,
    no reload, no invalidation) and the very next query must return the new
    rows: DuckDB validates cached bytes against the object's version on each
    open. This is the property that makes it safe for a model save to skip
    cache-clearing, and it is exactly what the HTTP metadata cache used to
    break."""
    from app import cache, duck, s3

    # pinning is the one deliberate staleness window (TTL-bounded, and every
    # app-made write invalidates it); this test is about the streamed path
    monkeypatch.setattr(config, "SOURCE_CACHE_MAX_BYTES", 0)

    bucket = config.DEMO_BUCKET
    key = "freshness/probe.parquet"
    s3_client = s3.client(bucket)

    def upload(amounts):
        buf = io.BytesIO()
        pq.write_table(pa.table({"region": ["north", "south"],
                                 "amount": amounts}), buf)
        s3_client.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())

    upload([1.0, 2.0])
    created = client.post("/api/models", json={"yaml": f"""
name: freshness_probe
label: Freshness Probe
source: {{format: parquet, path: s3://{bucket}/{key}}}
dimensions:
  - name: region
measures:
  - name: total
    expr: SUM(amount)
"""})
    assert created.status_code == 201, created.text
    try:
        query = {"model": "freshness_probe", "dimensions": [], "measures": ["total"]}
        first = client.post("/api/query", json=query)
        assert first.status_code == 200, first.text
        assert first.json()["rows"][0]["total"] == 3.0

        upload([10.0, 20.0])   # straight to the bucket: no app code sees this
        second = client.post("/api/query", json=query)
        assert second.status_code == 200, second.text
        assert second.json()["rows"][0]["total"] == 30.0, (
            "a query served stale bytes after the object changed in the bucket"
        )
    finally:
        client.delete("/api/models/freshness_probe")
        s3_client.delete_object(Bucket=bucket, Key=key)
        cache.clear()
        duck.invalidate()
