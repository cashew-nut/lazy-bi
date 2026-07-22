"""app.seed.seed_raw_data_bucket: user-supplied raw_data/<dataset>/ files get
uploaded into their own bucket (config.RAW_BUCKET), separate from the
generated demo data in config.BUCKET, and the upload is a no-op once seeded.
"""


def test_seed_raw_data_bucket_uploads_and_is_idempotent(moto_server):
    from app import config, s3, seed

    # may already be seeded by an earlier test's app lifespan (moto_server is
    # session-scoped) — assert on the bucket's contents, not this call's
    # return value, then confirm a further call is a no-op either way.
    seed.seed_raw_data_bucket()
    client = s3.client()
    keys = sorted(o["Key"] for o in client.list_objects_v2(Bucket=config.RAW_BUCKET)["Contents"])
    assert "clinical-ops-synthetic/studies.parquet" in keys
    assert "clinical-ops-synthetic/studies.csv" in keys
    assert "clinical-ops-synthetic/sites.parquet" in keys
    assert "clinical-ops-synthetic/milestones.parquet" in keys
    assert "clinical-ops-synthetic/recruitment.parquet" in keys
    # README.md isn't a dataset file — only .csv/.parquet get uploaded
    assert not any(k.endswith(".md") for k in keys)

    # already has data -> skipped, no duplicate work
    assert seed.seed_raw_data_bucket() is False
