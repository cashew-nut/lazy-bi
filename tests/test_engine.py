"""Query engine against the seeded emulator bucket: aggregation, filters,
joins, time grains, spine semantics, interval imports, delta sources."""
import io
import re
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import cache, config, duck, engine, s3, semantic


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


# ── offsets on any keyword, not just "today" (the reported bug: chat kept
# proposing 'start_of_year-1y' for "last year" — the only sensible way to
# say it — and the engine raised a bare ValueError that crashed the SSE
# stream, because the offset form was hardcoded to the "today" keyword) ──

@pytest.mark.parametrize("token,expected", [
    ("start_of_year-1y", date(2025, 1, 1)),      # 1 Jan last year
    ("end_of_year-1y", date(2025, 12, 31)),      # 31 Dec last year
    ("start_of_month-1mo", date(2026, 6, 1)),    # 1st of last month
    ("end_of_month-1mo", date(2026, 6, 30)),     # last day of last month
    ("start_of_quarter-1q", date(2026, 4, 1)),   # start of last quarter
    ("end_of_quarter-1q", date(2026, 6, 30)),    # end of last quarter
    ("start_of_week+1w", date(2026, 7, 13)),     # Monday of next week
    ("today-90d", date(2026, 4, 12)),            # the pre-existing form
    ("START_OF_YEAR-1Y", date(2025, 1, 1)),      # case-insensitive, as bare keywords are
])
def test_resolve_relative_date_keyword_with_offset(token, expected):
    assert engine.resolve_relative_date(token, today=date(2026, 7, 11)) == expected


def test_keyword_offset_applies_the_offset_before_the_keyword():
    """A composed token always lands on a real period edge, because the
    offset shifts *today* and the keyword then takes that date's boundary.
    Shifting the resolved boundary instead would drag the month-length of
    the starting month along with it."""
    # end of next month from a 28-day month: March's own end, not Feb 28 + 1mo
    assert engine.resolve_relative_date("end_of_month+1mo", today=date(2026, 2, 15)) == date(2026, 3, 31)
    # and from a 31-day month into a 30-day one
    assert engine.resolve_relative_date("end_of_month+1mo", today=date(2026, 8, 3)) == date(2026, 9, 30)
    # last quarter across a year boundary
    assert engine.resolve_relative_date("end_of_quarter-1q", today=date(2026, 1, 5)) == date(2025, 12, 31)


@pytest.mark.parametrize("token", [
    "last_year", "last_month", "ytd",       # keywords that don't exist
    "start_of_year - 1 year",               # spelled-out arithmetic
    "today-1y+2d",                          # two offsets
    "start_of_year-1",                      # no unit
    "today-3x",                             # unit outside the five
])
def test_unresolvable_relative_tokens_are_not_dates(token):
    assert engine.resolve_relative_date(token, today=date(2026, 7, 11)) is None
    assert engine.date_value_error(token)  # …and are reported, not silently kept


def test_date_value_error_accepts_real_values():
    assert engine.date_value_error("2025-01-31") is None
    assert engine.date_value_error("start_of_year-1y") is None
    # a time component is invalid for a Date column but fine for a Datetime
    # one, and the dtype isn't known this far upstream
    assert engine.date_value_error("2025-01-31T09:00") is None
    assert engine.date_value_error(None) is None


def test_bad_date_filter_value_raises_query_error_not_value_error(models):
    """The crash this fixes: date.fromisoformat's bare ValueError went all
    the way up as an unhandled exception (a mid-stream ASGI failure on the
    chat SSE endpoint). Callers handle QueryError; none handle ValueError."""
    with pytest.raises(engine.QueryError) as exc:
        run(models, "sales", dimensions=[], measures=["revenue"],
            filters=[{"field": "order_date", "op": "gte", "value": "last_year"}])
    assert "last_year" in str(exc.value)
    # the message carries the grammar, so the failure is self-explaining
    assert "start_of_year" in str(exc.value)


def test_keyword_offset_filter_matches_the_equivalent_fixed_dates(models):
    """A composed token filters exactly as the dates it resolves to."""
    start = engine.resolve_relative_date("start_of_year-1y")
    end = engine.resolve_relative_date("end_of_year-1y")
    dynamic = run(models, "sales", dimensions=[], measures=["revenue"], filters=[
        {"field": "order_date", "op": "gte", "value": "start_of_year-1y"},
        {"field": "order_date", "op": "lte", "value": "end_of_year-1y"},
    ])
    fixed = run(models, "sales", dimensions=[], measures=["revenue"], filters=[
        {"field": "order_date", "op": "gte", "value": start.isoformat()},
        {"field": "order_date", "op": "lte", "value": end.isoformat()},
    ])
    assert dynamic["rows"] == fixed["rows"]


def test_join_columns_usable(models):
    r = run(models, "sales", dimensions=["supplier"], measures=["revenue"],
            filters=[{"field": "tier", "op": "ne", "value": "street-grade"}])
    assert r["row_count"] > 0
    assert all(row["supplier"] for row in r["rows"])


def test_delta_source(models):
    r = run(models, "logistics", dimensions=["courier"], measures=["shipments"])
    assert r["row_count"] == 4
    assert sum(row["shipments"] for row in r["rows"]) == 20_000


def test_iceberg_source(models):
    r = run(models, "support", dimensions=["priority"], measures=["tickets"])
    assert r["row_count"] == 4
    assert sum(row["tickets"] for row in r["rows"]) == 15_000


def test_iceberg_source_joined_dimension(models):
    r = run(models, "support", dimensions=["region"], measures=["tickets", "avg_resolution_hours"])
    assert r["row_count"] == 5
    assert all(row["tickets"] > 0 for row in r["rows"])


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


# --- Period matching: the worked two-record example -----------------------
# One record open from Jan 1st 2026 with no end, one open Feb 2nd-15th only.
# Both point-in-time mechanisms must place them in the same periods.

def _calendar(start: date, end: date, quarter: bool = False) -> "pa.Table":
    """A day-per-row date table, the shape a `how: between` import reads."""
    days = []
    day = start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    columns = {"date": pa.array(days, pa.date32()),
               "year": pa.array([d.year for d in days])}
    if quarter:
        columns["quarter"] = pa.array([f"{d.year}-Q{(d.month - 1) // 3 + 1}" for d in days])
    return pa.table(columns)


@pytest.fixture(scope="module")
def two_records(seeded):
    client = s3.client()
    buf = io.BytesIO()
    pq.write_table(pa.table({
        "id": pa.array(["A", "B"]),
        "start_date": pa.array([date(2026, 1, 1), date(2026, 2, 2)], pa.date32()),
        "end_date": pa.array([None, date(2026, 2, 15)], pa.date32()),
    }), buf)
    client.put_object(Bucket=config.BUCKET, Key="test/two_records.parquet", Body=buf.getvalue())

    cal = io.BytesIO()
    pq.write_table(_calendar(date(2026, 1, 1), date(2026, 12, 31), quarter=True), cal)
    client.put_object(Bucket=config.BUCKET, Key="test/two_records_cal.parquet", Body=cal.getvalue())

    bundle = semantic.parse_bundle_text(f"""
name: two_cal
datasets:
  - name: days
    source: {{format: parquet, path: s3://{config.BUCKET}/test/two_records_cal.parquet}}
    dimensions:
      - {{name: as_of, column: date, label: As Of, type: time}}
      - {{name: as_of_quarter, column: quarter, label: Quarter, grain: 1q}}
      - {{name: as_of_year, column: year, label: Year, type: numeric, grain: 1y}}
""")

    def build(match):
        model = semantic.parse_model_text(f"""
name: two_records
source: {{format: parquet, path: s3://{config.BUCKET}/test/two_records.parquet}}
dimension_imports:
  - {{bundle: two_cal, anchor_dataset: days, how: between,
      left_on: [start_date, end_date], right_on: date, match: {match}}}
dimensions:
  - {{name: active_at, label: Active At, type: time,
      spine: {{start: start_date, end: end_date, match: {match}}}}}
measures:
  - {{name: n, expr: COUNT(*)}}
""")
        return semantic.resolve_model(model, {"two_cal": bundle})

    return build


def test_overlap_counts_a_record_in_every_period_it_touches(two_records):
    """The stated example: a record open Feb 2nd-15th belongs to February, to
    Q1 and to the year, even though it spans none of their boundaries."""
    model = two_records("overlap")
    q = lambda **kw: engine.run_query(model, {"measures": ["n"], "limit": 50, **kw})

    months = q(dimensions=[{"name": "as_of", "grain": "1mo"}])
    assert [(r["as_of"][:7], r["n"]) for r in months["rows"]] == \
        [("2026-01", 1), ("2026-02", 2)] + [(f"2026-{m:02d}", 1) for m in range(3, 13)]

    quarters = q(dimensions=["as_of_quarter"])
    assert sorted((r["as_of_quarter"], r["n"]) for r in quarters["rows"]) == \
        [("2026-Q1", 2), ("2026-Q2", 1), ("2026-Q3", 1), ("2026-Q4", 1)]

    assert [(r["as_of_year"], r["n"]) for r in q(dimensions=["as_of_year"])["rows"]] == [(2026, 2)]


def test_overlap_holds_for_the_spine_too(two_records):
    """The generated timeline answers it the same way as the date table."""
    model = two_records("overlap")
    q = lambda grain: engine.run_query(model, {
        "dimensions": [{"name": "active_at", "grain": grain}], "measures": ["n"], "limit": 50})
    assert [(r["active_at"][:7], r["n"]) for r in q("1mo")["rows"]][:3] == \
        [("2026-01", 1), ("2026-02", 2), ("2026-03", 1)]
    assert q("1q")["rows"][0]["n"] == 2  # Q1


@pytest.mark.parametrize("match", ["period_start", "period_end"])
def test_snapshot_matches_exclude_a_record_that_spans_no_boundary(two_records, match):
    """The other two readings are snapshots, so the Feb 2nd-15th record — open
    on neither the 1st nor the 28th — is absent from every period."""
    model = two_records(match)
    q = lambda **kw: engine.run_query(model, {"measures": ["n"], "limit": 50, **kw})
    assert all(r["n"] == 1 for r in q(dimensions=[{"name": "as_of", "grain": "1mo"}])["rows"])
    assert all(r["n"] == 1 for r in q(dimensions=["as_of_quarter"])["rows"])
    assert [r["n"] for r in q(dimensions=["as_of_year"])["rows"]] == [1]


# --- Interval (`how: between`) imports: the `calendar` bundle -> subscriptions
# The same point-in-time question the spine answers, asked through a real,
# standalone date table that relates to the model by nothing but the interval.

def test_interval_import_dimension_groups_point_in_time(models):
    r = run(models, "subscriptions", dimensions=["calendar_date"], measures=["active_customers"],
            filters=[{"field": "calendar_date", "op": "gte", "value": "2025-03-01"},
                     {"field": "calendar_date", "op": "lte", "value": "2025-03-05"}])
    assert r["row_count"] == 5
    # a subscription is counted on every day it was open, not just its start day
    assert all(row["active_customers"] > 100 for row in r["rows"])


@pytest.mark.parametrize("grain", ["1d", "1w", "1mo", "1q", "1y"])
def test_interval_import_matches_the_generated_spine_at_every_grain(models, grain):
    """The whole point of narrowing the date table to the query's grain: the
    calendar and the spine answer the same question, so they must agree bucket
    for bucket as the grain picker moves — for the *additive* mrr as much as for
    a distinct count, since a per-day join would inflate the former."""
    spine = run(models, "subscriptions", dimensions=[{"name": "active_at", "grain": grain}],
                measures=["active_customers", "mrr"], limit=1000)
    cal = run(models, "subscriptions", dimensions=[{"name": "calendar_date", "grain": grain}],
              measures=["active_customers", "mrr"], limit=1000)
    by_bucket = {r["active_at"]: r for r in spine["rows"]}
    # the calendar's own range bounds it, so it can hold fewer buckets than the
    # spine's generated timeline — every bucket it does hold must match
    assert cal["row_count"] > 1
    for row in cal["rows"]:
        spine_row = by_bucket[row["calendar_date"]]
        assert spine_row["active_customers"] == row["active_customers"]
        assert spine_row["mrr"] == pytest.approx(row["mrr"])


@pytest.mark.parametrize("dimension,spine_grain", [
    ("calendar_month_start", "1mo"), ("calendar_quarter", "1q"), ("calendar_year", "1y"),
])
def test_interval_import_declared_grain_needs_no_grain_picker(models, dimension, spine_grain):
    """A column that is inherently periodic declares its own grain, so grouping
    by it alone — no grain on the query — still narrows the table correctly."""
    spine = run(models, "subscriptions", dimensions=[{"name": "active_at", "grain": spine_grain}],
                measures=["active_customers", "mrr"], limit=1000)
    cal = run(models, "subscriptions", dimensions=[dimension],
              measures=["active_customers", "mrr"], limit=1000)
    spine_values = {(r["active_customers"], round(r["mrr"], 6)) for r in spine["rows"]}
    assert cal["row_count"] > 1
    for row in cal["rows"]:
        assert (row["active_customers"], round(row["mrr"], 6)) in spine_values


def test_interval_import_mixed_grains_uses_the_finest(models):
    """Quarter (declared 1q) alongside a day-grain date: the day wins, and the
    quarter is then just an attribute of that day."""
    r = run(models, "subscriptions",
            dimensions=["calendar_quarter", {"name": "calendar_date", "grain": "1d"}],
            measures=["active_customers"],
            filters=[{"field": "calendar_date", "op": "gte", "value": "2025-03-01"},
                     {"field": "calendar_date", "op": "lte", "value": "2025-03-05"}])
    assert r["row_count"] == 5
    assert {row["calendar_quarter"] for row in r["rows"]} == {"2025-Q1"}


def test_interval_import_match_modes_rank_as_expected(models):
    """On real data with churn: whatever was open on a month's first or last
    day was open during that month, so overlap dominates both snapshots — and
    strictly so wherever a subscription lived and died inside one month."""
    subs = models["subscriptions"]
    binding = next(b for b in subs.import_bindings if b.import_spec.is_interval)
    assert binding.import_spec.match == "overlap"
    query = {"dimensions": [{"name": "calendar_date", "grain": "1mo"}],
             "measures": ["active_customers"], "limit": 1000}

    def counts(match):
        binding.import_spec.match = match
        try:
            return {r["calendar_date"]: r["active_customers"] for r in engine.run_query(subs, query)["rows"]}
        finally:
            binding.import_spec.match = "overlap"

    overlap, at_start, at_end = counts("overlap"), counts("period_start"), counts("period_end")
    assert set(at_start) <= set(overlap) and set(at_end) <= set(overlap)
    for bucket, n in at_start.items():
        assert overlap[bucket] >= n
    for bucket, n in at_end.items():
        assert overlap[bucket] >= n
    assert any(overlap[b] > at_start[b] for b in at_start)
    assert any(overlap[b] > at_end[b] for b in at_end)


def test_interval_import_carries_its_own_attributes(models):
    r = run(models, "subscriptions", dimensions=["calendar_quarter"], measures=["active_customers"],
            limit=50)
    assert all(re.fullmatch(r"20\d\d-Q[1-4]", row["calendar_quarter"]) for row in r["rows"])


def test_interval_import_skipped_when_no_calendar_dimension_used(models):
    """The join multiplies rows by the periods each spans — a query that never
    touches the calendar must not pay for it, or every measure would inflate."""
    plain = run(models, "subscriptions", dimensions=["plan"], measures=["signups"])
    assert sum(row["signups"] for row in plain["rows"]) == 9000  # one row per customer
    subs = models["subscriptions"]
    # the bundle arrives with its dimensions under their dimension names, never
    # its own raw column names — see engine._scan_bundle
    assert "calendar_quarter" not in duck.relation_schema(engine.scan(subs, {"plan": None}))
    assert "calendar_quarter" in duck.relation_schema(engine.scan(subs, {"calendar_quarter": None}))
    assert "calendar_quarter" in duck.relation_schema(engine.scan(subs))  # None = introspection
    assert "quarter" not in duck.relation_schema(engine.scan(subs))


def test_matching_columns_import_skipped_when_none_of_its_dimensions_are_used(models):
    """A `how: left` import can only add columns, so a query reading none of
    them must not pay for the join — sales imports the calendar purely to
    conform with its neighbours, and most sales queries never touch it."""
    sales = models["sales"]
    plain = duck.relation_schema(engine.scan(sales, {"category": None}))
    assert "calendar_quarter" not in plain
    assert "territory" not in plain               # geography is `left` too
    assert "category" in plain                    # ...the model's own is untouched
    # ...but anything that reads one keeps it, whether grouped by or filtered on
    assert "calendar_quarter" in duck.relation_schema(engine.scan(sales, {"calendar_quarter": None}))
    assert "territory" in duck.relation_schema(engine.scan(sales, {"territory": None}))
    assert "calendar_quarter" in duck.relation_schema(engine.scan(sales))  # None = introspection


def test_skipping_an_import_does_not_change_a_measure(models):
    """The skip is only sound because a left join to a dimension table adds
    columns and nothing else — the numbers have to be identical either way."""
    without = run(models, "sales", dimensions=["category"], measures=["revenue"])
    with_join = run(models, "sales", dimensions=["category", "calendar_quarter"],
                    measures=["revenue"], limit=1000)
    rolled: dict = {}
    for row in with_join["rows"]:
        rolled[row["category"]] = rolled.get(row["category"], 0) + row["revenue"]
    for row in without["rows"]:
        assert rolled[row["category"]] == pytest.approx(row["revenue"])


def test_interval_import_values_read_off_the_calendar_itself(models):
    values = engine.dimension_values(models["subscriptions"], "calendar_quarter", limit=5)
    assert values == sorted(values)
    assert values[0] == "2024-Q1"


def test_interval_import_open_ended_rows_stay_active(models):
    """A null end date means still open — those rows must reach the last day of
    the calendar, exactly as the spine treats them."""
    last = run(models, "subscriptions", dimensions=["calendar_date"], measures=["active_customers"],
               filters=[{"field": "calendar_date", "op": "gte", "value": "2026-06-29"}], limit=10)
    assert last["row_count"] >= 1
    assert all(row["active_customers"] > 1000 for row in last["rows"])


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


def test_dimension_values_column_missing_from_source_is_a_query_error(models):
    """A dimension's declared column can drift from the real file underneath
    it (e.g. the source path got repointed at a differently-shaped extract).
    That used to surface as a raw, uncaught polars.exceptions.ColumnNotFoundError
    — a 500 with a Python traceback dumped to the server console instead of the
    same clean QueryError every other bad-column path (_as_date, _filter_expr)
    already raises."""
    import dataclasses

    model = models["sales"]
    bad_dim = dataclasses.replace(model.dimension("channel"), column="does_not_exist")
    broken = dataclasses.replace(model, dimensions={**model.dimensions, "channel": bad_dim})
    with pytest.raises(engine.QueryError, match="not found in source"):
        engine.dimension_values(broken, "channel")


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


def test_scan_builds_one_relation_with_its_joins(models):
    """scan() is SQL now — one subquery carrying the model's own source, its
    joins and the bundles a query reads from."""
    sql = engine.scan(models["sales"])
    assert sql.startswith("(SELECT ") and sql.endswith(")")
    assert sql.count(";") == 0
    assert "JOIN" in sql


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
    pq.write_table(pa.table({"id": pa.array([1, 2, 3]), "region": pa.array(["A", "B", "Z"]),
                             "amount": pa.array([10, 20, 30])}), buf)
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
measures: [{{name: total, expr: SUM(amount)}}]
dimension_imports:
  - {{bundle: test_geo, anchor_dataset: regions, on: region, how: {how}}}
""")
        semantic.resolve_model(model, {"test_geo": bundle})
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


# --- Regression: a "matching columns" (equality) import whose right_on key is
# itself a declared dimension, under a *different* name than left_on (e.g. a
# custom calendar bundle's own `date` column imported against a fact's
# `event_date`). Polars' default join `coalesce` behavior merges differently-
# named key columns into the left one and drops the right one entirely — which
# silently deleted the calendar's own `date` dimension after the join, even
# though it's declared and importable. An interval (`how: between`) import
# never hit this, since it joins via join_where rather than left_on/right_on.

@pytest.fixture(scope="module")
def renamed_key_import(seeded):
    client = s3.client()
    buf = io.BytesIO()
    pq.write_table(pa.table({
        "study": pa.array(["s1", "s1"]),
        "event_date": pa.array([date(2026, 1, 5), date(2026, 2, 10)], pa.date32()),
        "event_count": pa.array([3, 7]),
    }), buf)
    client.put_object(Bucket=config.BUCKET, Key="test/renamed_key_fact.parquet", Body=buf.getvalue())

    cal = io.BytesIO()
    pq.write_table(_calendar(date(2026, 1, 1), date(2026, 2, 28)), cal)
    client.put_object(Bucket=config.BUCKET, Key="test/renamed_key_cal.parquet", Body=cal.getvalue())

    bundle = semantic.parse_bundle_text(f"""
name: renamed_key_cal
datasets:
  - name: days
    source: {{format: parquet, path: s3://{config.BUCKET}/test/renamed_key_cal.parquet}}
    dimensions:
      - {{name: date, label: Date, type: time}}
      - {{name: year, label: Year, type: numeric}}
""")
    model = semantic.parse_model_text(f"""
name: renamed_key_fact
source: {{format: parquet, path: s3://{config.BUCKET}/test/renamed_key_fact.parquet}}
dimension_imports:
  - {{bundle: renamed_key_cal, anchor_dataset: days, left_on: event_date, right_on: date, how: left}}
dimensions:
  - {{name: study, label: Study}}
measures:
  - {{name: n, expr: SUM(event_count)}}
""")
    return semantic.resolve_model(model, {"renamed_key_cal": bundle})


def test_renamed_equality_import_key_stays_queryable(renamed_key_import):
    assert "date" in duck.relation_schema(engine.scan(renamed_key_import, {"date": None}))
    r = engine.run_query(renamed_key_import, {
        "dimensions": [{"name": "date", "grain": "1mo"}], "measures": ["n"],
    })
    assert {row["date"][:7]: row["n"] for row in r["rows"]} == {"2026-01": 3, "2026-02": 7}


def test_renamed_equality_import_non_key_column_still_worked_before(renamed_key_import):
    # sanity check: `year` was never the collision — it survived even before
    # the fix, unlike `date` (the join's own right_on key)
    r = engine.run_query(renamed_key_import, {"dimensions": ["year"], "measures": ["n"]})
    assert {row["year"]: row["n"] for row in r["rows"]} == {2026: 10}


# --- from: measures: expr aggregates a relation the measure declares -------
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
    pq.write_table(pa.Table.from_pylist(rows), buf)
    s3.client().put_object(Bucket=config.BUCKET, Key="test/framed_events.parquet",
                           Body=buf.getvalue())

    return semantic.parse_model_text(f"""
name: test_framed
source: {{format: parquet, path: s3://{config.BUCKET}/test/framed_events.parquet}}
dimensions:
  - name: cohort
  - name: event_date
    type: time
measures:
  - name: events
    expr: COUNT(*)
  - name: median_days_to_75
    expr: MEDIAN(days_to_75)
    from: |
      WITH ranked AS (
        SELECT {{dims}}, study_id, event_date,
               ROW_NUMBER() OVER (PARTITION BY study_id, {{dims}} ORDER BY event_date)
                 / COUNT(*) OVER (PARTITION BY study_id, {{dims}}) AS cume,
               MIN(event_date) OVER (PARTITION BY study_id, {{dims}}) AS first_event
        FROM {{model}}
      )
      SELECT {{dims}}, study_id,
             date_diff('day', MIN(first_event), MIN(event_date)) AS days_to_75,
             MIN(event_date) AS event_date
      FROM ranked
      WHERE cume >= 0.75
      GROUP BY {{dims}}, study_id
    emits: [event_date]
  - name: bad_from_drops_dims
    expr: COUNT(*)
    from: 'SELECT study_id FROM {{model}} GROUP BY study_id'
  - name: bad_emits_declared_not_output
    expr: COUNT(*)
    from: 'SELECT {{dims}}, study_id FROM {{model}} GROUP BY {{dims}}, study_id'
    emits: [event_date]
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


def test_from_block_that_drops_dimensions_rejected(framed_model):
    with pytest.raises(engine.QueryError, match="lost dimension"):
        engine.run_query(framed_model, {"dimensions": ["cohort"], "measures": ["bad_from_drops_dims"]})


def test_model_emitted_dimension_missing_from_frame_rejected(framed_model):
    with pytest.raises(engine.QueryError, match="emits:"):
        engine.run_query(framed_model, {
            "dimensions": [{"name": "event_date", "grain": "1mo"}],
            "measures": ["bad_emits_declared_not_output"],
        })


def test_inline_frame_measure_rejected(framed_model):
    # frame-based measures are a model-measure-only, authenticated-path
    # construct it replaced was python behind an eval, and a query body
    # carrying one has to be told so rather than quietly ignored.
    with pytest.raises(engine.QueryError, match="'frame:'"):
        engine.run_query(framed_model, {
            "dimensions": ["cohort"], "measures": ["n_studies"],
            "inline_measures": [{
                "name": "n_studies",
                "frame": 'lf.group_by(["study_id", *dims]).agg(pl.len())',
                "expr": "pl.len()",
            }],
        })


def test_inline_frame_emits_rejected_even_without_frame(framed_model):
    with pytest.raises(engine.QueryError, match="'frame:'"):
        engine.run_query(framed_model, {
            "dimensions": ["cohort"], "measures": ["bad"],
            "inline_measures": [{
                "name": "bad",
                "frame_emits": ["event_date"],
                "expr": "COUNT(*)",
            }],
        })


def test_an_inline_measure_may_declare_its_own_from_relation(framed_model):
    """The construct is allowlisted SQL that cannot name a table function, so
    it no longer needs a privilege of its own — the measure lab can prototype
    a complex metric without a model save."""
    r = engine.run_query(framed_model, {
        "dimensions": ["cohort"], "measures": ["studies"],
        "inline_measures": [{
            "name": "studies",
            "expr": "COUNT(*)",
            "from": "SELECT {dims}, study_id FROM {model} GROUP BY {dims}, study_id",
        }],
    })
    assert {row["cohort"]: row["studies"] for row in r["rows"]} == {"X": 2, "Y": 1}


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




def test_shipped_framed_measure_end_to_end(models):
    # the shipped demo measure: median tenure (in days) of ended
    # subscriptions, bucketed by churn month
    r = run(models, "subscriptions", dimensions=[], measures=["median_tenure_days"])
    assert r["row_count"] == 1
    v = r["rows"][0]["median_tenure_days"]
    assert v is not None and 0 < v < 2000


def test_shipped_framed_measure_grouped_with_plain(models):
    r = run(models, "subscriptions", dimensions=["plan"],
            measures=["active_customers", "median_tenure_days"])
    assert r["row_count"] == 3  # street, corpo, netrunner
    with_customers = [row for row in r["rows"] if row["active_customers"] > 0]
    assert with_customers and all(
        row["median_tenure_days"] > 0 for row in with_customers)


def test_shipped_framed_measure_on_timeline(models):
    # bucketed by each churn month: only as many rows as there are distinct
    # churn months in the demo window, every bucketed median positive
    r = run(models, "subscriptions",
            dimensions=[{"name": "churn_month", "grain": "1q"}],
            measures=["median_tenure_days"])
    assert r["row_count"] >= 1
    assert all(row["median_tenure_days"] > 0 for row in r["rows"])


# --- Window measures: running_total() / lag() over a query-time partition --

def _quarterly(models, extra_measures=(), extra_inline=None):
    return run(
        models, "sales",
        dimensions=[{"name": "order_date", "grain": "1q"}],
        measures=["revenue", *extra_measures],
        inline_measures=extra_inline or [],
    )


def test_running_total_inline_matches_cumulative_sum(models):
    inline = [{"name": "revenue_running_total", "expr": "SUM(revenue) OVER w"}]
    r = _quarterly(models, extra_measures=["revenue_running_total"], extra_inline=inline)
    rows = sorted(r["rows"], key=lambda row: row["order_date"])
    running = 0.0
    for row in rows:
        running += row["revenue"]
        assert row["revenue_running_total"] == pytest.approx(running)


def test_running_total_partitions_by_other_query_dimensions(models):
    inline = [{"name": "revenue_running_total", "expr": "SUM(revenue) OVER w"}]
    r = run(
        models, "sales",
        dimensions=["channel", {"name": "order_date", "grain": "1q"}],
        measures=["revenue", "revenue_running_total"],
        inline_measures=inline,
    )
    by_channel: dict = {}
    for row in sorted(r["rows"], key=lambda row: (row["channel"], row["order_date"])):
        running = by_channel.setdefault(row["channel"], 0.0) + row["revenue"]
        assert row["revenue_running_total"] == pytest.approx(running)
        by_channel[row["channel"]] = running
    assert len(by_channel) > 1  # actually exercised more than one partition


def test_pct_change_from_previous_quarter(models):
    text = "(revenue - LAG(revenue) OVER w) / LAG(revenue) OVER w"
    inline = [{"name": "revenue_qoq", "expr": text}]
    r = _quarterly(models, extra_measures=["revenue_qoq"], extra_inline=inline)
    rows = sorted(r["rows"], key=lambda row: row["order_date"])
    assert rows[0]["revenue_qoq"] is None  # no prior quarter to compare to
    for prev, cur in zip(rows, rows[1:]):
        expected = (cur["revenue"] - prev["revenue"]) / prev["revenue"]
        assert cur["revenue_qoq"] == pytest.approx(expected)


def test_window_measure_dependency_dropped_when_not_requested(models):
    # requesting only the running total shouldn't force `revenue` into the
    # result — it's still computed internally (the running total needs it)
    # but trimmed from the response unless also explicitly requested
    inline = [{"name": "revenue_running_total", "expr": "SUM(revenue) OVER w"}]
    r = run(
        models, "sales",
        dimensions=[{"name": "order_date", "grain": "1q"}],
        measures=["revenue_running_total"],
        inline_measures=inline,
    )
    assert "revenue" not in r["rows"][0]
    assert "revenue_running_total" in r["rows"][0]
    assert all(c["name"] != "revenue" for c in r["columns"])


def test_window_measure_requires_a_time_dimension(models):
    inline = [{"name": "revenue_running_total", "expr": "SUM(revenue) OVER w"}]
    with pytest.raises(engine.QueryError, match="time dimension"):
        run(models, "sales", dimensions=["channel"],
            measures=["revenue", "revenue_running_total"], inline_measures=inline)


def test_window_measure_rejects_ambiguous_multiple_time_dimensions(models):
    inline = [{"name": "mrr_running_total", "expr": "SUM(mrr) OVER w"}]
    with pytest.raises(engine.QueryError, match="one time dimension"):
        run(models, "subscriptions",
            dimensions=[{"name": "active_at", "grain": "1mo"}, {"name": "start_month", "grain": "1mo"}],
            measures=["mrr", "mrr_running_total"], inline_measures=inline)


def test_window_measure_cannot_depend_on_another_window_measure(models):
    inline = [
        {"name": "revenue_running_total", "expr": "SUM(revenue) OVER w"},
        {"name": "double_running_total", "expr": "SUM(revenue_running_total) OVER w"},
    ]
    with pytest.raises(engine.QueryError, match="window measure"):
        run(models, "sales", dimensions=[{"name": "order_date", "grain": "1q"}],
            measures=["revenue_running_total", "double_running_total"], inline_measures=inline)


def test_window_measure_unknown_dependency_rejected(models):
    inline = [{"name": "bogus_running_total", "expr": "SUM(does_not_exist) OVER w"}]
    with pytest.raises(engine.QueryError):
        run(models, "sales", dimensions=[{"name": "order_date", "grain": "1q"}],
            measures=["bogus_running_total"], inline_measures=inline)


def test_shipped_model_window_measures_end_to_end(models):
    """revenue_running_total / revenue_pct_change ship on the sales model
    itself (not just as inline demos) — exercise them as real model measures."""
    r = _quarterly(models, extra_measures=["revenue_running_total", "revenue_pct_change"])
    rows = sorted(r["rows"], key=lambda row: row["order_date"])
    running = 0.0
    for prev, cur in zip([None, *rows], rows):
        running += cur["revenue"]
        assert cur["revenue_running_total"] == pytest.approx(running)
        if prev is None:
            assert cur["revenue_pct_change"] is None
        else:
            expected = (cur["revenue"] - prev["revenue"]) / prev["revenue"]
            assert cur["revenue_pct_change"] == pytest.approx(expected)


# --- Visual parameters: param() reference in lag(), resolved per query -----

def _period_list_query(models, parameter_values=None):
    inline = [{"name": "revenue_lag", "expr": "LAG(revenue, param('period_list')) OVER w"}]
    return run(
        models, "sales",
        dimensions=[{"name": "order_date", "grain": "1q"}],
        measures=["revenue", "revenue_lag"],
        inline_measures=inline,
        parameters=[{"name": "period_list", "values": [1, 2, 3, 4], "default": 1}],
        parameter_values=parameter_values or {},
    )


def test_parameter_uses_declared_default_when_no_override(models):
    default_r = _period_list_query(models)
    literal = [{"name": "revenue_lag", "expr": "LAG(revenue, 1) OVER w"}]
    literal_r = run(
        models, "sales", dimensions=[{"name": "order_date", "grain": "1q"}],
        measures=["revenue", "revenue_lag"], inline_measures=literal,
    )
    rows_default = sorted(default_r["rows"], key=lambda row: row["order_date"])
    rows_literal = sorted(literal_r["rows"], key=lambda row: row["order_date"])
    assert [r["revenue_lag"] for r in rows_default] == [r["revenue_lag"] for r in rows_literal]


def test_parameter_override_changes_result(models):
    r1 = _period_list_query(models, {"period_list": 1})
    r2 = _period_list_query(models, {"period_list": 2})
    lag1 = [row["revenue_lag"] for row in sorted(r1["rows"], key=lambda row: row["order_date"])]
    lag2 = [row["revenue_lag"] for row in sorted(r2["rows"], key=lambda row: row["order_date"])]
    assert lag1 != lag2


def test_parameter_value_outside_declared_list_rejected(models):
    with pytest.raises(engine.QueryError, match="not a declared value"):
        _period_list_query(models, {"period_list": 99})


def test_parameter_value_outside_declared_list_never_scans(models, monkeypatch):
    # validation must reject before any scan work happens — no partial run
    called = []
    real_scan = engine.scan
    monkeypatch.setattr(engine, "scan", lambda model: called.append(1) or real_scan(model))
    with pytest.raises(engine.QueryError):
        _period_list_query(models, {"period_list": 99})
    assert called == []


def test_parameter_undeclared_name_rejected(models):
    with pytest.raises(engine.QueryError, match="unknown parameter"):
        _period_list_query(models, {"nope": 1})


def test_parameter_default_not_in_values_rejected(models):
    inline = [{"name": "revenue_lag", "expr": "LAG(revenue, param('period_list')) OVER w"}]
    with pytest.raises(engine.QueryError, match="not one of its declared values"):
        run(
            models, "sales", dimensions=[{"name": "order_date", "grain": "1q"}],
            measures=["revenue", "revenue_lag"], inline_measures=inline,
            parameters=[{"name": "period_list", "values": [1, 2, 3], "default": 9}],
        )


def test_resolve_parameter_values_helper_directly():
    resolved = engine.resolve_parameter_values(
        [{"name": "period_list", "values": [1, 2, 3, 4], "default": 1}], {"period_list": 3},
    )
    assert resolved == {"period_list": 3}
    with pytest.raises(engine.QueryError):
        engine.resolve_parameter_values(
            [{"name": "period_list", "values": [1, 2, 3, 4], "default": 1}], {"period_list": 99},
        )


# --- Parameter types (specs/010-parameter-type-generalization) -------------

def test_param_type_ok_float_accepts_json_integer_shape():
    # JSON/JS have one numeric type — a float parameter's values routinely
    # arrive as JSON integers from a well-behaved frontend (research.md §5)
    assert engine.param_type_ok(100, "float") is True
    assert engine.param_type_ok(100.5, "float") is True
    assert engine.param_type_ok(True, "float") is False  # bool is never numeric here


def test_param_type_ok_int_rejects_float_shape():
    assert engine.param_type_ok(5, "int") is True
    assert engine.param_type_ok(5.0, "int") is False
    assert engine.param_type_ok(True, "int") is False


def test_param_type_ok_string():
    assert engine.param_type_ok("east", "string") is True
    assert engine.param_type_ok(1, "string") is False


def test_coerce_param_value_float_always_returns_genuine_float():
    coerced = engine.coerce_param_value(100, "float")
    assert coerced == 100.0
    assert isinstance(coerced, float)
    assert engine.coerce_param_value("east", "string") == "east"
    assert engine.coerce_param_value(5, "int") == 5


def test_resolve_parameter_values_float_type_accepts_json_int_and_coerces():
    resolved = engine.resolve_parameter_values(
        [{"name": "threshold", "type": "float", "values": [10, 25.5, 100], "default": 25.5}],
        {"threshold": 10},
    )
    assert resolved == {"threshold": 10.0}
    assert isinstance(resolved["threshold"], float)


def test_resolve_parameter_values_int_type_rejects_float_value():
    with pytest.raises(engine.QueryError, match="does not match declared type"):
        engine.resolve_parameter_values(
            [{"name": "x", "type": "int", "values": [1, 2.5, 3], "default": 1}], {},
        )


def test_resolve_parameter_values_string_type_round_trips():
    resolved = engine.resolve_parameter_values(
        [{"name": "region", "type": "string", "values": ["east", "west"], "default": "east"}],
        {"region": "west"},
    )
    assert resolved == {"region": "west"}


def test_resolve_parameter_values_string_type_rejects_numeric_default():
    with pytest.raises(engine.QueryError, match="not one of its declared values"):
        engine.resolve_parameter_values(
            [{"name": "region", "type": "string", "values": ["east", "west"], "default": 1}], {},
        )


def test_resolve_parameter_values_absent_type_behaves_as_int():
    resolved_implicit = engine.resolve_parameter_values(
        [{"name": "p", "values": [1, 2, 3], "default": 1}], {"p": 2},
    )
    resolved_explicit = engine.resolve_parameter_values(
        [{"name": "p", "type": "int", "values": [1, 2, 3], "default": 1}], {"p": 2},
    )
    assert resolved_implicit == resolved_explicit == {"p": 2}
    with pytest.raises(engine.QueryError, match="does not match declared type"):
        engine.resolve_parameter_values([{"name": "p", "values": [1, 2.5], "default": 1}], {})


def test_resolve_parameter_values_unsupported_type_rejected():
    with pytest.raises(engine.QueryError, match="unsupported type"):
        engine.resolve_parameter_values(
            [{"name": "p", "type": "date", "values": ["2026-01-01"], "default": "2026-01-01"}], {},
        )


def test_query_with_float_param_in_comparison(models):
    # aggregate-mode measure: bare identifiers are raw source columns, so
    # this exercises param() inside where()'s predicate against a real
    # column comparison, not a sibling-measure (window-mode) reference
    inline = [{"name": "flagged_units", "expr": "SUM(quantity) FILTER (WHERE unit_price > param('threshold'))"}]
    r = run(
        models, "sales", dimensions=[], measures=["flagged_units"], inline_measures=inline,
        parameters=[{"name": "threshold", "type": "float", "values": [10, 50.5, 100], "default": 50.5}],
        parameter_values={"threshold": 10},
    )
    assert r["rows"]


def test_query_with_string_param_in_comparison(models):
    inline = [{"name": "flagged_units", "expr": "SUM(quantity) FILTER (WHERE channel == param('target_channel'))"}]
    r = run(
        models, "sales", dimensions=[], measures=["flagged_units"], inline_measures=inline,
        parameters=[{"name": "target_channel", "type": "string", "values": ["online", "retail"], "default": "online"}],
        parameter_values={"target_channel": "retail"},
    )
    assert r["rows"]


# --- Schema/bounds caching (app/cache.py) ----------------------------------
# engine.source_schema/scan_schema and the spine-bounds lookup all sit on
# app/cache.py's TTL cache instead of re-resolving straight from S3 every
# time. These count real duck.relation calls through a thin monkeypatch rather
# than asserting on wall-clock speed, so they're deterministic.

def _count_scan_source_calls(monkeypatch):
    calls = []
    real = duck.relation

    def counting(path, fmt):
        calls.append(1)
        return real(path, fmt)

    monkeypatch.setattr(duck, "relation", counting)
    return calls


def test_source_schema_reuses_cached_result_for_same_source(seeded, monkeypatch):
    cache.clear()
    calls = _count_object_store_reads(monkeypatch)
    source = semantic.Source(path="s3://cash-intel/ref/products.csv", format="csv")

    first = engine.source_schema(source)
    engine.source_schema(source)
    engine.source_schema(semantic.Source(path="s3://cash-intel/ref/products.csv", format="csv"))
    assert len(calls) == 1  # same (path, format) every time, even via a fresh Source object
    assert "supplier" in first


def test_source_schema_cache_keyed_on_path_and_format(seeded, monkeypatch):
    cache.clear()
    calls = _count_object_store_reads(monkeypatch)
    engine.source_schema(semantic.Source(path="s3://cash-intel/ref/products.csv", format="csv"))
    engine.source_schema(semantic.Source(path="s3://cash-intel/ref/regions.csv", format="csv"))
    assert len(calls) == 2  # a different path is a genuine cache miss


def test_scan_schema_reuses_cached_result_for_same_model(models, monkeypatch):
    cache.clear()
    calls = _count_scan_source_calls(monkeypatch)
    model = models["sales"]

    first = engine.scan_schema(model)
    after_first = len(calls)
    assert after_first > 0  # sales joins several ref tables — real work happened

    engine.scan_schema(model)
    assert len(calls) == after_first  # identical call added nothing

    uncached = duck.relation_schema(engine.scan(model))
    assert first == uncached  # the cache never changes the actual answer


def test_scan_schema_cache_keyed_on_dimensions_argument(models, monkeypatch):
    cache.clear()
    calls = _count_scan_source_calls(monkeypatch)
    model = models["sales"]

    engine.scan_schema(model, {"region": None})
    after_first = len(calls)
    engine.scan_schema(model, None)  # "everything" is a different plan than one dimension
    assert len(calls) > after_first


def test_scan_schema_does_not_cross_pollute_same_named_models(seeded, monkeypatch):
    """A guided-editor reparse of a model's YAML produces a brand new Model
    object that happens to share an existing model's name. Caching that
    under the bare name would either leak the in-progress edit's schema to
    the real model's readers, or hand the editor back a stale cached answer
    for a source it just changed — see engine._model_cache_key."""
    cache.clear()
    text = (config.MODELS_DIR / "sales.yaml").read_text()
    model_a = semantic.parse_model_text(text)
    model_b = semantic.parse_model_text(text)
    assert model_a is not model_b and model_a.name == model_b.name == "sales"

    calls = _count_scan_source_calls(monkeypatch)
    engine.scan_schema(model_a)
    a_calls = len(calls)
    calls.clear()
    engine.scan_schema(model_b)
    assert len(calls) == a_calls  # model_b did its own real work, not a hand-me-down from model_a


def test_unbounded_spine_query_reuses_cached_bounds_on_repeat(models, monkeypatch):
    """subscriptions' active_at spine, queried with no date filter, falls
    back to the data's own min/max (engine._spine_bounds) — an identical
    repeat query should skip both the schema resolution and that bounds
    collect, leaving only the live per-query scan (never cached, since it
    has to reflect the query's actual result)."""
    cache.clear()
    calls = _count_scan_source_calls(monkeypatch)
    query = {"dimensions": [{"name": "active_at", "grain": "1mo"}], "measures": ["active_customers"]}

    first = run(models, "subscriptions", **query)
    cold = len(calls)
    calls.clear()
    second = run(models, "subscriptions", **query)
    warm = len(calls)

    assert first["rows"] == second["rows"]  # caching must never change the result
    assert 0 < warm < cold


# --- Source read cache (app/duck.py) ---------------------------------------
# The caches above memoize *derived* values — a schema, a bounds pair. These
# cover the layer that decides where a query's bytes come from: a small source
# is pinned as a local DuckDB table and read from memory afterwards, a large
# one keeps streaming with pushdown intact, and neither may change an answer.
#
# Real object-store reads are counted through duck._scan_sql (the uncached
# path) rather than duck.relation, which is still called every time and is
# exactly what is expected to stop reaching S3.

def _assert_same_rows(first: dict, second: dict, label: str = "") -> None:
    """Row-by-row equality with a tolerance on floats.

    Aggregation is parallel and a float sum is not associative, so two
    identical runs of the same query can differ in a value's last bits purely
    from how the reduction happened to be scheduled — under 1e-14 relative.
    That is a property of the engine, not of anything these tests
    change, so asserting bit-equality on floats would make them flaky without
    testing anything real."""
    assert first["row_count"] == second["row_count"], label
    assert first["columns"] == second["columns"], label
    for a, b in zip(first["rows"], second["rows"]):
        assert set(a) == set(b), label
        for column, value in a.items():
            if isinstance(value, float) and isinstance(b[column], float):
                assert value == pytest.approx(b[column], rel=1e-9), (label, column)
            else:
                assert value == b[column], (label, column)


def _count_object_store_reads(monkeypatch):
    reads = []
    real = duck._scan_sql

    def counting(fmt, path):
        reads.append(path)
        return real(fmt, path)

    monkeypatch.setattr(duck, "_scan_sql", counting)
    return reads


def _read(source) -> int:
    """Read a source the way a query does — through duck.relation, which is
    either a pinned local table or the table function that fetches it."""
    return duck.cursor().execute(
        f"SELECT count(*) FROM {duck.relation(source.path, source.format)}").fetchone()[0]


def _sources_of(model):
    return [model.source] + [j.source for j in model.joins]


def test_small_source_is_read_from_the_object_store_once(models, monkeypatch):
    cache.clear()
    reads = _count_object_store_reads(monkeypatch)
    source = semantic.Source(path="s3://cash-intel/ref/products.csv", format="csv")

    first = _read(source)
    _read(source)
    _read(source)

    assert reads == ["s3://cash-intel/ref/products.csv"]  # read once, then served from memory
    assert first == 15


def test_repeat_query_stops_touching_the_object_store(models, monkeypatch):
    """The editing loop: the same visual re-run costs no object-store reads."""
    cache.clear()
    reads = _count_object_store_reads(monkeypatch)
    query = {"dimensions": ["region"], "measures": ["revenue"]}

    first = run(models, "sales", **query)
    cold = len(reads)
    reads.clear()
    second = run(models, "sales", **query)

    _assert_same_rows(first, second)
    assert cold > 0 and reads == []


def test_a_different_query_on_a_cached_model_also_stops_touching_s3(models, monkeypatch):
    """Not just an identical repeat — the cache is on the *source*, so
    re-dimensioning a visual mid-edit reads nothing either."""
    cache.clear()
    run(models, "sales", dimensions=["region"], measures=["revenue"])
    reads = _count_object_store_reads(monkeypatch)

    other = run(models, "sales", dimensions=["category"], measures=["profit", "units"])

    assert other["row_count"] > 0
    assert reads == []


def test_source_cache_never_changes_an_answer(models, monkeypatch):
    """Same queries, cache off then on. Float aggregates are compared with a
    tolerance: a parallel sum reorders its additions when the data is chunked
    differently, which moves the last bits of a float64 and nothing else."""
    queries = [
        ("sales", {"dimensions": ["region"], "measures": ["revenue", "margin_pct"]}),
        ("sales", {"dimensions": ["supplier", "tier"], "measures": ["revenue", "orders"]}),
        ("sales", {"dimensions": [{"name": "order_date", "grain": "1q"}],
                   "measures": ["revenue", "revenue_running_total"]}),
        ("commercial_overview", {"dimensions": [{"name": "calendar_date", "grain": "1mo"}],
                                 "measures": ["revenue", "ad_spend", "mrr"]}),
        ("logistics", {"dimensions": ["courier"], "measures": ["shipments"]}),
    ]
    for name, query in queries:
        monkeypatch.setattr(config, "SOURCE_CACHE_TTL", 0)
        cache.clear()
        off = run(models, name, **query)
        monkeypatch.setattr(config, "SOURCE_CACHE_TTL", 60.0)
        cache.clear()
        on = run(models, name, **query)

        _assert_same_rows(off, on, name)


def test_source_larger_than_the_gate_keeps_streaming(models, monkeypatch):
    """The gate is the object store's own byte total, so an oversized source
    is never downloaded just to find out it was oversized."""
    monkeypatch.setattr(config, "SOURCE_CACHE_MAX_BYTES", 1)   # everything is "too big"
    cache.clear()
    reads = _count_object_store_reads(monkeypatch)
    query = {"dimensions": ["region"], "measures": ["revenue"]}

    first = run(models, "sales", **query)
    reads.clear()
    second = run(models, "sales", **query)

    _assert_same_rows(first, second)
    assert reads  # still going to S3 every time, exactly as before


def test_frame_is_dropped_when_it_expands_past_the_resident_cap(models, monkeypatch):
    """Second guard: compressed columnar data can pass the on-disk gate and
    still be too big to hold once decoded."""
    monkeypatch.setattr(config, "SOURCE_CACHE_MAX_RESIDENT_BYTES", 1)
    cache.clear()
    source = semantic.Source(path="s3://cash-intel/ref/products.csv", format="csv")

    assert duck._pin_source(source.path, source.format) is None


def test_ttl_of_zero_restores_the_uncached_behaviour(models, monkeypatch):
    monkeypatch.setattr(config, "SOURCE_CACHE_TTL", 0)
    cache.clear()
    reads = _count_object_store_reads(monkeypatch)
    source = semantic.Source(path="s3://cash-intel/ref/products.csv", format="csv")

    _read(source)
    _read(source)

    assert len(reads) == 2  # no caching at all


def test_model_reload_drops_held_frames(models, monkeypatch):
    """registry.reload_all() clears the cache outright — a model edit can
    change what a path resolves to, and stale rows must not outlive it."""
    cache.clear()
    source = semantic.Source(path="s3://cash-intel/ref/products.csv", format="csv")
    _read(source)

    reads = _count_object_store_reads(monkeypatch)
    cache.clear()                      # what reload_all() does
    _read(source)

    assert reads == ["s3://cash-intel/ref/products.csv"]


def test_glob_is_listed_once_and_reused(models, monkeypatch):
    """A parquet glob is resolved to a file list once per TTL and handed to
    polars already resolved, instead of being re-listed on every collect."""
    cache.clear()
    lists = []
    real = duck._list_objects
    monkeypatch.setattr(duck, "_list_objects",
                        lambda path: lists.append(path) or real(path))
    source = semantic.Source(path="s3://cash-intel/sales/*.parquet", format="parquet")

    paths, total = duck.objects(source.path)
    duck.objects(source.path)
    duck.objects(source.path)

    assert lists == ["s3://cash-intel/sales/*.parquet"]
    assert [p.rsplit("/", 1)[1] for p in paths] == ["2024.parquet", "2025.parquet", "2026.parquet"]
    assert total > 0


def test_listing_matches_the_glob_and_not_its_neighbours(seeded):
    csvs, _ = duck._list_objects("s3://cash-intel/ref/*.csv")
    assert all(p.endswith(".csv") for p in csvs)
    assert "s3://cash-intel/ref/products.csv" in csvs
    assert not any("sales/" in p for p in csvs)

    exact, size = duck._list_objects("s3://cash-intel/ref/products.csv")
    assert exact == ["s3://cash-intel/ref/products.csv"] and size > 0


def test_a_non_s3_path_is_left_alone(tmp_path, monkeypatch):
    """Nothing to list, so nothing to cache — the source just gets scanned,
    which is what a local path in a test or a future backend needs."""
    local = tmp_path / "rows.csv"
    local.write_text("a,b\n1,2\n3,4\n")
    cache.clear()
    source = semantic.Source(path=str(local), format="csv")

    assert duck._list_objects(str(local)) is None
    assert duck._pin_source(source.path, source.format) is None
    assert _read(source) == 2


def test_glob_does_not_reach_into_a_nested_prefix(seeded):
    """A `*` must not cross a `/`. The listing decides which files a query
    reads, so a pattern that swallowed a nested archive directory would put its
    rows in someone's totals. Asserted against DuckDB's own glob rather than
    against the expectation, so the two can't drift apart."""
    client = s3.client()
    buf = io.BytesIO()
    pq.write_table(pa.table({
        "order_id": pa.array([1]), "region": pa.array(["Nested"]),
        "unit_price": pa.array([1.0]), "quantity": pa.array([1]),
        "unit_cost": pa.array([0.5]),
    }), buf)
    client.put_object(Bucket=config.BUCKET, Key="sales/archive/old.parquet",
                      Body=buf.getvalue())
    try:
        cache.clear()
        listed, _ = duck._list_objects("s3://cash-intel/sales/*.parquet")
        assert "s3://cash-intel/sales/archive/old.parquet" not in listed

        # and the file list handed to read_parquet must select exactly what
        # DuckDB's own glob would have selected
        cursor = duck.cursor()
        via_glob = cursor.execute(
            "SELECT count(*) AS n, count(*) FILTER (WHERE region = 'Nested') AS nested "
            "FROM read_parquet('s3://cash-intel/sales/*.parquet')").fetchone()
        via_listing = cursor.execute(
            f"SELECT count(*) FROM {duck._scan_sql('parquet', 's3://cash-intel/sales/*.parquet')}"
        ).fetchone()[0]
        assert via_listing == via_glob[0]
        assert via_glob[1] == 0
    finally:
        client.delete_object(Bucket=config.BUCKET, Key="sales/archive/old.parquet")

def test_glob_match_segment_semantics():
    assert duck.glob_match("sales/*.parquet", "sales/2024.parquet")
    assert not duck.glob_match("sales/*.parquet", "sales/archive/old.parquet")
    assert not duck.glob_match("sales/*.parquet", "sales/2024.csv")
    assert not duck.glob_match("sales/*.parquet", "other/2024.parquet")
    assert duck.glob_match("sales/**/*.parquet", "sales/archive/old.parquet")
    assert duck.glob_match("ref/products.csv", "ref/products.csv")
    assert not duck.glob_match("ref/Products.csv", "ref/products.csv")  # S3 is case-sensitive


def test_denied_listing_falls_back_to_streaming(models, monkeypatch):
    """A bucket may allow GetObject and deny ListBucket — a shape this app
    supports (app/seed.py tolerates a denied CreateBucket for the same
    reason). Without a listing there is no byte total to gate on and no file
    list to pin, so the source cache simply switches itself off for that
    source rather than failing the query."""
    from botocore.exceptions import ClientError

    denied = ClientError({"Error": {"Code": "AccessDenied",
                                    "Message": "not authorized to perform s3:ListBucket"}},
                         "ListObjectsV2")

    class DeniedClient:
        def get_paginator(self, _name):
            raise denied

    monkeypatch.setattr(s3, "client", lambda: DeniedClient())
    cache.clear()

    source = semantic.Source(path="s3://cash-intel/sales/*.parquet", format="parquet")
    assert duck._list_objects(source.path) is None
    assert duck._pin_source(source.path, source.format) is None

    # and a real query still answers, straight from the object store
    result = run(models, "sales", dimensions=["region"], measures=["revenue"])
    assert result["row_count"] == 5
