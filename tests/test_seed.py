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
    monkeypatch.setattr(config, "BUCKET", "probe-bucket-eu-west-1")
    client = s3.client()
    try:
        seed._create_bucket(client)  # must not raise
        client.head_bucket(Bucket="probe-bucket-eu-west-1")  # must exist
    finally:
        client.delete_bucket(Bucket="probe-bucket-eu-west-1")


def test_local_data_dir_reuploaded_on_seed(seeded):
    """config.LOCAL_DATA_DIR (app/api/datasets.py's upload disk cache) is
    re-synced into the bucket the same way app/load_taxi.py's data_cache/ is
    — this is what makes an upload survive a restart against the embedded
    (in-memory) emulator, whose bucket is otherwise gone the moment the
    process exits."""
    from app import config, s3, seed

    (config.LOCAL_DATA_DIR / "resync_probe").mkdir(parents=True, exist_ok=True)
    (config.LOCAL_DATA_DIR / "resync_probe" / "f.csv").write_text("a,b\n1,2\n")
    try:
        client = s3.client()
        seed._upload_local_data(client)
        keys = {o["Key"] for o in client.list_objects_v2(Bucket=config.BUCKET)["Contents"]}
        assert "local/resync_probe/f.csv" in keys
    finally:
        s3.client().delete_object(Bucket=config.BUCKET, Key="local/resync_probe/f.csv")
