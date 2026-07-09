"""Query engine against the seeded emulator bucket: aggregation, filters,
joins, time grains, spine semantics, delta sources."""
import pytest

from app import engine


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
