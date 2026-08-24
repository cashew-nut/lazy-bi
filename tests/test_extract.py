"""Instant cross-filter extracts (specs/016-instant-cross-filter/).

Two halves: the parse-only roll-up decomposition in app/sqlgrammar.py, and the
POST /api/query/extract contract on top of it. The property that matters
throughout is the one the browser depends on — an extract rolled back up to a
tile's own grain has to equal what /query would have returned for that tile,
not merely look plausible.
"""
import base64
import io
import json

import pyarrow as pa
import pyarrow.ipc as pa_ipc
import pytest

from app import config, sqlgrammar


# ── roll-up decomposition ───────────────────────────────────────────────

@pytest.mark.parametrize("text,agg,emitted", [
    ("SUM(revenue)", "sum", "sum(revenue)"),
    ("COUNT(*)", "sum", "count_star()"),
    ("COUNT(order_id)", "sum", "count(order_id)"),
    ("MIN(fare_amount)", "min", "min(fare_amount)"),
    ("MAX(fare_amount)", "max", "max(fare_amount)"),
])
def test_single_aggregate_decomposes_to_itself(text, agg, emitted):
    plan = sqlgrammar.rollup_plan(text)
    assert plan["formula"] == {"ref": 0}
    assert plan["components"] == [{"agg": agg, "expr": emitted}]


def test_mean_decomposes_into_sum_over_count():
    """The whole reason decomposition exists: averaging per-bucket averages is
    wrong, but summing the two additive halves and dividing once is exact."""
    plan = sqlgrammar.rollup_plan("AVG(fare_amount)")
    assert plan["components"] == [
        {"agg": "sum", "expr": "SUM(fare_amount)"},
        {"agg": "sum", "expr": "COUNT(fare_amount)"},
    ]
    assert plan["formula"] == {"op": "/", "l": {"ref": 0}, "r": {"ref": 1}}


def test_ratio_of_sums_decomposes():
    plan = sqlgrammar.rollup_plan("SUM(tip_amount) / SUM(fare_amount)")
    assert [c["expr"] for c in plan["components"]] == ["SUM(tip_amount)", "SUM(fare_amount)"]
    assert plan["formula"] == {"op": "/", "l": {"ref": 0}, "r": {"ref": 1}}


def test_constants_stay_in_the_formula_not_the_components():
    plan = sqlgrammar.rollup_plan("SUM(spend) / (SUM(impressions) / 1000)")
    assert [c["expr"] for c in plan["components"]] == ["SUM(spend)", "SUM(impressions)"]
    assert plan["formula"]["r"] == {"op": "/", "l": {"ref": 1}, "r": {"const": 1000}}


def test_decimal_constants_keep_their_scale():
    """DuckDB serializes a DECIMAL literal as its unscaled integer plus a
    scale, so reading the value alone turns 1.5 into 15 — and the browser
    would then re-aggregate ten times too large while the live path stayed
    right."""
    plan = sqlgrammar.rollup_plan("SUM(x) * 1.5")
    assert plan["formula"]["r"] == {"const": 1.5}


def test_repeated_component_is_fetched_once():
    plan = sqlgrammar.rollup_plan("SUM(a) / SUM(a)")
    assert len(plan["components"]) == 1
    assert plan["formula"] == {"op": "/", "l": {"ref": 0}, "r": {"ref": 0}}


def test_filtered_aggregate_carries_its_filter_into_the_component():
    plan = sqlgrammar.rollup_plan("SUM(revenue) FILTER (WHERE region = 'EU')")
    assert plan["components"] == [
        {"agg": "sum", "expr": "SUM(revenue) FILTER (WHERE (region = 'EU'))"}]


@pytest.mark.parametrize("text", [
    "COUNT(DISTINCT order_id)",              # no combining function exists
    "MEDIAN(tenure_days)",
    "STDDEV(x)", "VARIANCE(x)", "FIRST(x)", "LAST(x)",
    "QUANTILE_CONT(x, 0.95)",
    "SUM(revenue) OVER w",                   # reads neighbouring rows
    "(revenue - LAG(revenue) OVER w) / LAG(revenue) OVER w",
    "SUM(monthly_fee) / COUNT(DISTINCT customer_id)",   # one bad half poisons it
    "param('k') * SUM(a)",                   # needs a value the browser lacks
    "revenue",                               # bare sibling reference
    "not valid syntax (",
])
def test_undecomposable_measures_return_none(text):
    assert sqlgrammar.rollup_plan(text) is None


def test_decomposition_never_evaluates_the_text(monkeypatch):
    """Same posture as the rest of the grammar module: parse only. The AST
    comes back from json_serialize_sql, which plans nothing and binds
    nothing."""
    for name in ("eval", "exec", "compile"):
        monkeypatch.setattr(
            sqlgrammar, name,
            lambda *a, **k: pytest.fail("rollup_plan must never eval measure text"),
            raising=False,
        )
    assert sqlgrammar.rollup_plan("SUM(revenue) / COUNT(*)") is not None


# ── the extract endpoint ────────────────────────────────────────────────

def _meta(res):
    return json.loads(base64.b64decode(res.headers["X-Extract-Meta"]))


class _Frame:
    """The bits of a frame these tests use, over a pyarrow Table."""

    def __init__(self, table):
        self.table = table
        self.height = table.num_rows
        self.columns = table.column_names
        self.schema = {f.name: f.type for f in table.schema}

    def __getitem__(self, name):
        return self.table.column(name).to_pylist()

    def to_dicts(self):
        return self.table.to_pylist()

    def sort(self, name):
        order = sorted(range(self.height), key=lambda i: (self[name][i] is None, self[name][i]))
        return _Frame(self.table.take(pa.array(order)))


def _frame(res):
    with pa_ipc.open_stream(io.BytesIO(res.content)) as reader:
        return _Frame(reader.read_all())


SALES = {"model": "sales", "dimensions": ["region"], "measures": ["revenue"]}


def test_extract_returns_an_arrow_ipc_stream(client):
    res = client.post("/api/query/extract", json=SALES)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/vnd.apache.arrow.stream"
    df = _frame(res)
    assert df.height > 0
    meta = _meta(res)
    assert meta["row_count"] == df.height
    assert meta["byte_size"] == len(res.content)


def test_extract_columns_match_the_json_query_contract(client):
    """FR-005: the chart renderers read `columns` from an extract exactly as
    they read it from /query, so the two must agree field for field."""
    live = client.post("/api/query", json=SALES).json()
    meta = _meta(client.post("/api/query/extract", json=SALES))
    assert meta["columns"] == live["columns"]


def test_extract_carries_other_tiles_dimensions(client):
    """FR-006: the union dimension is projected in so a cross-filter from
    another tile lands locally; other tiles' measures are not."""
    res = client.post("/api/query/extract",
                      json={**SALES, "cross_dimensions": ["channel", "not_a_dimension"]})
    df = _frame(res)
    assert {"region", "channel"} <= set(df.columns)
    names = {d["name"]: d for d in _meta(res)["dimensions"]}
    assert names["region"]["display"] is True
    assert names["channel"]["display"] is False   # carried for filtering only


def test_time_dimensions_are_never_unioned_in(client):
    """No renderer emits a cross-filter from a time mark (charts/bar.js and
    friends guard on type !== "time"), so another tile's dates can never be
    the value that arrives — and carrying them, ungrained, is the single most
    expensive thing an extract could do."""
    lean = client.post("/api/query/extract",
                       json={**SALES, "cross_dimensions": ["channel", "category"]})
    fat = client.post("/api/query/extract",
                      json={**SALES, "cross_dimensions": ["channel", "category", "order_date"]})
    assert "order_date" not in _frame(fat).columns
    assert _meta(lean)["row_count"] == _meta(fat)["row_count"]


def test_a_tiles_own_time_dimension_is_still_carried(client):
    """The exclusion above is about *other* tiles' dates. A tile groups by its
    own, so those stay — with their coarser buckets intact."""
    res = client.post("/api/query/extract", json={
        "model": "sales", "dimensions": [{"name": "order_date", "grain": "1mo"}],
        "measures": ["revenue"], "cross_dimensions": ["channel"]})
    dims = {d["name"]: d for d in _meta(res)["dimensions"]}
    assert dims["order_date"]["display"] is True
    assert set(dims["order_date"]["coarser"]) == {"1q", "1y"}


# ── hoisting the dashboard's own filters ────────────────────────────────

def test_an_interactive_filter_is_carried_not_pushed_down(client):
    """The point of hoisting: the extract holds every value of the filtered
    dimension, so changing the filter is a re-slice rather than a re-fetch."""
    body = {"model": "sales", "dimensions": ["channel"], "measures": ["revenue"],
            "filters": [{"field": "region", "op": "in", "values": ["Night City"]}],
            "interactive_filters": ["region"]}
    res = client.post("/api/query/extract", json=body)
    meta, df = _meta(res), _frame(res)
    assert meta["local_filters"] == ["region"]
    assert "region" in df.columns
    assert len(set(df["region"])) > 1, "the filter should not have been applied server-side"


def test_a_filter_field_with_no_value_yet_is_still_carried(client):
    """The state a dashboard sits in before anyone touches its filters. If the
    column only appeared once a value existed, the *first* change would cost a
    re-fetch and only the second would be instant."""
    res = client.post("/api/query/extract", json={
        "model": "sales", "dimensions": ["channel"], "measures": ["revenue"],
        "filters": [], "interactive_filters": ["region"]})
    assert _meta(res)["local_filters"] == ["region"]
    assert "region" in _frame(res).columns


def test_a_filter_left_out_of_interactive_stays_pushed_down(client):
    """A visual's own saved filter never changes, so baking it in is strictly
    better — smaller extract, same answer."""
    body = {"model": "sales", "dimensions": ["channel"], "measures": ["revenue"],
            "filters": [{"field": "region", "op": "in", "values": ["Night City"]}]}
    res = client.post("/api/query/extract", json=body)
    assert _meta(res)["local_filters"] == []
    assert "region" not in _frame(res).columns


def test_time_filters_are_never_hoisted(client):
    """The trap: the engine filters the *raw* date column, while an extract
    holds it truncated to the tile's grain — so `>= 2024-06-15` would keep all
    of June locally instead of half of it."""
    body = {"model": "sales", "dimensions": ["channel"], "measures": ["revenue"],
            "filters": [{"field": "order_date", "op": "gte", "value": "2024-06-15"}],
            "interactive_filters": ["order_date"]}
    assert _meta(client.post("/api/query/extract", json=body))["local_filters"] == []


def test_contains_filters_are_never_hoisted(client):
    """The engine runs `contains` as a case-insensitive regex over the column
    cast to string, which no client-side substring match reproduces."""
    body = {"model": "sales", "dimensions": ["channel"], "measures": ["revenue"],
            "filters": [{"field": "region", "op": "contains", "value": "night"}],
            "interactive_filters": ["region"]}
    assert _meta(client.post("/api/query/extract", json=body))["local_filters"] == []


def test_hoisted_filter_applied_locally_equals_the_live_query(client):
    """The correctness property for hoisting, the same one the roll-up test
    makes for measures: filtering the extract has to reproduce /query exactly."""
    flt = [{"field": "region", "op": "in", "values": ["Night City", "Pacifica"]}]
    live = client.post("/api/query", json={
        "model": "sales", "dimensions": ["channel"], "measures": ["revenue"], "filters": flt}).json()
    res = client.post("/api/query/extract", json={
        "model": "sales", "dimensions": ["channel"], "measures": ["revenue"],
        "filters": flt, "interactive_filters": ["region"]})
    meta, df = _meta(res), _frame(res)
    aggs = {c["col"]: c["agg"] for m in meta["measures"] for c in m["components"]}
    kept = [r for r in df.to_dicts() if r["region"] in ("Night City", "Pacifica")]
    rolled = _rollup(kept, "channel", aggs)
    got = {r["channel"]: _evaluate(meta["measures"][0]["formula"],
                                   [r[c["col"]] for c in meta["measures"][0]["components"]])
           for r in rolled}
    assert got
    for row in live["rows"]:
        assert got[row["channel"]] == pytest.approx(row["revenue"], rel=1e-9)


def test_hoisting_is_given_up_before_the_tile_is(client, monkeypatch):
    """Hoisting multiplies an extract by however selective those filters were,
    which can blow a cap the tile fitted under. That costs the hoisting, not
    the whole feature — the retry pushes the filters back down and the tile
    stays instant."""
    body = {"model": "sales", "dimensions": ["channel"], "measures": ["revenue"],
            "filters": [{"field": "region", "op": "in", "values": ["Night City"]}],
            "interactive_filters": ["region"]}
    hoisted = _meta(client.post("/api/query/extract", json=body))
    assert hoisted["local_filters"] == ["region"]

    # a cap that the hoisted extract misses but the pushed-down one clears
    monkeypatch.setattr(config, "EXTRACT_MAX_ROWS", hoisted["row_count"] - 1)
    res = client.post("/api/query/extract", json=body)
    assert res.headers["content-type"] == "application/vnd.apache.arrow.stream", \
        "the tile should have kept instant mode by giving up the hoisting"
    assert _meta(res)["local_filters"] == []
    assert "region" not in _frame(res).columns


def test_a_tile_that_cannot_fit_either_way_still_falls_back(client, monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_MAX_ROWS", 1)
    body = {"model": "sales", "dimensions": ["channel"], "measures": ["revenue"],
            "filters": [{"field": "region", "op": "in", "values": ["Night City"]}],
            "interactive_filters": ["region"]}
    assert client.post("/api/query/extract", json=body).json()["fallback"]["cap"] == "rows"


def test_extract_rolled_back_up_equals_the_live_query(client):
    """The core correctness property. Re-aggregating the wider extract down to
    the tile's own dimension must reproduce /query's numbers exactly — this is
    what the browser does after every cross-filter."""
    query = {"model": "sales", "dimensions": ["region"],
             "measures": ["revenue", "avg_unit_price", "margin_pct"]}
    live = client.post("/api/query", json=query).json()
    res = client.post("/api/query/extract",
                      json={**query, "cross_dimensions": ["channel", "category"]})
    assert res.status_code == 200, res.text
    df, meta = _frame(res), _meta(res)

    # the union of every measure's components, deduped — two measures sharing
    # an aggregate share one extract column, so this is what the browser
    # hands Perspective as its aggregate map
    aggs = {c["col"]: c["agg"] for m in meta["measures"] for c in m["components"]}
    assert len(aggs) < SUM(len(m["components"]) for m in meta["measures"]), \
        "revenue and margin_pct share sum(unit_price * quantity) — it should be fetched once"
    rolled = _rollup(df.to_dicts(), "region", aggs)
    values = {}
    for row in rolled:
        values[row["region"]] = {
            m["name"]: _evaluate(m["formula"], [row[c["col"]] for c in m["components"]])
            for m in meta["measures"]
        }
    for row in live["rows"]:
        for measure in ("revenue", "avg_unit_price", "margin_pct"):
            assert values[row["region"]][measure] == pytest.approx(row[measure], rel=1e-9)


def _rollup(rows, key, aggs):
    """The local re-aggregation Perspective does in the browser, in ten lines
    of python: group the extract's rows by `key` and combine each component
    column with its declared aggregate."""
    groups = {}
    for row in rows:
        bucket = groups.setdefault(row[key], {key: row[key]})
        for column, agg in aggs.items():
            value = row[column]
            if value is None:
                continue
            if column not in bucket:
                bucket[column] = value
            elif agg == "sum":
                bucket[column] += value
            elif agg == "min":
                bucket[column] = min(bucket[column], value)
            elif agg == "max":
                bucket[column] = max(bucket[column], value)
    for bucket in groups.values():
        for column in aggs:
            bucket.setdefault(column, None)
    return list(groups.values())


def _evaluate(node, refs):
    """The formula evaluator the browser runs, in eight lines of python."""
    if "const" in node:
        return node["const"]
    if "ref" in node:
        return refs[node["ref"]]
    left, right = _evaluate(node["l"], refs), _evaluate(node["r"], refs)
    if node["op"] == "/":
        return None if not right else left / right
    return {"+": lambda: left + right, "-": lambda: left - right,
            "*": lambda: left * right}[node["op"]]()


def test_time_dimension_arrives_as_the_same_iso_string_json_would_give(client):
    """A cross-filter value clicked on a live tile has to compare equal to one
    sitting in an extract, so both paths must render a date identically."""
    query = {"model": "sales", "dimensions": [{"name": "order_date", "grain": "1mo"}],
             "measures": ["revenue"]}
    live = client.post("/api/query", json=query).json()
    df = _frame(client.post("/api/query/extract", json=query))
    assert pa.types.is_large_string(df.schema["order_date"])
    assert set(df["order_date"]) == {r["order_date"] for r in live["rows"]}


def test_every_dtype_normalizes_to_something_perspective_can_read(client):
    """Perspective's Arrow reader is narrower than Arrow itself — it rejects
    string_view outright, and has no notion of decimal, dictionary or null
    types. Every column has to land on a type it understands, without the
    values drifting from what the JSON path would have produced."""
    import datetime as dt
    import decimal

    from app import extract as extract_mod

    table = pa.table({
        "d": pa.array([dt.date(2024, 1, 15)], pa.date32()),
        "ts": pa.array([dt.datetime(2024, 1, 15, 5, 30, 0, 123456)], pa.timestamp("us")),
        "dec": pa.array([decimal.Decimal("1.50")], pa.decimal128(10, 2)),
        "nul": pa.array([None], pa.null()),
        "cat": pa.array(["a"]).dictionary_encode(),
        "u32": pa.array([7], pa.uint32()),
        "i64": pa.array([3], pa.int64()),
        "f64": pa.array([1.5], pa.float64()),
        "b": pa.array([True]),
        "s": pa.array(["hi"]),
    })
    out = extract_mod._normalize(table)
    schema = {f.name: f.type for f in out.schema}
    assert pa.types.is_large_string(schema["d"]) and pa.types.is_large_string(schema["ts"])
    # the JSON path gives a decimal as a number, and a chart wants one too
    assert pa.types.is_floating(schema["dec"])
    assert pa.types.is_large_string(schema["nul"])
    assert pa.types.is_large_string(schema["cat"])
    assert pa.types.is_integer(schema["u32"]) and not pa.types.is_unsigned_integer(schema["u32"])
    assert pa.types.is_large_string(schema["s"])

    # dates must serialize to the same token the JSON path emits, or a
    # cross-filter value from a live tile won't match one in an extract
    row = out.to_pylist()[0]
    assert row["d"] == "2024-01-15"
    assert row["ts"] == "2024-01-15T05:30:00"
    assert (row["cat"], row["s"], row["b"], row["i64"], row["f64"], row["u32"]) == \
        ("a", "hi", True, 3, 1.5, 7)
    assert row["dec"] == pytest.approx(1.5)

    payload = extract_mod._ipc_stream(out)
    with pa_ipc.open_stream(io.BytesIO(payload)) as reader:
        assert reader.read_all().to_pylist() == out.to_pylist()


def test_strings_are_not_written_as_utf8_view(client):
    """Regression guard for the one incompatibility that actually bit: Arrow's
    newer string layout is written as string_view, which Perspective refuses
    to load ("Could not load arrow column of type `utf8_view`")."""
    import pyarrow.ipc

    res = client.post("/api/query/extract", json=SALES)
    schema = pyarrow.ipc.open_stream(io.BytesIO(res.content)).read_all().schema
    kinds = {str(f.type) for f in schema}
    assert "string_view" not in kinds and "utf8_view" not in kinds, kinds
    assert "large_string" in kinds


def test_coarser_grain_buckets_are_precomputed(client):
    """R3: a coarser session grain is answered locally, from buckets the
    truncated — never from date arithmetic in the browser. Weeks don't nest in
    months, so a month-grained extract offers quarters and years only."""
    res = client.post("/api/query/extract", json={
        "model": "sales", "dimensions": [{"name": "order_date", "grain": "1mo"}],
        "measures": ["revenue"]})
    dim = _meta(res)["dimensions"][0]
    assert set(dim["coarser"]) == {"1q", "1y"}
    df = _frame(res)
    for column in dim["coarser"].values():
        assert column in df.columns
    quarters = df.select(dim["coarser"]["1q"]).to_series()
    assert all(q[5:7] in ("01", "04", "07", "10") for q in quarters)


def test_week_grain_offers_no_coarser_buckets(client):
    res = client.post("/api/query/extract", json={
        "model": "sales", "dimensions": [{"name": "order_date", "grain": "1w"}],
        "measures": ["revenue"]})
    assert _meta(res)["dimensions"][0]["coarser"] == {}


def test_undecomposable_measure_falls_back_with_a_reason(client):
    """FR-009/FR-013: a tile this feature can't serve correctly is declined —
    as a routine 200 answer, not an error status. A dashboard mixing instant
    and live tiles is the normal case (US2), so declining must not put a red
    line in the console on every single load."""
    res = client.post("/api/query/extract", json={
        "model": "sales", "dimensions": ["region"], "measures": ["orders"]})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    fallback = res.json()["fallback"]
    assert "orders" in fallback["reason"]
    assert fallback["cap"] is None      # refused on its merits, not its size


def test_window_measure_falls_back(client):
    res = client.post("/api/query/extract", json={
        "model": "sales", "dimensions": [{"name": "order_date", "grain": "1mo"}],
        "measures": ["revenue_running_total"]})
    assert res.json()["fallback"]["cap"] is None


def test_row_cap_falls_back_and_names_the_cap(client, monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_MAX_ROWS", 2)
    fallback = client.post("/api/query/extract", json=SALES).json()["fallback"]
    assert fallback["cap"] == "rows"
    assert fallback["rows"] > 2


def test_byte_cap_falls_back_and_names_the_cap(client, monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_MAX_BYTES", 16)
    fallback = client.post("/api/query/extract", json=SALES).json()["fallback"]
    assert fallback["cap"] == "bytes"
    assert fallback["bytes"] > 16


def test_extract_is_not_bound_by_the_render_row_cap(client):
    """An extract is a cache, not a render payload, so it is allowed past
    MAX_ROWS — but only up to its own cap, never unbounded."""
    monkey = {"model": "sales", "dimensions": ["region", "category", "channel"],
              "measures": ["revenue"]}
    res = client.post("/api/query/extract", json=monkey)
    assert res.status_code == 200
    assert _meta(res)["row_count"] <= config.EXTRACT_MAX_ROWS


def test_unknown_model_is_still_a_404(client):
    assert client.post("/api/query/extract",
                       json={"model": "nope", "dimensions": [], "measures": ["x"]}).status_code == 404


def test_bad_query_is_still_a_400(client):
    """A request that wouldn't have worked on /query either keeps a real error
    status — only the routine "runs live instead" answer is a 200."""
    res = client.post("/api/query/extract",
                      json={"model": "sales", "dimensions": ["nope"], "measures": ["revenue"]})
    assert res.status_code == 400


def test_plain_query_endpoint_is_unchanged(client):
    """SC-003: /query itself must not move for any existing caller."""
    res = client.post("/api/query", json=SALES)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert set(res.json()) == {"columns", "rows", "row_count", "elapsed_ms"}
