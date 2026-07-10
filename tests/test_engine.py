"""Query engine against the seeded emulator bucket: aggregation, filters,
joins, time grains, spine semantics, delta sources."""
import io

import polars as pl
import pytest

from app import config, engine, s3, semantic


def run(models, model, **query):
    return engine.run_query(models[model], query)


def test_group_by_dimension(models):
    r = run(models, "sales", dimensions=["region"], measures=["revenue"])
    assert r["row_count"] == 5
    assert all(row["revenue"] > 0 for row in r["rows"])
    # default sort: first measure desc
    revs = [row["revenue"] for row in r["rows"]]
    assert revs == sorted(revs, reverse=True)


def test_grand_total_without_dimensions(models):
    r = run(models, "sales", dimensions=[], measures=["revenue", "orders"])
    assert r["row_count"] == 1
    assert r["rows"][0]["orders"] > 0


def test_time_grain_truncation(models):
    r = run(models, "sales", dimensions=[{"name": "order_date", "grain": "1y"}], measures=["revenue"])
    assert [row["order_date"][:10] for row in r["rows"]] == ["2024-01-01", "2025-01-01", "2026-01-01"]


def test_filters(models):
    base = run(models, "sales", dimensions=["region"], measures=["orders"])
    filtered = run(models, "sales", dimensions=["region"], measures=["orders"],
                   filters=[{"field": "region", "op": "in", "values": ["Badlands"]}])
    assert filtered["row_count"] == 1
    assert filtered["rows"][0]["region"] == "Badlands"
    assert filtered["rows"][0]["orders"] < max(r["orders"] for r in base["rows"])


def test_join_columns_usable(models):
    r = run(models, "sales", dimensions=["supplier"], measures=["revenue"],
            filters=[{"field": "tier", "op": "ne", "value": "street-grade"}])
    assert r["row_count"] > 0
    assert all(row["supplier"] for row in r["rows"])


def test_delta_source(models):
    r = run(models, "logistics", dimensions=["courier"], measures=["shipments"])
    assert r["row_count"] == 4
    assert sum(row["shipments"] for row in r["rows"]) == 20_000


def test_spine_timeline_grows(models):
    r = run(models, "subscriptions",
            dimensions=[{"name": "active_at", "grain": "1y"}], measures=["active_customers"])
    counts = [row["active_customers"] for row in r["rows"]]
    assert len(counts) >= 2
    assert counts == sorted(counts)  # growing business in the demo data


def test_spine_snapshot_without_grouping(models):
    r = run(models, "subscriptions", dimensions=[], measures=["active_customers"],
            filters=[{"field": "active_at", "op": "eq", "value": "2026-01-01"}])
    assert r["row_count"] == 1
    assert 0 < r["rows"][0]["active_customers"] < 9000


def test_spine_window_bounds_timeline(models):
    r = run(models, "subscriptions",
            dimensions=[{"name": "active_at", "grain": "1mo"}], measures=["active_customers"],
            filters=[{"field": "active_at", "op": "gte", "value": "2026-01-01"},
                     {"field": "active_at", "op": "lte", "value": "2026-03-01"}])
    assert all(row["active_at"].startswith("2026-0") for row in r["rows"])
    assert r["row_count"] == 3


def test_geo_dimension_carries_coordinates(models):
    r = run(models, "marketing", dimensions=["region"], measures=["spend"])
    assert "__lat_region" in r["rows"][0] and "__lon_region" in r["rows"][0]
    # hidden coordinates never appear as declared columns
    assert all(not c["name"].startswith("__") for c in r["columns"])


def test_unknown_measure_rejected(models):
    with pytest.raises(Exception):
        run(models, "sales", dimensions=[], measures=["nope"])


def test_no_measures_rejected(models):
    with pytest.raises(engine.QueryError, match="measure"):
        run(models, "sales", dimensions=["region"], measures=[])


def test_dimension_values(models):
    values = engine.dimension_values(models["sales"], "channel")
    assert values == sorted(values)
    assert "web" in values


def test_spine_dimension_has_no_stored_values(models):
    with pytest.raises(engine.QueryError, match="timeline"):
        engine.dimension_values(models["subscriptions"], "active_at")


# --- Dimension bundle imports (real `geography` bundle -> `sales`) ---------

def test_imported_dimension_queryable_like_native(models):
    r = run(models, "sales", dimensions=["region"], measures=["revenue"])
    assert r["row_count"] == 5  # unchanged from the pre-import native `region` behavior


def test_imported_transitive_dimension_groups_correctly(models):
    r = run(models, "sales", dimensions=["territory_name"], measures=["revenue"])
    # 5 regions collapse into 3 territories (see app/seed.py TERRITORIES)
    assert r["row_count"] == 3
    assert all(row["revenue"] > 0 for row in r["rows"])


def test_imported_dimension_filters_with_existing_ops(models):
    r = run(models, "sales", dimensions=["territory_name"], measures=["orders"],
            filters=[{"field": "territory_name", "op": "eq", "value": "EMEA"}])
    assert r["row_count"] == 1
    assert r["rows"][0]["territory_name"] == "EMEA"


def test_imported_dimension_carries_geo(models):
    r = run(models, "sales", dimensions=["region"], measures=["revenue"])
    assert "__lat_region" in r["rows"][0] and "__lon_region" in r["rows"][0]


def test_scan_with_imports_stays_lazy(models):
    assert isinstance(engine.scan(models["sales"]), pl.LazyFrame)


def test_geography_bundle_shared_across_two_fact_models(models):
    # sales and logistics both import `geography` independently — proves
    # reuse (spec SC-001/SC-003), not a one-off wiring that happens to work
    # for a single model
    for model_name in ("sales", "logistics"):
        r = run(models, model_name, dimensions=["territory_name"],
                measures=["revenue" if model_name == "sales" else "shipments"])
        assert r["row_count"] == 3
        assert {row["territory_name"] for row in r["rows"]} == {"North America", "Pacific Rim", "EMEA"}


# --- Synthetic fixture for import edge cases the real demo data doesn't hit:
# an unmatched anchor row (region "Z" has no lookup match) and inner-join
# row-dropping.

@pytest.fixture(scope="module")
def import_edge_cases(seeded):
    client = s3.client()
    client.put_object(Bucket=config.BUCKET, Key="test/import_regions.csv",
                       Body=b"region,territory\nA,T1\nB,T2\n")
    client.put_object(Bucket=config.BUCKET, Key="test/import_territories.csv",
                       Body=b"territory,name\nT1,Territory One\nT2,Territory Two\n")
    buf = io.BytesIO()
    pl.DataFrame({"id": [1, 2, 3], "region": ["A", "B", "Z"], "amount": [10, 20, 30]}).write_parquet(buf)
    client.put_object(Bucket=config.BUCKET, Key="test/import_fact.parquet", Body=buf.getvalue())

    bundle = semantic.parse_bundle_text(f"""
name: test_geo
datasets:
  - name: regions
    source: {{format: csv, path: s3://{config.BUCKET}/test/import_regions.csv}}
    dimensions: [{{name: region, label: Region}}, {{name: territory, label: Territory Code}}]
    joins: [{{to: territories, on: territory}}]
  - name: territories
    source: {{format: csv, path: s3://{config.BUCKET}/test/import_territories.csv}}
    dimensions: [{{name: territory_name, column: name, label: Territory}}]
""")

    def make_model(how="left"):
        model = semantic.parse_model_text(f"""
name: test_fact
source: {{format: parquet, path: s3://{config.BUCKET}/test/import_fact.parquet}}
dimensions: [{{name: id, label: Id}}]
measures: [{{name: total, expr: pl.col("amount").sum()}}]
dimension_imports:
  - {{bundle: test_geo, anchor_dataset: regions, on: region, how: {how}}}
""")
        semantic.resolve_imports(model, {"test_geo": bundle})
        return model

    return make_model


def test_import_left_join_keeps_unmatched_anchor_rows(import_edge_cases):
    model = import_edge_cases(how="left")
    r = engine.run_query(model, {"dimensions": [], "measures": ["total"]})
    assert r["rows"][0]["total"] == 60  # all 3 rows counted; "Z" just has null territory_name

    by_territory = engine.run_query(model, {"dimensions": ["territory_name"], "measures": ["total"]})
    values = {row["territory_name"]: row["total"] for row in by_territory["rows"]}
    assert values.get("Territory One") == 10
    assert values.get("Territory Two") == 20
    assert None in values  # unmatched "Z" row forms its own null group, not dropped


def test_import_inner_join_drops_unmatched_anchor_rows(import_edge_cases):
    model = import_edge_cases(how="inner")
    r = engine.run_query(model, {"dimensions": [], "measures": ["total"]})
    assert r["rows"][0]["total"] == 30  # only "A" (10) + "B" (20); unmatched "Z" is dropped
