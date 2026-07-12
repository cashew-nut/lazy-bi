"""Query engine against the seeded emulator bucket: aggregation, filters,
joins, time grains, spine semantics, delta sources."""
import io
from datetime import date, timedelta

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


def test_relative_date_filter_matches_resolved_fixed_date(models):
    # "today" on a plain time column must behave exactly like the ISO date
    # it resolves to today, re-evaluated at query time rather than baked in.
    today = date.today().isoformat()
    dynamic = run(models, "sales", dimensions=["region"], measures=["orders"],
                  filters=[{"field": "order_date", "op": "lte", "value": "today"}])
    fixed = run(models, "sales", dimensions=["region"], measures=["orders"],
                filters=[{"field": "order_date", "op": "lte", "value": today}])
    assert dynamic["rows"] == fixed["rows"]


def test_relative_date_filter_on_spine_dimension(models):
    today = date.today().isoformat()
    dynamic = run(models, "subscriptions", dimensions=[], measures=["active_customers"],
                  filters=[{"field": "active_at", "op": "lte", "value": "today"}])
    fixed = run(models, "subscriptions", dimensions=[], measures=["active_customers"],
                filters=[{"field": "active_at", "op": "lte", "value": today}])
    assert dynamic["rows"] == fixed["rows"]


@pytest.mark.parametrize("token", ["today", "TODAY", "Start_Of_Month", "today-7d", "today+2w", "not_a_token"])
def test_resolve_relative_date(token):
    ref = date(2026, 7, 11)  # a Saturday
    resolved = engine.resolve_relative_date(token, today=ref)
    expected = {
        "today": ref, "TODAY": ref,
        "Start_Of_Month": date(2026, 7, 1),
        "today-7d": date(2026, 7, 4),
        "today+2w": date(2026, 7, 25),
        "not_a_token": None,
    }[token]
    assert resolved == expected


def test_resolve_relative_date_month_and_quarter_boundaries():
    ref = date(2026, 7, 11)
    assert engine.resolve_relative_date("end_of_month", today=ref) == date(2026, 7, 31)
    assert engine.resolve_relative_date("start_of_quarter", today=ref) == date(2026, 7, 1)
    assert engine.resolve_relative_date("end_of_quarter", today=ref) == date(2026, 9, 30)
    assert engine.resolve_relative_date("start_of_year", today=ref) == date(2026, 1, 1)
    assert engine.resolve_relative_date("end_of_year", today=ref) == date(2026, 12, 31)
    # crossing a year boundary via month offset
    assert engine.resolve_relative_date("today-8mo", today=ref) == date(2025, 11, 11)


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
measures: [{{name: total, expr: sum(amount)}}]
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


# --- Framed measures: expr aggregates over an intermediary derived frame ---
# Synthetic event log with hand-computable answers: per study, the "days to
# reach 75% of that study's events" is the date of the ceil(0.75 * n)-th
# event minus the first event's date.
#   S1 (cohort X): events on days 0/10/20/30 -> 3rd of 4  -> 20
#   S2 (cohort X): events on days 0/100      -> 2nd of 2  -> 100
#   S3 (cohort Y): events on days 0/5/8      -> 3rd of 3  -> 8

@pytest.fixture(scope="module")
def framed_model(seeded):
    days = {"S1": [0, 10, 20, 30], "S2": [0, 100], "S3": [0, 5, 8]}
    cohort = {"S1": "X", "S2": "X", "S3": "Y"}
    rows = [
        {"study_id": sid, "cohort": cohort[sid], "event_date": date(2025, 1, 1) + timedelta(days=d)}
        for sid, offsets in days.items() for d in offsets
    ]
    buf = io.BytesIO()
    pl.DataFrame(rows).write_parquet(buf)
    s3.client().put_object(Bucket=config.BUCKET, Key="test/framed_events.parquet", Body=buf.getvalue())

    return semantic.parse_model_text(f"""
name: test_framed
source: {{format: parquet, path: s3://{config.BUCKET}/test/framed_events.parquet}}
dimensions:
  - name: cohort
  - name: event_date
    type: time
measures:
  - name: events
    expr: count()
  - name: median_days_to_75
    frame: |
      keys = list(dict.fromkeys(["study_id", *dims]))
      ordered = lf.sort("event_date").with_columns(
          (pl.int_range(1, pl.len() + 1).over(keys) / pl.len().over(keys)).alias("cume"),
          pl.col("event_date").min().over(keys).alias("first_event"),
      )
      frame = (
          ordered.filter(pl.col("cume") >= 0.75)
          .group_by(keys)
          .agg(pl.col("first_event").first(), pl.col("event_date").min().alias("date_75"))
          .with_columns(
              (pl.col("date_75") - pl.col("first_event")).dt.total_days().alias("days_to_75"),
              pl.col("date_75").alias("event_date"),
          )
      )
    frame_emits: [event_date]
    expr: pl.col("days_to_75").median()
  - name: bad_frame_drops_dims
    frame: 'lf.group_by("study_id").agg(pl.len())'
    expr: pl.len()
  - name: bad_frame_emits_declared_not_output
    frame: |
      keys = list(dict.fromkeys(["study_id", *dims]))
      frame = lf.group_by(keys).agg(pl.len())
    frame_emits: [event_date]
    expr: pl.len()
""")


def test_framed_measure_grand_total(framed_model):
    r = engine.run_query(framed_model, {"dimensions": [], "measures": ["median_days_to_75"]})
    assert r["row_count"] == 1
    assert r["rows"][0]["median_days_to_75"] == 20.0  # median of {20, 100, 8}


def test_framed_measure_grouped_by_dimension(framed_model):
    r = engine.run_query(framed_model, {"dimensions": ["cohort"], "measures": ["median_days_to_75"]})
    values = {row["cohort"]: row["median_days_to_75"] for row in r["rows"]}
    assert values == {"X": 60.0, "Y": 8.0}  # X: median(20, 100); Y: median(8)


def test_framed_and_plain_measures_mix(framed_model):
    r = engine.run_query(framed_model, {"dimensions": ["cohort"], "measures": ["events", "median_days_to_75"]})
    values = {row["cohort"]: (row["events"], row["median_days_to_75"]) for row in r["rows"]}
    assert values == {"X": (6, 60.0), "Y": (3, 8.0)}


def test_framed_measure_respects_filters(framed_model):
    r = engine.run_query(framed_model, {
        "dimensions": [], "measures": ["median_days_to_75"],
        "filters": [{"field": "cohort", "op": "eq", "value": "X"}],
    })
    assert r["rows"][0]["median_days_to_75"] == 60.0


def test_model_frame_that_drops_dimensions_rejected(framed_model):
    with pytest.raises(engine.QueryError, match="lost dimension"):
        engine.run_query(framed_model, {"dimensions": ["cohort"], "measures": ["bad_frame_drops_dims"]})


def test_model_emitted_dimension_missing_from_frame_rejected(framed_model):
    with pytest.raises(engine.QueryError, match="frame_emits"):
        engine.run_query(framed_model, {
            "dimensions": [{"name": "event_date", "grain": "1mo"}],
            "measures": ["bad_frame_emits_declared_not_output"],
        })


def test_inline_frame_measure_rejected(framed_model):
    # frame-based measures are a model-measure-only, authenticated-path
    # construct (see specs/008-safe-measure-compilation) — inline/query-time
    # measures must never be able to run one, regardless of shape.
    with pytest.raises(engine.QueryError, match="authenticated model-measure save"):
        engine.run_query(framed_model, {
            "dimensions": ["cohort"], "measures": ["n_studies"],
            "inline_measures": [{
                "name": "n_studies",
                "frame": 'lf.group_by(["study_id", *dims]).agg(pl.len())',
                "expr": "pl.len()",
            }],
        })


def test_inline_frame_emits_rejected_even_without_frame(framed_model):
    with pytest.raises(engine.QueryError, match="authenticated model-measure save"):
        engine.run_query(framed_model, {
            "dimensions": ["cohort"], "measures": ["bad"],
            "inline_measures": [{
                "name": "bad",
                "frame_emits": ["event_date"],
                "expr": "pl.len()",
            }],
        })


def test_framed_measure_timeline_buckets_derived_rows(framed_model):
    # event_date is in frame_emits: the timeline buckets each study by its own
    # 75%-crossing date (the frame's output column), not by raw event months.
    # Crossings: S1 -> Jan 21 (20d), S3 -> Jan 9 (8d), S2 -> Apr 11 (100d).
    r = engine.run_query(framed_model, {
        "dimensions": [{"name": "event_date", "grain": "1mo"}],
        "measures": ["events", "median_days_to_75"],
    })
    rows = {row["event_date"][:10]: row for row in r["rows"]}
    assert rows["2025-01-01"]["median_days_to_75"] == 14.0  # median(20, 8)
    assert rows["2025-04-01"]["median_days_to_75"] == 100.0
    # the plain measure still buckets the raw events (8 in Jan, 1 in Apr)
    assert rows["2025-01-01"]["events"] == 8
    assert rows["2025-04-01"]["events"] == 1


def test_framed_measure_timeline_respects_grain(framed_model):
    r = engine.run_query(framed_model, {
        "dimensions": [{"name": "event_date", "grain": "1y"}],
        "measures": ["median_days_to_75"],
    })
    assert r["row_count"] == 1  # all three crossings land in 2025
    assert r["rows"][0]["median_days_to_75"] == 20.0




def test_clinical_framed_measure_end_to_end(models):
    # the shipped demo measure: median months from first actual randomisation
    # to the month cumulative randomisations crossed 75% of the study total
    r = run(models, "clinical_ops_recruitment",
            dimensions=[], measures=["median_months_to_75pct_randomised"])
    assert r["row_count"] == 1
    v = r["rows"][0]["median_months_to_75pct_randomised"]
    assert v is not None and 0 < v < 40


def test_clinical_framed_measure_grouped_with_plain(models):
    r = run(models, "clinical_ops_recruitment", dimensions=["therapeutic_area"],
            measures=["randomised_actual", "median_months_to_75pct_randomised"])
    assert r["row_count"] >= 3
    with_events = [row for row in r["rows"] if row["randomised_actual"] > 0]
    assert with_events and all(
        row["median_months_to_75pct_randomised"] > 0 for row in with_events)


def test_clinical_framed_measure_on_timeline(models):
    # bucketed by each study's 75% crossing quarter: only as many rows as
    # there are distinct crossing quarters (10 studies -> <= 10 buckets),
    # every bucketed median positive, and per-bucket study counts sum to the
    # number of studies that crossed at all
    r = run(models, "clinical_ops_recruitment",
            dimensions=[{"name": "event_date", "grain": "1q"}],
            measures=["median_months_to_75pct_randomised"])
    assert 1 <= r["row_count"] <= 10
    assert all(row["median_months_to_75pct_randomised"] > 0 for row in r["rows"])
