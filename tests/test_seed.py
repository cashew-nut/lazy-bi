"""app.seed's raw_data/<dataset>/ upload: user-supplied source files,
committed to the repo, land in the same bucket as the generated demo data
(config.BUCKET) under <dataset>/<filename> — plain unmodeled objects, ready
to build a model on from the Modelling workspace's source picker.
"""


def test_raw_data_uploaded_alongside_demo_data(seeded):
    from app import config, s3

    client = s3.client()
    keys = {o["Key"] for o in client.list_objects_v2(Bucket=config.BUCKET)["Contents"]}
    assert "clinical-ops-synthetic/studies.parquet" in keys
    assert "clinical-ops-synthetic/studies.csv" in keys
    assert "clinical-ops-synthetic/sites.parquet" in keys
    assert "clinical-ops-synthetic/milestones.parquet" in keys
    assert "clinical-ops-synthetic/recruitment.parquet" in keys
    # README.md isn't a dataset file — only .csv/.parquet get uploaded
    assert not any(k.startswith("clinical-ops-synthetic/") and k.endswith(".md") for k in keys)
