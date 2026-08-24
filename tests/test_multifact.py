"""Models holding several unrelated fact tables.

A model's datasets don't have to be related to each other: two fact tables that
each relate to a common dimensional model, and to nothing else, is the ordinary
shape. Each connected component of the relation graph is a part, and the
contract under test is that the parts are never joined to each other — each is
queried on its own and the aggregates are merged on the dimensions they share —
so every measure keeps the grain of its own table no matter what else is on the
chart.
"""
import pytest

from app import engine, semantic
from app.semantic import ModelError


def _parse(text: str) -> semantic.Model:
    return semantic.parse_model_text(text)


def _resolved(text: str, bundles: dict | None = None) -> semantic.Model:
    return semantic.resolve_model(_parse(text), bundles or {})


# two fact tables that share nothing but the dimension names they both declare
TWO_FACTS = """
name: pair
datasets:
  - name: orders
    source: {format: parquet, path: s3://b/orders/*.parquet}
    dimensions:
      - name: region
      - name: channel
    measures:
      - name: revenue
        expr: SUM(amount)
  - name: spend
    source: {format: parquet, path: s3://b/spend/*.parquet}
    dimensions:
      - name: region
    measures:
      - name: cost
        expr: SUM(cost)
"""


# ── parsing ────────────────────────────────────────────────────────

def test_facts_is_gone_and_says_what_to_do_instead():
    """The old shape listed *other models* as facts. It's an error now rather
    than a silently ignored key, and the message names the replacement."""
    with pytest.raises(ModelError, match="'facts:' is no longer supported"):
        _parse("name: bad\nfacts:\n  - model: sales\n")


@pytest.mark.parametrize("key,block", [
    ("source", "source: {format: parquet, path: s3://b/x.parquet}"),
    ("joins", "joins:\n  - name: j\n    source: {format: csv, path: s3://b/j.csv}\n    on: id"),
    ("dimensions", "dimensions:\n  - name: region"),
    ("measures", "measures:\n  - name: n\n    expr: COUNT(*)"),
])
def test_the_two_spellings_cannot_be_mixed(key, block):
    """`source`/`joins`/`dimensions`/`measures` are the single-dataset
    shorthand. Alongside `datasets:` they would be silently ignored, so they
    are an error that says where they belong instead."""
    with pytest.raises(ModelError, match=f"both 'datasets' and .*{key}"):
        _parse(f"""
name: bad
datasets:
  - name: y
    source: {{format: parquet, path: s3://b/y.parquet}}
    measures: [{{name: m, expr: COUNT(*)}}]
{block}
""")


def test_a_model_with_no_datasets_at_all_says_so():
    with pytest.raises(ModelError, match="has no datasets"):
        _parse("name: bad\nlabel: Bad\n")


def test_duplicate_dataset_names_are_rejected():
    with pytest.raises(ModelError, match="duplicate dataset 'x'"):
        _parse("""
name: bad
datasets:
  - name: x
    source: {format: parquet, path: s3://b/a.parquet}
  - name: x
    source: {format: parquet, path: s3://b/b.parquet}
""")


def test_a_relation_to_an_unknown_dataset_is_rejected():
    with pytest.raises(ModelError, match="relates to unknown dataset 'nope'"):
        _parse("""
name: bad
datasets:
  - name: x
    source: {format: parquet, path: s3://b/a.parquet}
    joins:
      - to: nope
        on: id
""")


def test_the_terse_source_spelling_parses_into_the_same_datasets():
    """`source:`/`joins:` is shorthand, not a second concept — after parsing
    there is one shape, and the join is a dataset like any other."""
    model = _parse("""
name: shop
source: {format: parquet, path: s3://b/orders/*.parquet}
joins:
  - name: products
    source: {format: csv, path: s3://b/products.csv}
    on: product
dimensions:
  - name: region
measures:
  - name: n
    expr: COUNT(*)
""")
    assert list(model.datasets) == ["shop", "products"]
    assert model.datasets["shop"].joins[0].to == "products"
    assert "region" in model.datasets["shop"].dimensions
    assert "n" in model.datasets["shop"].measures


# ── splitting into parts ───────────────────────────────────────────

def test_related_datasets_are_one_fact_table():
    model = _resolved("""
name: shop
datasets:
  - name: orders
    source: {format: parquet, path: s3://b/orders/*.parquet}
    joins:
      - to: products
        on: product
    measures:
      - name: n
        expr: COUNT(*)
  - name: products
    source: {format: csv, path: s3://b/products.csv}
    dimensions:
      - name: supplier
""")
    assert [p.name for p in model.parts] == ["orders"]
    assert not model.is_composite
    # a single-part model *is* its part: source/joins/catalog sit on the model
    assert model.parts[0].model is model
    assert model.source.path == "s3://b/orders/*.parquet"
    assert [j.name for j in model.joins] == ["products"]
    # dimensions pool across the component — one joined frame to read them from
    assert "supplier" in model.dimensions


def test_unrelated_datasets_are_separate_fact_tables():
    model = _resolved(TWO_FACTS)
    assert [p.name for p in model.parts] == ["orders", "spend"]
    assert model.is_composite
    assert model.source is None and model.joins == []


def test_the_catalog_is_the_intersection_of_dimensions_and_the_union_of_measures():
    model = _resolved(TWO_FACTS)
    assert list(model.dimensions) == ["region"]      # channel is orders-only
    assert list(model.measures) == ["revenue", "cost"]


def test_measures_keep_their_own_names_and_map_back_to_their_fact_table():
    model = _resolved(TWO_FACTS)
    assert semantic.part_for_measure(model, "revenue").name == "orders"
    assert semantic.part_for_measure(model, "cost").name == "spend"
    with pytest.raises(ModelError, match="unknown measure 'nope'"):
        semantic.part_for_measure(model, "nope")


def test_a_measure_name_used_by_two_fact_tables_is_rejected():
    with pytest.raises(ModelError, match="measure 'total' is declared on both"):
        _resolved("""
name: bad
datasets:
  - name: a
    source: {format: parquet, path: s3://b/a.parquet}
    measures: [{name: total, expr: COUNT(*)}]
  - name: b
    source: {format: parquet, path: s3://b/b.parquet}
    measures: [{name: total, expr: COUNT(*)}]
""")


def test_a_dimension_name_reused_by_two_related_datasets_is_rejected():
    """Within one fact table there is a single joined frame, so two datasets
    claiming the same dimension name is a collision — unlike across fact
    tables, where it is the conformance mechanism."""
    with pytest.raises(ModelError, match="dimension 'region' is declared by both"):
        _resolved("""
name: bad
datasets:
  - name: a
    source: {format: parquet, path: s3://b/a.parquet}
    joins: [{to: b, on: id}]
    dimensions: [{name: region}]
    measures: [{name: n, expr: COUNT(*)}]
  - name: b
    source: {format: parquet, path: s3://b/b.parquet}
    dimensions: [{name: region}]
""")


def test_a_stranded_dataset_with_nothing_to_measure_is_rejected():
    """It would contribute no measures and silently narrow what the model's
    real fact tables can be grouped by."""
    with pytest.raises(ModelError, match="declares no measures"):
        _resolved("""
name: bad
datasets:
  - name: orders
    source: {format: parquet, path: s3://b/orders/*.parquet}
    measures: [{name: n, expr: COUNT(*)}]
  - name: stray
    source: {format: csv, path: s3://b/stray.csv}
    dimensions: [{name: colour}]
""")


def test_fact_tables_that_disagree_on_a_dimensions_type_are_rejected():
    with pytest.raises(ModelError, match="shared dimension 'when' is"):
        _resolved("""
name: bad
datasets:
  - name: a
    source: {format: parquet, path: s3://b/a.parquet}
    dimensions: [{name: when, type: time}]
    measures: [{name: n, expr: COUNT(*)}]
  - name: b
    source: {format: parquet, path: s3://b/b.parquet}
    dimensions: [{name: when, type: categorical}]
    measures: [{name: m, expr: COUNT(*)}]
""")


def test_a_type_clash_is_caught_even_outside_the_all_parts_intersection():
    """Two fact tables disagreeing is a load-time error even when a third keeps
    the name out of model.dimensions — a query reading just those two would
    otherwise conform on it at run time."""
    with pytest.raises(ModelError, match="shared dimension 'when' is"):
        _resolved("""
name: bad
datasets:
  - name: a
    source: {format: parquet, path: s3://b/a.parquet}
    dimensions: [{name: when, type: time}]
    measures: [{name: n, expr: COUNT(*)}]
  - name: b
    source: {format: parquet, path: s3://b/b.parquet}
    dimensions: [{name: when, type: categorical}]
    measures: [{name: m, expr: COUNT(*)}]
  - name: c
    source: {format: parquet, path: s3://b/c.parquet}
    dimensions: [{name: other}]
    measures: [{name: o, expr: COUNT(*)}]
""")


# ── imports across several fact tables ─────────────────────────────

def test_an_import_must_say_which_fact_table_it_relates_to(models):
    with pytest.raises(ModelError, match="needs 'from_dataset'"):
        _parse("""
name: bad
datasets:
  - name: a
    source: {format: parquet, path: s3://b/a.parquet}
    measures: [{name: n, expr: COUNT(*)}]
  - name: b
    source: {format: parquet, path: s3://b/b.parquet}
    measures: [{name: m, expr: COUNT(*)}]
dimension_imports:
  - bundle: calendar
    anchor_dataset: days
    on: date
""")


def test_a_single_fact_table_needs_no_from_dataset():
    """With one fact table there is only one thing an import could relate to,
    which is what the terse spelling always relies on."""
    model = _parse("""
name: ok
source: {format: parquet, path: s3://b/a.parquet}
dimension_imports:
  - bundle: calendar
    anchor_dataset: days
    on: date
""")
    assert model.imports[0].from_dataset == "ok"


def test_an_import_from_an_unknown_dataset_is_rejected():
    with pytest.raises(ModelError, match="relates from unknown dataset 'nope'"):
        _parse("""
name: bad
source: {format: parquet, path: s3://b/a.parquet}
dimension_imports:
  - bundle: calendar
    from_dataset: nope
    anchor_dataset: days
    on: date
""")


def test_two_fact_tables_importing_one_common_model_conform_on_it(models):
    """The shape the whole design is for: two fact tables related to the same
    common model and to nothing else, groupable on its dimensions."""
    overview = models["commercial_overview"]
    assert [p.name for p in overview.parts] == ["orders", "spend", "subs"]
    # all three import `calendar`, so its dimensions survive the intersection
    # with nothing declared at the model level at all
    assert "calendar_date" in overview.dimensions
    assert "calendar_quarter" in overview.dimensions
    # ...as does `region`, which all three declare natively
    assert "region" in overview.dimensions
    # channel is on orders and spend but not subs, so it is not offered up
    # front — a query reading only those two still gets it, see
    # test_the_catalog_is_the_intersection_of_the_parts_a_query_reads
    assert "channel" not in overview.dimensions
    assert "plan" not in overview.dimensions
    assert "category" not in overview.dimensions


# ── querying ──────────────────────────────────────────────────────

def _by_date(result: dict, measure: str) -> dict:
    return {row["calendar_date"]: row[measure] for row in result["rows"]}


def test_each_fact_tables_measure_matches_what_it_returns_alone(models):
    """The whole point: putting three unrelated fact tables on one axis must
    not change any of their numbers."""
    overview = models["commercial_overview"]
    together = engine.run_query(overview, {
        "dimensions": [{"name": "calendar_date", "grain": "1mo"}],
        "measures": ["ad_spend", "revenue", "active_customers"],
        "limit": 500,
    })

    alone = {
        "ad_spend": ("marketing", "month", "spend"),
        "revenue": ("sales", "order_date", "revenue"),
        "active_customers": ("subscriptions", "calendar_date", "active_customers"),
    }
    for name, (fact, axis, own) in alone.items():
        solo = engine.run_query(models[fact], {
            "dimensions": [{"name": axis, "grain": "1mo"}], "measures": [own], "limit": 500})
        solo_by_date = {row[axis]: row[own] for row in solo["rows"]}
        merged = _by_date(together, name)
        for bucket, value in solo_by_date.items():
            assert merged[bucket] == pytest.approx(value), f"{name} @ {bucket}"


def test_the_merged_axis_is_the_union_of_the_buckets(models):
    """A bucket only one fact table has rows for keeps its row; the others read
    null rather than a zero nobody measured."""
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": [{"name": "calendar_date", "grain": "1mo"}],
        "measures": ["ad_spend", "active_customers"],
        "limit": 500,
    })
    dates = [row["calendar_date"] for row in result["rows"]]
    assert dates == sorted(dates)              # time ascending by default
    assert len(dates) == len(set(dates))       # one row per bucket, not a cross product
    assert any(row["ad_spend"] is not None for row in result["rows"])
    assert any(row["active_customers"] is not None for row in result["rows"])


def test_measures_do_not_inflate_when_a_second_fact_table_joins_the_query(models):
    """A fact-to-fact join would multiply each side by the other's row count.
    Asking for one measure or three has to give the same numbers."""
    overview = models["commercial_overview"]
    query = {"dimensions": [{"name": "calendar_date", "grain": "1q"}, "region"], "limit": 500}
    one = engine.run_query(overview, {**query, "measures": ["revenue"]})
    three = engine.run_query(overview, {
        **query, "measures": ["revenue", "ad_spend", "active_customers"]})

    keyed = lambda r, m: {(row["calendar_date"], row["region"]): row[m] for row in r["rows"]}
    solo, joint = keyed(one, "revenue"), keyed(three, "revenue")
    assert solo  # the query returns something to compare in the first place
    for key, value in solo.items():
        assert joint[key] == pytest.approx(value)


def test_a_filter_on_a_shared_dimension_reaches_every_fact_table(models):
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": ["region"],
        "measures": ["revenue", "ad_spend"],
        "filters": [{"field": "region", "op": "eq", "value": "Euro-Zone"}],
        "limit": 100,
    })
    assert [row["region"] for row in result["rows"]] == ["Euro-Zone"]
    assert result["rows"][0]["revenue"] > 0
    assert result["rows"][0]["ad_spend"] > 0


def test_grouping_by_a_dimension_a_read_fact_table_lacks_is_refused(models):
    with pytest.raises(engine.QueryError, match="'channel' is not shared by the fact tables"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["channel"], "measures": ["revenue", "mrr"]})


def test_the_refusal_names_the_fact_table_that_lacks_the_dimension(models):
    """Which measure to drop is the actionable half of the message."""
    with pytest.raises(engine.QueryError, match="'subs' doesn't offer it"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["channel"], "measures": ["revenue", "mrr"]})


def test_filtering_on_a_dimension_a_read_fact_table_lacks_is_refused(models):
    with pytest.raises(engine.QueryError, match="'plan' is not shared by the fact tables"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["region"], "measures": ["revenue", "ad_spend"],
            "filters": [{"field": "plan", "op": "eq", "value": "pro"}]})


# ── the catalog follows the query, not the model ──────────────────

def test_the_catalog_is_the_intersection_of_the_parts_a_query_reads(models):
    """`channel` is on orders and spend but not subs. A query that measures
    only the first two never reads a subscriptions row, so there is nothing
    dishonest about a channel axis — and no reason to make the user declare a
    second model for the pair."""
    overview = models["commercial_overview"]
    assert "channel" not in overview.dimensions          # not offered up front
    result = engine.run_query(overview, {
        "dimensions": ["channel"], "measures": ["revenue", "ad_spend"], "limit": 100})
    assert result["rows"]
    for row in result["rows"]:
        assert row["channel"] is not None
    assert [c["name"] for c in result["columns"]] == ["channel", "revenue", "ad_spend"]


def test_adding_a_third_fact_tables_measure_withdraws_the_dimension(models):
    overview = models["commercial_overview"]
    query = {"dimensions": ["channel"], "limit": 100}
    engine.run_query(overview, {**query, "measures": ["revenue", "ad_spend"]})
    with pytest.raises(engine.QueryError, match="not shared by the fact tables this query reads"):
        engine.run_query(overview, {**query, "measures": ["revenue", "ad_spend", "mrr"]})


def test_one_fact_table_alone_offers_everything_that_table_offers(models):
    """A single-table query conforms with itself, so it can be grouped by
    anything that table has — `category` is orders-only."""
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": ["category"], "measures": ["revenue"], "limit": 10})
    assert result["rows"] and result["rows"][0]["category"] is not None


def test_an_unknown_measure_is_refused(models):
    with pytest.raises(engine.QueryError, match="unknown measure 'nope'"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["region"], "measures": ["nope"]})


def test_grand_totals_need_no_dimension_at_all(models):
    result = engine.run_query(models["commercial_overview"], {
        "measures": ["revenue", "ad_spend"]})
    assert result["row_count"] == 1
    row = result["rows"][0]
    assert row["revenue"] > 0 and row["ad_spend"] > 0


def test_only_the_fact_tables_a_query_asks_for_are_read(models):
    """A query naming no subscriptions measure must not pull the interval join
    in — the result would still be right, but it would cost a scan of a table
    nothing on the chart came from."""
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": [{"name": "calendar_date", "grain": "1mo"}], "measures": ["revenue"]})
    assert set(result["rows"][0]) == {"calendar_date", "revenue"}
    assert [c["name"] for c in result["columns"]] == ["calendar_date", "revenue"]


def test_columns_carry_the_shared_label_and_the_owning_fact_table(models):
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": [{"name": "calendar_date", "grain": "1y"}], "measures": ["mrr"]})
    date_col, measure_col = result["columns"]
    # the axis is a real dimension with a real label, owned by the common model
    # the fact tables conform through — not a name invented here
    assert date_col == {"name": "calendar_date", "label": "Calendar Date",
                        "kind": "dimension", "type": "time"}
    assert measure_col["fact"] == "subs"
    assert measure_col["format"] == "currency"


def test_sort_and_limit_apply_after_the_merge(models):
    result = engine.run_query(models["commercial_overview"], {
        "dimensions": ["region"], "measures": ["revenue"],
        "sort": {"by": "revenue", "desc": True}, "limit": 2,
    })
    values = [row["revenue"] for row in result["rows"]]
    assert len(values) == 2 and values == sorted(values, reverse=True)


def test_inline_measures_are_refused_across_fact_tables(models):
    with pytest.raises(engine.QueryError, match="doesn't take inline"):
        engine.run_query(models["commercial_overview"], {
            "dimensions": ["region"], "measures": ["adhoc"],
            "inline_measures": [{"name": "adhoc", "expr": "COUNT(*)"}]})


def test_scanning_a_multi_table_model_says_why_it_cannot(models):
    with pytest.raises(engine.QueryError, match="no single frame to scan"):
        engine.scan(models["commercial_overview"])


def test_dimension_values_come_from_whichever_fact_table_can_supply_them(models):
    values = engine.dimension_values(models["commercial_overview"], "region")
    assert set(values) == set(engine.dimension_values(models["sales"], "region"))


# ── the form/editor surface ───────────────────────────────────────

def test_the_guided_form_opens_a_model_with_several_fact_tables(models):
    """No escape hatch needed any more: the form edits datasets and relations,
    which is all this model is."""
    spec = semantic.model_to_spec(_parse(models["commercial_overview"].origin.read_text()))
    assert [d["name"] for d in spec["datasets"]] == ["orders", "spend", "subs"]
    assert {i["from_dataset"] for i in spec["dimension_imports"]} == {"orders", "spend", "subs"}


def test_to_public_reports_the_parts_and_marks_the_kind(models):
    public = models["commercial_overview"].to_public()
    assert public["kind"] == "composite"
    assert public["path"] is None
    assert [p["name"] for p in public["parts"]] == ["orders", "spend", "subs"]
    assert "revenue" in next(p for p in public["parts"] if p["name"] == "orders")["measures"]


def test_a_single_table_model_reports_one_part(models):
    public = models["sales"].to_public()
    assert public["kind"] == "fact"
    assert [p["name"] for p in public["parts"]] == ["sales"]
    assert public["parts"][0]["datasets"] == ["sales", "products"]


def test_every_fact_tables_objects_are_attributed_to_the_model(models):
    """Unlike the old shape, a model with several fact tables reads bucket
    objects of its own — all of them."""
    matchers = semantic.model_source_matchers(list(models.values()), "cash-intel")
    roles = {role for name, role, _match in matchers if name == "commercial_overview"}
    assert roles >= {"source: orders", "source: spend", "source: subs"}
