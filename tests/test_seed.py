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
