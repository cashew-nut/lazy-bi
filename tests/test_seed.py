"""app.seed's raw_data/<dataset>/ upload: user-supplied source files,
committed to the repo, land in the same bucket as the generated demo data
(config.BUCKET) under <dataset>/<filename> — plain unmodeled objects, ready
to build a model on from the Modelling workspace's source picker. The repo
doesn't ship a raw_data/ dataset by default, so seeding must not error when
the directory is absent.
"""


def test_seed_bucket_succeeds_without_raw_data_dir(seeded):
    from app import config, s3

    client = s3.client()
    keys = {o["Key"] for o in client.list_objects_v2(Bucket=config.BUCKET)["Contents"]}
    assert "sales/2024.parquet" in keys
    # nothing raw_data-shaped got uploaded — there's no dataset dir to source it from
    assert not any(k.endswith(".md") for k in keys)


def test_create_bucket_sets_location_constraint_outside_us_east_1(seeded, monkeypatch):
    """Real S3 (and moto, faithfully) rejects a bare CreateBucket outside
    us-east-1 with IllegalLocationConstraintException — this is what
    surfaced as exactly that error the first time this app was pointed at a
    real bucket outside us-east-1."""
    from app import config, s3, seed

    monkeypatch.setattr(config, "AWS_REGION", "eu-west-1")
    client = s3.client()
    try:
        # must not raise
        seed._create_bucket(client, "probe-bucket-eu-west-1", "eu-west-1")
        client.head_bucket(Bucket="probe-bucket-eu-west-1")  # must exist
    finally:
        client.delete_bucket(Bucket="probe-bucket-eu-west-1")


def test_create_bucket_tolerates_denied_create_permission(monkeypatch):
    """A read-only identity pointed at a real, pre-existing bucket someone
    else manages typically has no s3:CreateBucket permission at all — that
    must not be fatal, since the bucket exists by construction of that use
    case (point this app at an existing bucket to read)."""
    import pytest
    from botocore.exceptions import ClientError

    from app import s3, seed

    client = s3.client()

    def deny(**kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied",
                       "Message": "no identity-based policy allows the s3:CreateBucket action"}},
            "CreateBucket")

    monkeypatch.setattr(client, "create_bucket", deny)
    seed._create_bucket(client, "probe", "us-east-1")  # must not raise

    def fail_differently(**kwargs):
        raise ClientError({"Error": {"Code": "SomeOtherError", "Message": "boom"}}, "CreateBucket")

    monkeypatch.setattr(client, "create_bucket", fail_differently)
    with pytest.raises(ClientError):
        seed._create_bucket(client, "probe", "us-east-1")  # only AccessDenied is tolerated


def test_local_data_dir_reuploaded_into_an_ephemeral_bucket(seeded, monkeypatch):
    """config.LOCAL_DATA_DIR (app/api/datasets.py's upload disk cache) is
    re-synced into the bucket the same way app/load_taxi.py's data_cache/ is
    — this is what makes an upload survive a restart against the embedded
    (in-memory) emulator, whose bucket is otherwise gone the moment the
    process exits."""
    from app import config, s3, seed

    monkeypatch.setattr(config.Store, "ephemeral", property(lambda self: True))
    (config.LOCAL_DATA_DIR / "resync_probe").mkdir(parents=True, exist_ok=True)
    (config.LOCAL_DATA_DIR / "resync_probe" / "f.csv").write_text("a,b\n1,2\n")
    try:
        assert seed.restore_local_uploads() == 1
        client = s3.client(config.BUCKET)
        keys = {o["Key"] for o in client.list_objects_v2(Bucket=config.BUCKET)["Contents"]}
        assert "local/resync_probe/f.csv" in keys
    finally:
        s3.client(config.BUCKET).delete_object(
            Bucket=config.BUCKET, Key="local/resync_probe/f.csv")


def test_local_data_dir_not_reuploaded_into_a_durable_bucket(seeded):
    """The restore exists for a bucket that forgets. Re-uploading into a real
    one on every start would resurrect anything deleted from outside the app
    and re-pay for bytes already sitting there — so a durable store gets
    nothing put back into it (config.Store.ephemeral is false for every
    endpoint that isn't the embedded emulator)."""
    from app import config, seed

    (config.LOCAL_DATA_DIR / "durable_probe").mkdir(parents=True, exist_ok=True)
    (config.LOCAL_DATA_DIR / "durable_probe" / "f.csv").write_text("a,b\n1,2\n")
    assert not config.primary_store().ephemeral
    assert seed.restore_local_uploads() == 0
