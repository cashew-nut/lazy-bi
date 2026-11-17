"""RELATED(): a measure reading one value off a related lookup row.

Against the seeded demo bucket: `sales` joins the products.csv lookup
(`joins:`) and imports the `geography` bundle (regions -> territories) with an
equality import; `subscriptions` imports the calendar with `how: between`."""
import pytest

from app import engine, sqlgrammar, semantic


def run(models, model, **query):
    return engine.run_query(models[model], query)


def total(models, model, expr, **query):
    r = run(models, model, measures=["probe"],
            inline_measures=[{"name": "probe", "expr": expr}], **query)
    return r


# ── the grammar: shape only, no model ────────────────────────────────────

def test_structural_compile_keeps_the_call_for_a_later_resolve():
    sql = sqlgrammar.compile_expression(
        "SUM(CASE WHEN RELATED(products.tier) = 'x' THEN quantity END)")
    assert "related(products.tier)" in sql.lower()


def test_resolver_rewrites_the_call_into_a_column():
    seen = []

    def resolve(qualifier, column):
        seen.append((qualifier, column))
        return "__related__products__tier"

    sql = sqlgrammar.compile_expression(
        "COUNT(*) FILTER (WHERE RELATED(products.tier) = 'x')", {"quantity": "INT"},
        related=resolve)
    assert seen == [("products", "tier")]
    assert "related(" not in sql.lower()
    assert "__related__products__tier" in sql


def test_a_schema_check_does_not_mistake_the_related_table_for_a_column():
    # `products` is not a fact column, and must not be reported as one
    sqlgrammar.compile_expression("COUNT(*) FILTER (WHERE RELATED(products.tier) = 'x')",
                                  {"quantity": "INT"})


@pytest.mark.parametrize("expr", [
    "SUM(RELATED(a, b))",
    "SUM(RELATED(1))",
    "SUM(RELATED(a.b.c))",
    "SUM(RELATED(DISTINCT a.b))",
    "SUM(RELATED(lower(a.b)))",
])
def test_bad_shapes_are_refused(expr):
    with pytest.raises(sqlgrammar.SqlCompileError, match="RELATED"):
        sqlgrammar.compile_expression(expr)


def test_related_alone_is_not_a_measure():
    with pytest.raises(sqlgrammar.SqlCompileError) as exc:
        sqlgrammar.compile_expression("RELATED(products.tier)")
    assert exc.value.kind == "not_aggregate"


def test_refused_in_a_window_measure():
    with pytest.raises(sqlgrammar.SqlCompileError, match="window"):
        sqlgrammar.compile_expression("SUM(RELATED(products.tier)) OVER w", window=True)


def test_refused_inside_a_from_block():
    with pytest.raises(sqlgrammar.SqlCompileError, match="from: block"):
        sqlgrammar.compile_relation(
            "SELECT RELATED(products.tier) AS t FROM __model", allowed_tables={"__model"})


def test_referenced_related():
    assert sqlgrammar.referenced_related(
        "SUM(CASE WHEN RELATED(products.tier) = 'a' THEN 1 END) "
        "+ COUNT(*) FILTER (WHERE RELATED(territory_name) = 'EMEA')"
    ) == {("products", "tier"), (None, "territory_name")}
    assert sqlgrammar.referenced_related("SUM(x)") == set()
    # and the lookup's names are not mistaken for sibling measures/columns
    assert sqlgrammar.referenced_names("SUM(RELATED(products.tier) = 'a')") == set()


# ── resolving against a model ────────────────────────────────────────────

def test_resolve_join_import_and_dimension(models):
    sales = models["sales"]
    ref = semantic.resolve_related(sales, "products", "tier")
    assert (ref.kind, ref.table, ref.target) == ("join", "products", "__related__products__tier")
    ref = semantic.resolve_related(sales, "territories", "name")
    assert (ref.kind, ref.table) == ("import", "territories")
    ref = semantic.resolve_related(sales, None, "territory_name")
    assert (ref.kind, ref.target) == ("dimension", "territory_name")
    assert ref.index >= 0           # it came from an import


def test_unknown_table_lists_the_related_ones(models):
    with pytest.raises(semantic.ModelError, match="related tables: products, regions, territories"):
        semantic.resolve_related(models["sales"], "suppliers", "name")


def test_unknown_dimension_suggests_the_qualified_form(models):
    with pytest.raises(semantic.ModelError, match=r"RELATED\(table.nope\)"):
        semantic.resolve_related(models["sales"], None, "nope")


def test_between_import_is_refused(models):
    subs = models["subscriptions"]
    with pytest.raises(semantic.ModelError, match="between"):
        semantic.resolve_related(subs, None, "calendar_date")
    with pytest.raises(semantic.ModelError, match="between"):
        semantic.resolve_related(subs, "days", "date")


def test_spine_dimension_is_refused(models):
    with pytest.raises(semantic.ModelError, match="timeline"):
        semantic.resolve_related(models["subscriptions"], None, "active_at")


def test_a_bad_reference_fails_the_model_at_load(models):
    from app import config
    bundles = semantic.load_dimension_bundles(config.DIMENSIONS_DIR)
    model = semantic.parse_model_text(
        "name: rel_probe\n"
        "source: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
        "dimension_imports:\n  - {bundle: geography, anchor_dataset: regions, on: region}\n"
        "measures:\n"
        "  - name: bad\n"
        "    expr: COUNT(*) FILTER (WHERE RELATED(nowhere.tier) = 'x')\n")
    with pytest.raises(semantic.ModelError, match="measure 'bad'.*'nowhere' is not a table"):
        semantic.resolve_model(model, bundles)


# ── the engine: what it reads ────────────────────────────────────────────

def test_join_column_in_a_case(models):
    related = total(models, "sales",
                    "SUM(CASE WHEN RELATED(products.tier) = 'military-grade' THEN quantity END)")
    filtered = run(models, "sales", measures=["units"],
                   filters=[{"field": "tier", "op": "eq", "value": "military-grade"}])
    assert related["rows"][0]["probe"] == filtered["rows"][0]["units"] > 0


def test_join_column_grouped_by_something_else(models):
    related = total(models, "sales",
                    "COUNT(*) FILTER (WHERE RELATED(products.tier) = 'street-grade')",
                    dimensions=["channel"])
    by_channel = {row["channel"]: row["probe"] for row in related["rows"]}
    for channel, count in by_channel.items():
        expected = run(models, "sales", measures=["probe"], dimensions=[],
                       inline_measures=[{"name": "probe", "expr": "COUNT(*)"}],
                       filters=[{"field": "tier", "op": "eq", "value": "street-grade"},
                                {"field": "channel", "op": "eq", "value": channel}])
        assert count == expected["rows"][0]["probe"]


def test_imported_dimension_without_grouping_by_its_bundle(models):
    """The gap RELATED() closes: an imported dimension is only in a plain
    measure's scan while the query groups or filters by its bundle. With no
    dimensions at all, RELATED(territory_name) still brings the import in."""
    related = total(models, "sales",
                    "COUNT(DISTINCT order_id) FILTER (WHERE RELATED(territory_name) = 'EMEA')")
    filtered = run(models, "sales", measures=["orders"],
                   filters=[{"field": "territory_name", "op": "eq", "value": "EMEA"}])
    assert related["rows"][0]["probe"] == filtered["rows"][0]["orders"] > 0


def test_bundle_column_no_dimension_declares(models):
    # territories.name is declared as `territory_name`; reading it raw by its
    # own column name works the same, and so does a two-hop bundle dataset
    by_column = total(models, "sales",
                      "SUM(quantity) FILTER (WHERE RELATED(territories.name) = 'Pacific Rim')")
    by_code = total(models, "sales",
                    "SUM(quantity) FILTER (WHERE RELATED(regions.territory) = 'pacific-rim')")
    assert by_column["rows"][0]["probe"] == by_code["rows"][0]["probe"] > 0


def test_forcing_an_import_does_not_change_other_measures(models):
    plain = run(models, "sales", measures=["revenue", "units"])["rows"][0]
    both = run(models, "sales", measures=["revenue", "units", "probe"],
               inline_measures=[{"name": "probe",
                                 "expr": "SUM(quantity) FILTER (WHERE RELATED(territory_name) = 'EMEA')"}]
               )["rows"][0]
    assert both["revenue"] == plain["revenue"]
    assert both["units"] == plain["units"]
    assert 0 < both["probe"] < both["units"]


def test_a_window_measure_reading_a_related_sibling(models):
    r = run(models, "sales", dimensions=[{"name": "order_date", "grain": "1y"}],
            measures=["premium_running"],
            inline_measures=[
                {"name": "premium", "expr":
                    "SUM(quantity) FILTER (WHERE RELATED(products.tier) = 'military-grade')"},
                {"name": "premium_running", "expr": "SUM(premium) OVER w"},
            ])
    running = [row["premium_running"] for row in r["rows"]]
    assert running == sorted(running) and running[-1] > 0


def test_unknown_related_column_names_the_table_columns(models):
    with pytest.raises(engine.QueryError, match=r"'products' has no column 'colour'"):
        total(models, "sales", "COUNT(*) FILTER (WHERE RELATED(products.colour) = 'red')")


def test_from_measure_expr_refuses_related(models):
    with pytest.raises(engine.QueryError, match="from: block"):
        run(models, "sales", measures=["probe"], inline_measures=[{
            "name": "probe", "expr": "COUNT(*) FILTER (WHERE RELATED(products.tier) = 'x')",
            "from": "SELECT {dims}, order_id FROM {model} GROUP BY {dims}, order_id"}])


def test_decomposes_for_instant_mode(models):
    text = "SUM(CASE WHEN RELATED(products.tier) = 'street-grade' THEN quantity END)"
    plan = sqlgrammar.rollup_plan(text)
    assert plan is not None and len(plan["components"]) == 1
    # the component text still carries the call, and runs through the engine
    comp = plan["components"][0]["expr"]
    assert "related(" in comp.lower()
    assert total(models, "sales", comp)["rows"][0]["probe"] == total(models, "sales", text)["rows"][0]["probe"]


# ── saving one ───────────────────────────────────────────────────────────

def test_save_measure_with_related(client):
    yaml_text = ("name: rel_lab_probe\n"
                 "source: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "joins:\n  - name: products\n"
                 "    source: {format: csv, path: s3://cash-intel/ref/products.csv}\n"
                 "    on: product\n"
                 "dimensions:\n  - name: channel\n"
                 "measures:\n  - name: rows\n    expr: COUNT(*)\n")
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201
    try:
        res = client.post("/api/models/rel_lab_probe/measures", json={
            "name": "premium_units",
            "expr": "SUM(CASE WHEN RELATED(products.tier) = 'military-grade' THEN quantity END)"})
        assert res.status_code == 201, res.text
        bad = client.post("/api/models/rel_lab_probe/measures", json={
            "name": "bad_units", "expr": "SUM(quantity) FILTER (WHERE RELATED(products.nope) = 1)"})
        assert bad.status_code == 400 and "no column 'nope'" in bad.json()["detail"]
        bad = client.post("/api/models/rel_lab_probe/measures", json={
            "name": "bad_units", "expr": "SUM(quantity) FILTER (WHERE RELATED(regions.x) = 1)"})
        assert bad.status_code == 400 and "not a table related" in bad.json()["detail"]
        q = client.post("/api/query", json={"model": "rel_lab_probe", "dimensions": ["channel"],
                                            "measures": ["premium_units"]})
        assert q.status_code == 200 and q.json()["row_count"] > 0
    finally:
        client.delete("/api/models/rel_lab_probe")
