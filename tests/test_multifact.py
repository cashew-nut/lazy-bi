"""Multi-fact models: several unrelated fact tables read on one axis.

The contract under test is that the facts are never joined to each other —
each is queried on its own and the aggregates are merged on the dimensions
they share — so every measure keeps the grain of its own table no matter what
else is on the chart.
"""
from pathlib import Path

import pytest

from app import engine, semantic
from app.semantic import ModelError


def _parse(text: str) -> semantic.Model:
    return semantic.parse_model_text(text)


def _resolved(text: str, others: dict) -> semantic.Model:
    model = _parse(text)
    return semantic.resolve_facts(model, others)


# ── parsing ────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,block", [
    ("dimensions", "dimensions:\n  - name: region"),
    ("measures", "measures:\n  - name: n\n    expr: count()"),
    ("joins", "joins:\n  - name: j\n    source: {format: csv, path: s3://b/j.csv}\n    on: id"),
    ("dimension_imports", "dimension_imports:\n  - bundle: geography\n    anchor_dataset: regions\n    on: region"),
])
def test_a_sourceless_multi_fact_model_cannot_declare_its_own_fields(key, block):
    with pytest.raises(ModelError, match=f"declares '{key}' but no 'source'"):
        _parse(f"name: bad\nfacts:\n  - model: sales\n{block}\n")


def test_a_model_may_declare_a_source_and_facts_together():
    """The other shape: an ordinary fact model that also reads its neighbours.
    It keeps everything a fact model declares."""
    parsed = _parse("""
name: hybrid
source: {format: parquet, path: s3://b/x.parquet}
dimensions:
  - name: region
measures:
  - name: n
    expr: count()
facts:
  - model: marketing
""")
    assert parsed.source is not None
    assert not parsed.is_composite      # it has a fact table of its own
    assert [f.model for f in parsed.facts] == ["marketing"]
    assert list(parsed.dimensions) == ["region"]
    assert list(parsed.measures) == ["n"]


def test_a_model_cannot_list_itself_as_a_fact():
    with pytest.raises(ModelError, match="lists itself under 'facts'"):
        _parse("""
name: loop
source: {format: parquet, path: s3://b/x.parquet}
dimensions:
  - name: region
measures:
  - name: n
    expr: count()
facts:
  - model: loop
    alias: again
""")


def test_a_fact_entry_needs_a_model():
    with pytest.raises(ModelError, match="needs a 'model'"):
        _parse("name: bad\nfacts:\n  - alias: nope\n")


def test_duplicate_fact_aliases_are_rejected():
    with pytest.raises(ModelError, match="duplicate fact alias 'sales'"):
        _parse("name: bad\nfacts:\n  - model: sales\n  - model: sales\n")


def test_an_alias_defaults_to_the_fact_model_name():
    parsed = _parse("name: ok\nfacts:\n  - model: sales\n  - model: marketing\n    alias: mkt\n")
    assert [f.alias for f in parsed.facts] == ["sales", "mkt"]
    assert parsed.is_composite
    assert parsed.source is None


def test_an_alias_must_be_an_identifier_since_it_prefixes_measures():
    with pytest.raises(ModelError, match="must be lowercase letters"):
        _parse("name: bad\nfacts:\n  - model: sales\n    alias: 'Sales Orders'\n")


def test_map_is_no_longer_accepted_and_says_what_to_do_instead():
    """Conformance is a property of the fact, declared once on the fact model.
    A stale `map:` is an error rather than a silently ignored key."""
    with pytest.raises(ModelError, match="'map' is no longer supported"):
        _parse("name: bad\nfacts:\n  - model: sales\n    map: {date: order_date}\n")


# ── resolving the shared catalog ──────────────────────────────────

def test_shared_dimensions_are_the_intersection_not_the_union(models):
    """model.dimensions is the all-facts intersection: what stays groupable
    whichever measures you end up asking for."""
    overview = models["commercial_overview"]
    # all three facts import the calendar bundle, so they conform on its
    # dimensions with nothing declared on the multi-fact model at all
    assert "calendar_date" in overview.dimensions
    assert "calendar_quarter" in overview.dimensions
    assert "region" in overview.dimensions
    # channel is on sales and marketing but not subscriptions, so it is not
    # offered up front — a query reading only those two still gets it, see
    # test_the_catalog_is_the_intersection_of_the_facts_a_query_reads
    assert "channel" in models["sales"].dimensions
    assert "channel" in models["marketing"].dimensions
    assert "channel" not in overview.dimensions
    # ...and neither is anything only one fact knows about
    assert "plan" not in overview.dimensions
    assert "category" not in overview.dimensions


def test_every_facts_measures_are_offered_under_an_alias_prefix(models):
    overview = models["commercial_overview"]
    assert "sales.revenue" in overview.measures
    assert "marketing.spend" in overview.measures
    assert "subs.active_customers" in overview.measures
    # each fact's whole measure list comes across, prefix and all
    for alias, fact in (("sales", "sales"), ("marketing", "marketing"), ("subs", "subscriptions")):
        assert {f"{alias}.{m}" for m in models[fact].measures} <= set(overview.measures)


def test_a_measure_label_names_the_fact_it_came_from(models):
    overview = models["commercial_overview"]
    assert overview.measures["sales.revenue"].label.endswith(models["sales"].label)


def test_an_unknown_fact_model_is_a_load_time_error(models):
    with pytest.raises(ModelError, match="references unknown model 'nope'"):
        _resolved("name: bad\nfacts:\n  - model: nope\n", models)


def test_a_fact_cannot_itself_read_facts(models):
    """Facts don't nest, whether the target is a standalone multi-fact model or
    a fact model that borrows from its neighbours."""
    with pytest.raises(ModelError, match="which reads facts of its own"):
        _resolved("name: bad\nfacts:\n  - model: commercial_overview\n", models)
    borrower = _parse(
        "name: borrower\nsource: {format: parquet, path: s3://b/x.parquet}\n"
        "dimensions:\n  - name: region\nmeasures:\n  - name: n\n    expr: count()\n"
        "facts:\n  - model: marketing\n"
    )
    with pytest.raises(ModelError, match="which reads facts of its own"):
        _resolved("name: bad\nfacts:\n  - model: borrower\n", {**models, "borrower": borrower})


def _fact_with(name: str, dim: str, dim_type: str) -> semantic.Model:
    """A throwaway single-source model offering one dimension of a given type
    — enough to exercise conformance checks without a real dataset."""
    model = semantic.Model(name=name, label=name, description="",
                           source=semantic.Source(format="parquet", path=f"s3://b/{name}.parquet"))
    model.dimensions = {dim: semantic.Dimension(name=dim, column=dim, label=dim, type=dim_type)}
    return model


def test_facts_that_disagree_on_a_dimensions_type_are_rejected(models):
    # one fact calls "when" a time dimension, the other a categorical one — the
    # shared name can't be grouped consistently, so say so at load time
    others = {**models,
              "clock": _fact_with("clock", "when", "time"),
              "label": _fact_with("label", "when", "string")}
    with pytest.raises(ModelError, match="shared dimension 'when' is"):
        _resolved("name: bad\nfacts:\n  - model: clock\n  - model: label\n", others)


def test_a_type_clash_is_caught_even_outside_the_all_facts_intersection(models):
    """Two facts disagreeing on a type is a load-time error even when a third
    fact keeps the name out of model.dimensions — a query reading just those
    two would otherwise conform on it at run time."""
    others = {**models,
              "clock": _fact_with("clock", "when", "time"),
              "label": _fact_with("label", "when", "string"),
              "neither": _fact_with("neither", "other", "string")}
    with pytest.raises(ModelError, match="shared dimension 'when' is"):
        _resolved("name: bad\nfacts:\n  - model: clock\n  - model: label\n  - model: neither\n", others)


def test_facts_conform_on_bundle_dimensions_without_any_map(models):
    """sales and clinical models aside, two models importing the same bundle
    already agree on its dimension names — that is what a bundle is for."""
    overview = _resolved("name: geo\nfacts:\n  - model: sales\n  - model: logistics\n", models)
    shared = set(overview.dimensions)
    assert "region" in shared and "territory" in shared


# ── querying ──────────────────────────────────────────────────────

def _by_date(result: dict, measure: str) -> dict:
    return {row["calendar_date"]: row[measure] for row in result["rows"]}


def test_each_facts_measure_matches_what_that_fact_returns_alone(models):
    """The whole point: putting three unrelated facts on one axis must not
    change any of their numbers."""
    overview = models["commercial_overview"]
    together = engine.run_query(overview, {
        "dimensions": [{"name": "calendar_date", "grain": "1mo"}],
        "measures": ["marketing.spend", "sales.revenue", "subs.active_customers"],
        "limit": 500,
    })

    alone = {
        "marketing.spend": engine.run_query(models["marketing"], {
            "dimensions": [{"name": "month", "grain": "1mo"}], "measures": ["spend"], "limit": 500}),
        "sales.revenue": engine.run_query(models["sales"], {
            "dimensions": [{"name": "order_date", "grain": "1mo"}], "measures": ["revenue"], "limit": 500}),
        "subs.active_customers": engine.run_query(models["subscriptions"], {
            "dimensions": [{"name": "calendar_date", "grain": "1mo"}],
            "measures": ["active_customers"], "limit": 500}),
    }
    for qualified, solo in alone.items():
        own = qualified.split(".", 1)[1]
        solo_by_date = {row[next(iter(row))]: row[own] for row in solo["rows"]}
        merged = _by_date(together, qualified)
        for bucket, value in solo_by_date.items():
            assert merged[bucket] == pytest.approx(value), f"{qualified} @ {bucket}"


def test_the_merged_axis_is_the_union_of_the_facts_buckets(models):
    """A bucket only one fact has rows for keeps its row; the others read null
    rather than a zero nobody measured."""
    overview = models["commercial_overview"]
    result = engine.run_query(overview, {
        "dimensions": [{"name": "calendar_date", "grain": "1mo"}],
        "measures": ["marketing.spend", "subs.active_customers"],
        "limit": 500,
    })
    dates = [row["calendar_date"] for row in result["rows"]]
    assert dates == sorted(dates)              # time ascending by default
    assert len(dates) == len(set(dates))       # one row per bucket, not a cross product
    assert any(row["marketing.spend"] is not None for row in result["rows"])
    assert any(row["subs.active_customers"] is not None for row in result["rows"])


def test_measures_do_not_inflate_when_a_second_fact_joins_the_query(models):
    """A fact-to-fact join would multiply each side by the other's row count.
    Asking for one measure or three has to give the same numbers."""
    overview = models["commercial_overview"]
    query = {"dimensions": [{"name": "calendar_date", "grain": "1q"}, "region"], "limit": 500}
    one = engine.run_query(overview, {**query, "measures": ["sales.revenue"]})
    three = engine.run_query(overview, {
        **query, "measures": ["sales.revenue", "marketing.spend", "subs.active_customers"]})

    keyed = lambda r, m: {(row["calendar_date"], row["region"]): row[m] for row in r["rows"]}
    solo, joint = keyed(one, "sales.revenue"), keyed(three, "sales.revenue")
    assert solo  # the query returns something to compare in the first place
    for key, value in solo.items():
        assert joint[key] == pytest.approx(value)


def test_a_filter_on_a_shared_dimension_reaches_every_fact(models):
    overview = models["commercial_overview"]
    result = engine.run_query(overview, {
        "dimensions": ["region"],
        "measures": ["sales.revenue", "marketing.spend"],
        "filters": [{"field": "region", "op": "eq", "value": "Euro-Zone"}],
        "limit": 100,
    })
    assert [row["region"] for row in result["rows"]] == ["Euro-Zone"]
    assert result["rows"][0]["sales.revenue"] > 0
    assert result["rows"][0]["marketing.spend"] > 0


def test_grouping_by_a_dimension_a_read_fact_lacks_is_refused(models):
    with pytest.raises(engine.QueryError, match="'channel' is not shared by the facts"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["channel"], "measures": ["sales.revenue", "subs.mrr"]})


def test_the_refusal_names_the_fact_that_lacks_the_dimension(models):
    """Which measure to drop is the actionable half of the message."""
    with pytest.raises(engine.QueryError, match="'subs' doesn't offer it"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["channel"], "measures": ["sales.revenue", "subs.mrr"]})


def test_filtering_on_a_dimension_a_read_fact_lacks_is_refused(models):
    with pytest.raises(engine.QueryError, match="'plan' is not shared by the facts"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["region"], "measures": ["sales.revenue", "marketing.spend"],
            "filters": [{"field": "plan", "op": "eq", "value": "pro"}]})


# ── the catalog follows the query, not the model ──────────────────

def test_the_catalog_is_the_intersection_of_the_facts_a_query_reads(models):
    """`channel` is on sales and marketing but not subscriptions. A query that
    measures only the first two never reads a subscriptions row, so there is
    nothing dishonest about a channel axis — and no reason to make the user
    declare a second multi-fact model for the pair."""
    overview = models["commercial_overview"]
    assert "channel" not in overview.dimensions          # not offered up front
    result = engine.run_query(overview, {
        "dimensions": ["channel"],
        "measures": ["sales.revenue", "marketing.spend"], "limit": 100})
    assert result["rows"]
    for row in result["rows"]:
        assert row["channel"] is not None
    assert [c["name"] for c in result["columns"]] == ["channel", "sales.revenue", "marketing.spend"]


def test_a_per_query_dimension_still_matches_what_each_fact_returns_alone(models):
    """The subset catalog must not change any numbers either — the merge is
    the same one, over fewer facts."""
    overview = models["commercial_overview"]
    together = engine.run_query(overview, {
        "dimensions": ["channel"],
        "measures": ["sales.revenue", "marketing.spend"], "limit": 100})
    merged = {row["channel"]: row for row in together["rows"]}
    for alias, fact, own in (("sales", "sales", "revenue"), ("marketing", "marketing", "spend")):
        solo = engine.run_query(models[fact], {
            "dimensions": ["channel"], "measures": [own], "limit": 100})
        for row in solo["rows"]:
            assert merged[row["channel"]][f"{alias}.{own}"] == pytest.approx(row[own])


def test_adding_a_third_facts_measure_withdraws_the_dimension(models):
    """The same axis that was fine for two facts is refused once a fact that
    can't answer for it is on the query."""
    overview = models["commercial_overview"]
    query = {"dimensions": ["channel"], "limit": 100}
    engine.run_query(overview, {**query, "measures": ["sales.revenue", "marketing.spend"]})
    with pytest.raises(engine.QueryError, match="not shared by the facts this query reads"):
        engine.run_query(overview, {
            **query, "measures": ["sales.revenue", "marketing.spend", "subs.mrr"]})


# ── a fact model that also reads its neighbours ───────────────────

BORROWING_SALES_YAML = (
    Path("models/sales.yaml").read_text() + "\nfacts:\n  - model: marketing\n    alias: mkt\n"
)


@pytest.fixture
def borrowing_sales(models):
    """`sales` as it stands, plus marketing's measures — the same reading as
    commercial_overview, expressed from inside a fact model instead of from a
    standalone list."""
    from app import config

    model = semantic.parse_model_text(BORROWING_SALES_YAML)
    semantic.resolve_imports(model, semantic.load_dimension_bundles(config.DIMENSIONS_DIR))
    return semantic.resolve_facts(model, models)


def test_a_host_keeps_its_own_catalog_whole(borrowing_sales, models):
    """Adding facts to a fact model must not narrow it: everything sales could
    be grouped by before is still there, and its measures keep their names."""
    assert set(models["sales"].dimensions) <= set(borrowing_sales.dimensions)
    assert "category" in borrowing_sales.dimensions      # sales-only, still offered
    assert "revenue" in borrowing_sales.measures         # unprefixed
    assert "mkt.spend" in borrowing_sales.measures       # borrowed, prefixed
    assert "spend" not in borrowing_sales.measures


def test_a_host_only_query_is_unchanged_by_the_facts_it_lists(borrowing_sales, models):
    query = {"dimensions": ["category"], "measures": ["revenue"], "limit": 100}
    borrowed = engine.run_query(borrowing_sales, query)
    plain = engine.run_query(models["sales"], query)
    assert borrowed["columns"] == plain["columns"]
    assert [r["category"] for r in borrowed["rows"]] == [r["category"] for r in plain["rows"]]
    for got, want in zip(borrowed["rows"], plain["rows"]):
        assert got["revenue"] == pytest.approx(want["revenue"])


def test_a_host_only_query_still_takes_inline_measures(borrowing_sales):
    """The merge can't scope an expression to one fact, but a query that
    borrows nothing never reaches it."""
    result = engine.run_query(borrowing_sales, {
        "dimensions": ["category"], "measures": ["adhoc"], "limit": 10,
        "inline_measures": [{"name": "adhoc", "expr": "count()"}]})
    assert result["rows"] and result["rows"][0]["adhoc"] > 0


def test_borrowing_a_measure_merges_without_inflating_the_hosts(borrowing_sales, models):
    both = engine.run_query(borrowing_sales, {
        "dimensions": ["channel"], "measures": ["revenue", "mkt.spend"], "limit": 100})
    alone = engine.run_query(models["sales"], {
        "dimensions": ["channel"], "measures": ["revenue"], "limit": 100})
    merged = {row["channel"]: row for row in both["rows"]}
    for row in alone["rows"]:
        assert merged[row["channel"]]["revenue"] == pytest.approx(row["revenue"])
    assert any(row["mkt.spend"] is not None for row in both["rows"])


def test_borrowing_narrows_the_axis_to_what_both_facts_have(borrowing_sales):
    """`category` is sales-only: fine on its own, refused once a marketing
    measure is on the same query."""
    engine.run_query(borrowing_sales, {
        "dimensions": ["category"], "measures": ["revenue"], "limit": 10})
    with pytest.raises(engine.QueryError, match="'category' is not shared by the facts"):
        engine.run_query(borrowing_sales, {
            "dimensions": ["category"], "measures": ["revenue", "mkt.spend"], "limit": 10})


def test_a_borrowed_query_cannot_also_carry_an_inline_measure(borrowing_sales):
    with pytest.raises(engine.QueryError, match="can't mix inline measures"):
        engine.run_query(borrowing_sales, {
            "dimensions": ["channel"], "measures": ["mkt.spend", "adhoc"],
            "inline_measures": [{"name": "adhoc", "expr": "count()"}]})


def test_the_host_is_not_reported_as_a_borrowed_fact(borrowing_sales):
    public = borrowing_sales.to_public()
    assert public["kind"] == "fact"          # it has a fact table of its own
    assert public["path"] is not None
    assert [f["alias"] for f in public["facts"]] == ["mkt"]


def test_the_guided_form_round_trips_the_facts_list():
    """The form rebuilds the whole file on save, so it has to carry `facts:`
    through or an unrelated edit would silently drop it. model_to_spec reads an
    unresolved model, the same as GET /models/{name}/spec."""
    original = semantic.parse_model_text(BORROWING_SALES_YAML)
    spec = semantic.model_to_spec(original)
    assert spec["facts"] == [{"model": "marketing", "alias": "mkt"}]

    reparsed = semantic.parse_model_text(semantic.spec_to_yaml(spec))
    assert [(f.model, f.alias) for f in reparsed.facts] == [("marketing", "mkt")]
    assert reparsed.source is not None                    # still its own fact model
    # ...and the rest of the file survives the trip untouched
    assert list(reparsed.dimensions) == list(original.dimensions)
    assert list(reparsed.measures) == list(original.measures)
    assert [(i.bundle, i.left_on, i.right_on) for i in reparsed.imports] == \
        [(i.bundle, i.left_on, i.right_on) for i in original.imports]


def test_a_default_alias_is_not_written_back_out(models):
    """`alias` defaults to the fact's own name, so writing it would be noise."""
    spec = semantic.model_to_spec(models["sales"])
    spec["facts"] = [{"model": "marketing", "alias": "marketing"}]
    text = semantic.spec_to_yaml(spec)
    assert "- model: marketing" in text and "alias" not in text
    assert [f.alias for f in semantic.parse_model_text(text).facts] == ["marketing"]


def test_the_guided_form_still_declines_a_standalone_multi_fact_model(models):
    """It has no fact table of its own — every panel of the form would be
    empty, so the yaml editor is the only place it can be edited."""
    with pytest.raises(ModelError, match="no fact table of its own"):
        semantic.model_to_spec(models["commercial_overview"])


def test_a_host_is_scannable_unlike_a_standalone_multi_fact_model(borrowing_sales, models):
    assert "category" in engine.scan(borrowing_sales, {"category": None}).collect_schema()
    with pytest.raises(engine.QueryError, match="no single source to scan"):
        engine.scan(models["commercial_overview"])


def test_columns_name_the_host_as_the_fact_for_its_own_measures(borrowing_sales):
    result = engine.run_query(borrowing_sales, {
        "dimensions": ["channel"], "measures": ["revenue", "mkt.spend"], "limit": 10})
    by_name = {c["name"]: c for c in result["columns"]}
    assert by_name["revenue"]["fact"] == "sales"
    assert by_name["mkt.spend"]["fact"] == "mkt"


def test_one_fact_alone_offers_everything_that_fact_offers(models):
    """A single-fact query on a multi-fact model conforms with itself, so it
    can be grouped by anything that fact has — `category` is sales-only."""
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": ["category"], "measures": ["sales.revenue"], "limit": 10})
    assert result["rows"] and result["rows"][0]["category"] is not None


def test_an_unknown_qualified_measure_is_refused(models):
    with pytest.raises(engine.QueryError, match="unknown measure 'sales.nope'"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["region"], "measures": ["sales.nope"]})
    with pytest.raises(engine.QueryError, match="unknown measure"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["region"], "measures": ["revenue"]})


def test_grand_totals_need_no_dimension_at_all(models):
    result = engine.run_query(models["commercial_overview"], {
        "measures": ["sales.revenue", "marketing.spend"]})
    assert result["row_count"] == 1
    row = result["rows"][0]
    assert row["sales.revenue"] > 0 and row["marketing.spend"] > 0


def test_only_the_facts_a_query_asks_for_are_read(models):
    """A query naming no subscriptions measure must not pull the calendar
    interval join in — the result would still be right, but it would cost a
    scan of a table nothing on the chart came from."""
    overview = models["commercial_overview"]
    result = engine.run_query(overview, {
        "dimensions": [{"name": "calendar_date", "grain": "1mo"}], "measures": ["sales.revenue"]})
    assert set(result["rows"][0]) == {"calendar_date", "sales.revenue"}
    assert [c["name"] for c in result["columns"]] == ["calendar_date", "sales.revenue"]


def test_columns_carry_the_shared_label_and_the_owning_fact(models):
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": [{"name": "calendar_date", "grain": "1y"}], "measures": ["subs.mrr"]})
    date_col, measure_col = result["columns"]
    # the axis is a real dimension with a real label, owned by the bundle the
    # facts conform through — not a name invented by this model
    assert date_col == {"name": "calendar_date", "label": "Calendar Date",
                        "kind": "dimension", "type": "time"}
    assert measure_col["fact"] == "subs"
    assert measure_col["format"] == "currency"


def test_a_shared_dimension_keeps_the_facts_label_and_synonyms(models):
    overview = models["commercial_overview"]
    assert overview.dimensions["region"].label == models["sales"].dimensions["region"].label
    calendar = models["subscriptions"].dimensions["calendar_date"]
    assert overview.dimensions["calendar_date"].label == calendar.label
    assert overview.dimensions["calendar_date"].synonyms == calendar.synonyms


def test_sort_and_limit_apply_after_the_merge(models):
    overview = models["commercial_overview"]
    result = engine.run_query(overview, {
        "dimensions": ["region"], "measures": ["sales.revenue"],
        "sort": {"by": "sales.revenue", "desc": True}, "limit": 2,
    })
    values = [row["sales.revenue"] for row in result["rows"]]
    assert len(values) == 2 and values == sorted(values, reverse=True)


def test_inline_measures_are_refused_on_a_multi_fact_model(models):
    with pytest.raises(engine.QueryError, match="doesn't take inline measures"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["region"], "measures": ["adhoc"],
            "inline_measures": [{"name": "adhoc", "expr": "count()"}]})


def test_scanning_a_multi_fact_model_says_why_it_cannot(models):
    with pytest.raises(engine.QueryError, match="no single source to scan"):
        engine.scan(models["commercial_overview"])


def test_dimension_values_come_from_whichever_fact_can_supply_them(models):
    values = engine.dimension_values(models["commercial_overview"], "region")
    assert set(values) == set(engine.dimension_values(models["sales"], "region"))


# ── the form/editor surface ───────────────────────────────────────

def test_the_guided_form_declines_a_multi_fact_model(models):
    with pytest.raises(ModelError, match="edit its 'facts' list in the yaml editor"):
        semantic.model_to_spec(models["commercial_overview"])


def test_to_public_reports_the_facts_and_marks_the_kind(models):
    public = models["commercial_overview"].to_public()
    assert public["kind"] == "composite"
    assert public["path"] is None
    assert [f["alias"] for f in public["facts"]] == ["marketing", "sales", "subs"]
    assert "sales.revenue" in next(f for f in public["facts"] if f["alias"] == "sales")["measures"]


def test_a_single_source_model_is_still_reported_as_a_fact(models):
    public = models["sales"].to_public()
    assert public["kind"] == "fact"
    assert public["facts"] == []


def test_a_multi_fact_model_reads_no_bucket_objects_of_its_own(models):
    matchers = semantic.model_source_matchers(list(models.values()), "cash-intel")
    assert all(name != "commercial_overview" for name, _role, _match in matchers)
