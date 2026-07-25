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

def test_a_model_declares_either_a_source_or_facts_never_both():
    with pytest.raises(ModelError, match="either 'source'.*or 'facts'"):
        _parse("""
name: bad
source: {format: parquet, path: s3://b/x.parquet}
facts:
  - model: sales
""")


@pytest.mark.parametrize("key,block", [
    ("dimensions", "dimensions:\n  - name: region"),
    ("measures", "measures:\n  - name: n\n    expr: count()"),
    ("joins", "joins:\n  - name: j\n    source: {format: csv, path: s3://b/j.csv}\n    on: id"),
    ("dimension_imports", "dimension_imports:\n  - bundle: geography\n    anchor_dataset: regions\n    on: region"),
])
def test_a_multi_fact_model_cannot_declare_its_own_fields(key, block):
    with pytest.raises(ModelError, match=f"declares '{key}'"):
        _parse(f"name: bad\nfacts:\n  - model: sales\n{block}\n")


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


def test_map_must_be_a_mapping():
    with pytest.raises(ModelError, match="'map' must be a mapping"):
        _parse("name: bad\nfacts:\n  - model: sales\n    map: [date]\n")


# ── resolving the shared catalog ──────────────────────────────────

def test_shared_dimensions_are_the_intersection_not_the_union(models):
    overview = models["commercial_overview"]
    assert list(overview.dimensions) == ["date", "region"]
    # channel is on sales and marketing but not subscriptions, so it is not
    # offered: there would be no honest subscriptions number for a channel row
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


def test_a_fact_cannot_itself_be_a_multi_fact_model(models):
    with pytest.raises(ModelError, match="which is itself"):
        _resolved("name: bad\nfacts:\n  - model: commercial_overview\n", models)


def test_mapping_to_a_dimension_the_fact_does_not_have_is_an_error(models):
    with pytest.raises(ModelError, match="which model 'sales' does not declare"):
        _resolved("name: bad\nfacts:\n  - model: sales\n    map:\n      date: nope\n", models)


def test_mapping_onto_a_name_the_fact_already_uses_is_an_error(models):
    with pytest.raises(ModelError, match="already has a dimension of its own called 'channel'"):
        _resolved("name: bad\nfacts:\n  - model: sales\n    map:\n      channel: order_date\n", models)


def test_facts_that_disagree_on_a_dimensions_type_are_rejected(models):
    # one fact calls a time dimension "when", the other a categorical one —
    # the shared name can't be grouped consistently, so say so at load time
    with pytest.raises(ModelError, match="shared dimension 'when' is"):
        _resolved("""
name: bad
facts:
  - model: marketing
    map:
      when: month
  - model: sales
    map:
      when: channel
""", models)


def test_facts_conform_on_bundle_dimensions_without_any_map(models):
    """sales and clinical models aside, two models importing the same bundle
    already agree on its dimension names — that is what a bundle is for."""
    overview = _resolved("name: geo\nfacts:\n  - model: sales\n  - model: logistics\n", models)
    shared = set(overview.dimensions)
    assert "region" in shared and "territory" in shared


# ── querying ──────────────────────────────────────────────────────

def _by_date(result: dict, measure: str) -> dict:
    return {row["date"]: row[measure] for row in result["rows"]}


def test_each_facts_measure_matches_what_that_fact_returns_alone(models):
    """The whole point: putting three unrelated facts on one axis must not
    change any of their numbers."""
    overview = models["commercial_overview"]
    together = engine.run_query(overview, {
        "dimensions": [{"name": "date", "grain": "1mo"}],
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
        "dimensions": [{"name": "date", "grain": "1mo"}],
        "measures": ["marketing.spend", "subs.active_customers"],
        "limit": 500,
    })
    dates = [row["date"] for row in result["rows"]]
    assert dates == sorted(dates)              # time ascending by default
    assert len(dates) == len(set(dates))       # one row per bucket, not a cross product
    assert any(row["marketing.spend"] is not None for row in result["rows"])
    assert any(row["subs.active_customers"] is not None for row in result["rows"])


def test_measures_do_not_inflate_when_a_second_fact_joins_the_query(models):
    """A fact-to-fact join would multiply each side by the other's row count.
    Asking for one measure or three has to give the same numbers."""
    overview = models["commercial_overview"]
    query = {"dimensions": [{"name": "date", "grain": "1q"}, "region"], "limit": 500}
    one = engine.run_query(overview, {**query, "measures": ["sales.revenue"]})
    three = engine.run_query(overview, {
        **query, "measures": ["sales.revenue", "marketing.spend", "subs.active_customers"]})

    keyed = lambda r, m: {(row["date"], row["region"]): row[m] for row in r["rows"]}
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


def test_grouping_by_a_dimension_only_one_fact_has_is_refused(models):
    with pytest.raises(engine.QueryError, match="not a shared dimension"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["channel"], "measures": ["sales.revenue"]})


def test_filtering_on_a_dimension_only_one_fact_has_is_refused(models):
    with pytest.raises(engine.QueryError, match="a filter has to mean the same thing"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["region"], "measures": ["sales.revenue"],
            "filters": [{"field": "plan", "op": "eq", "value": "pro"}]})


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
        "dimensions": [{"name": "date", "grain": "1mo"}], "measures": ["sales.revenue"]})
    assert set(result["rows"][0]) == {"date", "sales.revenue"}
    assert [c["name"] for c in result["columns"]] == ["date", "sales.revenue"]


def test_columns_carry_the_shared_label_and_the_owning_fact(models):
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": [{"name": "date", "grain": "1y"}], "measures": ["subs.mrr"]})
    date_col, measure_col = result["columns"]
    # a mapped dimension is labelled from the shared name — borrowing the first
    # fact's label ("Month", from marketing) would mislabel the axis at 1y
    assert date_col == {"name": "date", "label": "Date", "kind": "dimension", "type": "time"}
    assert measure_col["fact"] == "subs"
    assert measure_col["format"] == "currency"


def test_an_unmapped_shared_dimension_keeps_the_facts_label(models):
    overview = models["commercial_overview"]
    assert overview.dimensions["region"].label == models["sales"].dimensions["region"].label


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
