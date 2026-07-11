"""Dataset-discovery helpers + the /api/datasets endpoint (007-modelling-workspace).

The pure grouping/format-inference helpers are unit-tested without a bucket;
the endpoint is exercised against the seeded moto bucket via the shared client.
"""
from app import semantic


# ── pure helpers ────────────────────────────────────────────────

def test_infer_format_single_extension():
    assert semantic.infer_format(["sales/2021.parquet", "sales/2022.parquet"]) == ("parquet", False)
    assert semantic.infer_format(["ref/a.csv"]) == ("csv", False)


def test_infer_format_unrecognized_is_none():
    assert semantic.infer_format(["notes/readme.txt", "x"]) == (None, False)


def test_infer_format_mixed_is_ambiguous():
    fmt, ambiguous = semantic.infer_format(["d/a.parquet", "d/b.parquet", "d/c.csv"])
    assert fmt == "parquet"  # dominant
    assert ambiguous is True


def test_group_objects_prefix_grouping_and_glob():
    objs = [
        {"key": "sales/2021.parquet", "size": 10},
        {"key": "sales/2022.parquet", "size": 20},
        {"key": "ref/products.csv", "size": 5},
    ]
    datasets = {d["key"]: d for d in semantic.group_objects(objs, "cash-intel")}
    assert datasets["sales"]["path"] == "s3://cash-intel/sales/*.parquet"
    assert datasets["sales"]["format"] == "parquet"
    assert datasets["sales"]["object_count"] == 2
    assert datasets["sales"]["bytes"] == 30
    assert datasets["ref"]["path"] == "s3://cash-intel/ref/*.csv"
    assert datasets["ref"]["format"] == "csv"


def test_group_objects_root_level_glob():
    datasets = semantic.group_objects([{"key": "top.parquet", "size": 1}], "b")
    assert datasets[0]["key"] == ""
    assert datasets[0]["path"] == "s3://b/*.parquet"


def test_group_objects_delta_root_collapses():
    objs = [
        {"key": "logistics/shipments/_delta_log/00000.json", "size": 3},
        {"key": "logistics/shipments/part-0001.parquet", "size": 40},
        {"key": "logistics/shipments/part-0002.parquet", "size": 60},
    ]
    datasets = semantic.group_objects(objs, "cash-intel")
    # exactly one dataset, delta-rooted at the table dir (no /*.parquet glob)
    assert len(datasets) == 1
    ds = datasets[0]
    assert ds["key"] == "logistics/shipments"
    assert ds["format"] == "delta"
    assert ds["path"] == "s3://cash-intel/logistics/shipments"
    assert ds["object_count"] == 3
    assert ds["bytes"] == 103


def test_group_objects_drops_unrecognized_prefix():
    datasets = semantic.group_objects([{"key": "docs/readme.txt", "size": 1}], "b")
    assert datasets == []


def test_group_objects_empty():
    assert semantic.group_objects([], "b") == []


# ── endpoint (seeded moto bucket) ───────────────────────────────

def test_datasets_endpoint_lists_seeded_prefixes(client, seeded):
    body = client.get("/api/datasets").json()
    assert body["bucket"] == "cash-intel"
    by_key = {d["key"]: d for d in body["datasets"]}
    # seed layout: sales/*.parquet, ref/*.csv, logistics/shipments (delta)
    assert "sales" in by_key
    assert by_key["sales"]["format"] == "parquet"
    assert by_key["ref"]["format"] == "csv"
    assert "logistics/shipments" in by_key
    assert by_key["logistics/shipments"]["format"] == "delta"


def test_datasets_endpoint_maps_models(client, seeded):
    body = client.get("/api/datasets").json()
    by_key = {d["key"]: d for d in body["datasets"]}
    sales_readers = {m["name"] for m in by_key["sales"]["models"]}
    assert "sales" in sales_readers  # the sales model reads sales/*.parquet
