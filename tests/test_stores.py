"""Two object stores at once: the built-in demo catalog and a real bucket.

The failure this suite exists for: pointing the app at a real bucket used to
switch the embedded emulator off and take the demo catalog down with it. The
demo models stayed listed, and every one of their queries came back
`HTTP 404 NoSuchBucket` from an account that had never heard of `cash-intel`.
The fix is that a bucket, not a global endpoint, decides where a path is read
from — see app/config.py's store_for and app/duck.py's scoped secrets.
"""
from __future__ import annotations

import importlib
import io
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import config

REAL_ENDPOINT = "http://127.0.0.1:9802"
REAL_BUCKET = "acme-lake"


@pytest.fixture
def reloaded_config():
    """Reload config.py against a chosen environment, then put it back.

    The store settings are import-time constants derived from each other, so
    the only honest way to test how a *deployment* resolves is to re-import
    the module the way that deployment's process would.
    """
    original = dict(os.environ)

    def reload(**env):
        for key in list(os.environ):
            if key.startswith(("CI_", "AWS_")):
                os.environ.pop(key)
        os.environ["CI_ENV_FILE"] = ""
        os.environ.update(env)
        return importlib.reload(config)

    yield reload
    os.environ.clear()
    os.environ.update(original)
    importlib.reload(config)


# ── how a deployment resolves ────────────────────────────────────────────

def test_the_zero_config_demo_is_one_store(reloaded_config):
    cfg = reloaded_config()
    assert cfg.BUCKET == cfg.DEMO_BUCKET
    assert not cfg.stores_split()
    assert cfg.EMBEDDED_EMULATOR
    assert cfg.resolve_credentials()[0] == "testing"


def test_a_real_bucket_separates_the_stores_and_keeps_the_emulator(reloaded_config):
    """The regression, at the level it starts: naming a real bucket must not
    take the demo store down with it."""
    cfg = reloaded_config(CI_BUCKET=REAL_BUCKET, AWS_ACCESS_KEY_ID="AKIAX",
                          AWS_SECRET_ACCESS_KEY="sek", AWS_REGION="eu-west-2")
    assert cfg.stores_split()
    assert cfg.EMBEDDED_EMULATOR, "the demo bucket lost its emulator"
    assert cfg.store_for(REAL_BUCKET) == cfg.primary_store()
    assert cfg.store_for(cfg.DEMO_BUCKET) == cfg.demo_store()
    # and the demo store is never opened with the real credential
    assert cfg.demo_store().credentials() == cfg.DEMO_CREDENTIALS
    assert cfg.primary_store().credentials()[0] == "AKIAX"


def test_real_aws_is_addressed_by_region_not_by_a_pinned_endpoint(reloaded_config):
    """A pinned s3.amazonaws.com sends every request to the us-east-1
    frontend, which answers for a bucket living anywhere else with a redirect
    nobody follows. Left empty, both boto3 and DuckDB build
    <bucket>.s3.<region>.amazonaws.com themselves."""
    cfg = reloaded_config(CI_BUCKET=REAL_BUCKET, AWS_ACCESS_KEY_ID="AKIAX",
                          AWS_SECRET_ACCESS_KEY="sek", AWS_REGION="eu-west-2")
    store = cfg.primary_store()
    assert store.endpoint == ""
    assert store.aws and store.use_ssl and not store.path_style
    assert store.host == ""      # nothing for DuckDB's ENDPOINT to pin
    assert store.label == "s3.eu-west-2.amazonaws.com"   # what it derives instead


def test_an_s3_compatible_endpoint_gets_path_style_over_its_own_scheme(reloaded_config):
    cfg = reloaded_config(CI_S3_ENDPOINT="http://minio:9000", CI_BUCKET="lake",
                          AWS_ACCESS_KEY_ID="m", AWS_SECRET_ACCESS_KEY="m")
    store = cfg.primary_store()
    assert store.path_style and not store.use_ssl and store.host == "minio:9000"
    assert cfg.stores_split() and cfg.EMBEDDED_EMULATOR


def test_one_endpoint_for_everything_stays_one_store(reloaded_config):
    """The MinIO compose profile and the test suite both put the demo bucket
    on the external endpoint. Nothing about that changed."""
    cfg = reloaded_config(CI_S3_ENDPOINT="http://minio:9000",
                          AWS_ACCESS_KEY_ID="m", AWS_SECRET_ACCESS_KEY="m")
    assert not cfg.stores_split()
    assert not cfg.EMBEDDED_EMULATOR


def test_demo_off_leaves_no_demo_store_and_no_built_in_catalog(reloaded_config):
    cfg = reloaded_config(CI_DEMO="0", CI_BUCKET=REAL_BUCKET,
                          AWS_ACCESS_KEY_ID="AKIAX", AWS_SECRET_ACCESS_KEY="sek")
    assert not cfg.DEMO_ENABLED and not cfg.EMBEDDED_EMULATOR
    # nothing anywhere should still be pretending there are two stores
    assert not cfg.stores_split()
    assert cfg.store_for(cfg.DEMO_BUCKET) == cfg.primary_store()


def test_an_empty_setting_reads_as_unset(reloaded_config):
    """`${VAR:-}` in docker-compose.yml is how one compose file forwards a
    setting a developer may or may not have exported — it arrives as an empty
    string. Reading that literally is how a container ends up with no
    credentials, an S3 endpoint of "", or (before this) a crash on
    int("") before the app could even print why."""
    cfg = reloaded_config(CI_S3_ENDPOINT="", CI_BUCKET="", AWS_ACCESS_KEY_ID="",
                          AWS_SECRET_ACCESS_KEY="", AWS_PROFILE="", AWS_REGION="",
                          CI_DEMO="", CI_DUCKDB_THREADS="", CI_DUCKDB_MEMORY_LIMIT="",
                          CI_LIST_MAX_KEYS="", CI_SOURCE_CACHE_TTL="", CI_LLM_MODEL="",
                          CI_DB_PATH="", CI_MODELS_DIR="")
    assert cfg.BUCKET == cfg.DEMO_BUCKET and cfg.EMBEDDED_EMULATOR
    assert cfg.resolve_credentials()[0] == "testing"
    assert cfg.DUCKDB_THREADS is None and cfg.LIST_MAX_KEYS == 20_000
    assert cfg.LLM_MODEL and cfg.MODELS_DIR.name == "models"


def test_no_key_and_no_profile_falls_through_to_boto3s_own_chain(reloaded_config):
    """An instance/task role (EC2, ECS, EKS) is a credential nobody types.
    What must never happen is the emulator's "testing" going to real AWS."""
    cfg = reloaded_config(CI_BUCKET=REAL_BUCKET)
    assert cfg.AWS_ACCESS_KEY_ID == ""


# ── which store a path is actually read from ─────────────────────────────

def test_duckdb_scopes_a_secret_per_store(seeded, monkeypatch):
    """DuckDB picks between secrets by longest matching scope, and that is
    the whole mechanism: one query can read a demo path and a real path from
    two endpoints with two credentials."""
    from app import duck

    demo = config.Store(name="demo", endpoint="http://127.0.0.1:9600",
                        region="us-east-1", demo=True)
    monkeypatch.setattr(config, "BUCKET", REAL_BUCKET)
    monkeypatch.setattr(config, "DEMO_S3_ENDPOINT", demo.endpoint)
    duck._store_secrets.clear()
    try:
        con = duck.connection()
        which = lambda path: con.execute(  # noqa: E731
            f"FROM which_secret('{path}', 's3')").fetchone()[0]
        assert which(f"s3://{config.DEMO_BUCKET}/sales/2024.parquet") == "cash_intel_demo_s3"
        assert which(f"s3://{REAL_BUCKET}/warehouse/orders.parquet") == "cash_intel_s3"
    finally:
        duck._store_secrets.clear()
        duck.connection()


def test_the_demo_secret_is_dropped_again_when_the_stores_rejoin(seeded, monkeypatch):
    from app import duck

    monkeypatch.setattr(config, "BUCKET", REAL_BUCKET)
    monkeypatch.setattr(config, "DEMO_S3_ENDPOINT", "http://127.0.0.1:9600")
    duck._store_secrets.clear()
    con = duck.connection()
    assert con.execute("SELECT count(*) FROM duckdb_secrets() "
                       "WHERE name = 'cash_intel_demo_s3'").fetchone()[0] == 1
    monkeypatch.undo()
    duck.connection()
    assert con.execute("SELECT count(*) FROM duckdb_secrets() "
                       "WHERE name = 'cash_intel_demo_s3'").fetchone()[0] == 0


def test_a_stale_secret_cannot_outlive_the_config_that_wrote_it(seeded, monkeypatch):
    """The installed set has to be a function of the current config alone,
    not of what this process remembers installing. A memo cleared without the
    connection being closed (a test moving an endpoint, a reset between
    fixtures) would otherwise leave a secret behind — still scoped to the
    demo bucket, still pointing at an endpoint nothing is serving, and every
    demo read failing with a connection error against a port from a previous
    configuration."""
    from app import duck

    monkeypatch.setattr(config, "BUCKET", REAL_BUCKET)
    monkeypatch.setattr(config, "DEMO_S3_ENDPOINT", "http://127.0.0.1:9600")
    duck._store_secrets.clear()
    con = duck.connection()
    live = lambda: con.execute(          # noqa: E731
        "SELECT count(*) FROM duckdb_secrets() WHERE name = 'cash_intel_demo_s3'").fetchone()[0]
    assert live() == 1

    monkeypatch.undo()
    duck._store_secrets.clear()          # the memo is gone; the secret is not
    duck.connection()
    assert live() == 0


def test_browsable_buckets_covers_both_stores(monkeypatch):
    from app import s3

    assert s3.browsable_buckets() == [(config.BUCKET, "")]
    monkeypatch.setattr(config, "BUCKET", REAL_BUCKET)
    monkeypatch.setattr(config, "DEMO_S3_ENDPOINT", "http://127.0.0.1:9600")
    monkeypatch.setattr(config, "BUCKET_PREFIX", "warehouse/")
    assert s3.browsable_buckets() == [(REAL_BUCKET, "warehouse/"), (config.DEMO_BUCKET, "")]


# ── bounded listings ─────────────────────────────────────────────────────

def test_a_bucket_walk_stops_at_the_cap_and_says_so(seeded, monkeypatch):
    """S3 hands back 1000 keys a round trip, so an unbounded walk of a real
    bucket is not a slow page, it is a page that never finishes."""
    from app import s3

    objects, truncated = s3.walk(config.BUCKET, limit=3)
    assert len(objects) == 3 and truncated
    objects, truncated = s3.walk(config.BUCKET)
    assert len(objects) > 3 and not truncated


def test_a_glob_too_wide_to_cache_is_left_for_duckdb_to_resolve(seeded, monkeypatch):
    """Past the cap, resolving a glob into an explicit file list stops being
    the cheaper option — hand DuckDB the prefix instead of building SQL with
    tens of thousands of literals in it."""
    from app import cache, duck

    cache.clear()          # the pin/listing decisions are cached per path
    path = f"s3://{config.BUCKET}/sales/*.parquet"
    listing = duck._list_objects(path)
    assert listing and len(listing[0]) > 1
    monkeypatch.setattr(config, "LIST_MAX_KEYS", 1)
    cache.clear()
    assert duck._list_objects(path) is None
    assert duck._scan_sql("parquet", path) == f"read_parquet('{path}')"


# ── the two stores, end to end ───────────────────────────────────────────

@pytest.fixture
def real_store(seeded, monkeypatch):
    """A second moto server standing in for real S3, holding a real bucket,
    with the demo bucket left where it is on the first one."""
    from moto.server import ThreadedMotoServer

    from app import cache, duck, s3

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=9802, verbose=False)
    server.start()
    try:
        # Start cold. Small sources get pinned as local DuckDB tables, so a
        # demo query run after an earlier test in this session would answer
        # from that pin and never touch a store at all — which is exactly the
        # thing under test here.
        cache.clear()
        duck.invalidate()
        monkeypatch.setattr(config, "BUCKET", REAL_BUCKET)
        monkeypatch.setattr(config, "S3_ENDPOINT", REAL_ENDPOINT)
        # the demo bucket stays on the suite's own moto server (conftest's
        # TEST_ENDPOINT) while everything else moves to the new one
        monkeypatch.setattr(config, "DEMO_S3_ENDPOINT", "http://127.0.0.1:9700")
        s3.reset()
        duck._store_secrets.clear()
        duck._bucket_secrets.clear()
        client = s3.client(REAL_BUCKET)
        client.create_bucket(Bucket=REAL_BUCKET)
        table = pa.table({"region": ["north", "south"] * 3, "amount": [1.0] * 6})
        buf = io.BytesIO()
        pq.write_table(table, buf)
        client.put_object(Bucket=REAL_BUCKET, Key="warehouse/orders/2026.parquet",
                          Body=buf.getvalue())
        yield client
    finally:
        server.stop()
        monkeypatch.undo()
        s3.reset()
        duck._store_secrets.clear()
        duck._bucket_secrets.clear()
        cache.clear()
        duck.invalidate()
        duck.connection()


def test_a_demo_model_and_a_real_bucket_both_answer(real_store, client):
    """The reported failure, end to end: with a real store configured, a demo
    model's visual came back 404 NoSuchBucket while the real model worked.

    Both halves answering is what this asserts. It does not assert *which*
    endpoint each one reached — moto keeps one backend per process, shared by
    every server it runs, so the two here are the same bucket universe wearing
    two ports. The routing itself is pinned by the two tests below, which put
    one store somewhere nothing is listening."""
    real = client.post("/api/models", json={"yaml": f"""
name: acme_orders
label: Acme Orders
source: {{format: parquet, path: s3://{REAL_BUCKET}/warehouse/orders/*.parquet}}
dimensions:
  - name: region
measures:
  - name: total
    expr: SUM(amount)
"""})
    assert real.status_code == 201, real.text
    try:
        answer = client.post("/api/query", json={
            "model": "acme_orders", "dimensions": ["region"], "measures": ["total"]})
        assert answer.status_code == 200, answer.text
        assert answer.json()["row_count"] == 2

        demo = client.post("/api/query", json={
            "model": "sales", "dimensions": ["region"], "measures": ["revenue"]})
        assert demo.status_code == 200, demo.text
        assert demo.json()["row_count"] == 5
    finally:
        client.delete("/api/models/acme_orders")


def test_boto3_routes_each_bucket_to_its_own_endpoint(real_store):
    """The listing side of the same rule: a bucket, not a global setting,
    decides which endpoint and which credential a request is made with."""
    from app import s3

    assert s3.client(REAL_BUCKET).meta.endpoint_url == REAL_ENDPOINT
    assert s3.client(config.DEMO_BUCKET).meta.endpoint_url == "http://127.0.0.1:9700"


DEAD = "http://127.0.0.1:9899"      # nothing is listening here, on purpose


def test_a_demo_query_never_touches_the_primary_store(real_store, client, monkeypatch):
    """Point the primary store at a closed port and the demo models must not
    notice. Before scoped secrets they did: one secret covered every s3:// url,
    so a demo path was read with the real store's endpoint and credential."""
    from app import cache, duck

    monkeypatch.setattr(config, "S3_ENDPOINT", DEAD)
    duck._store_secrets.clear()
    cache.clear()
    duck.invalidate()
    answer = client.post("/api/query", json={
        "model": "sales", "dimensions": ["region"], "measures": ["revenue"]})
    assert answer.status_code == 200, answer.text
    assert answer.json()["row_count"] == 5


def test_a_real_bucket_query_never_touches_the_demo_store(real_store, client, monkeypatch):
    """And the other direction, so "it works" can't be the demo store quietly
    answering for everything."""
    from app import cache, duck

    created = client.post("/api/models", json={"yaml": f"""
name: dead_probe
label: Dead Probe
source: {{format: parquet, path: s3://{REAL_BUCKET}/warehouse/orders/*.parquet}}
dimensions:
  - name: region
measures:
  - name: total
    expr: SUM(amount)
"""})
    assert created.status_code == 201, created.text
    try:
        monkeypatch.setattr(config, "DEMO_S3_ENDPOINT", DEAD)
        duck._store_secrets.clear()
        cache.clear()
        duck.invalidate()
        answer = client.post("/api/query", json={
            "model": "dead_probe", "dimensions": ["region"], "measures": ["total"]})
        assert answer.status_code == 200, answer.text
        assert answer.json()["row_count"] == 2
    finally:
        client.delete("/api/models/dead_probe")


def test_dataset_discovery_reports_every_bucket_it_walked(real_store, client):
    body = client.get("/api/datasets").json()
    walked = {s["bucket"]: s for s in body["stores"]}
    assert set(walked) == {REAL_BUCKET, config.DEMO_BUCKET}
    assert walked[config.DEMO_BUCKET]["demo"] is True
    assert walked[REAL_BUCKET]["endpoint"] == REAL_ENDPOINT
    buckets = {ds["bucket"] for ds in body["datasets"]}
    assert buckets == {REAL_BUCKET, config.DEMO_BUCKET}
    # every dataset's path names its own bucket, so a picker holding both can
    # still build an object's url
    assert all(ds["path"].startswith(f"s3://{ds['bucket']}/") for ds in body["datasets"])


def test_seeding_never_writes_into_the_configured_real_bucket(real_store):
    """Demo data has no business landing in an account someone pays for."""
    from app import seed

    assert seed.seed_bucket() is False       # the demo bucket already has data
    keys = {o["Key"] for o in
            real_store.list_objects_v2(Bucket=REAL_BUCKET).get("Contents", [])}
    assert keys == {"warehouse/orders/2026.parquet"}
