"""Measure lab: inline measures, yaml append, schema + save-to-model API."""
import pytest

from app import engine, semantic


# ── inline measures in the engine ────────────────────────────

def test_inline_measure_resolves(models):
    r = engine.run_query(models["sales"], {
        "dimensions": ["region"],
        "measures": ["revenue", "avg_price_probe"],
        "inline_measures": [{"name": "avg_price_probe", "expr": "AVG(unit_price)",
                             "label": "Avg Price", "format": "currency"}],
    })
    meta = next(c for c in r["columns"] if c["name"] == "avg_price_probe")
    assert meta["label"] == "Avg Price" and meta["format"] == "currency" and meta["inline"]
    assert all(row["avg_price_probe"] > 0 for row in r["rows"])


def test_inline_measure_bad_expr_is_query_error(models):
    with pytest.raises(engine.QueryError, match="probe"):
        engine.run_query(models["sales"], {
            "dimensions": [], "measures": ["probe"],
            "inline_measures": [{"name": "probe", "expr": "nope(x)"}]})


def test_inline_measure_shadows_model_measure(models):
    r = engine.run_query(models["sales"], {
        "dimensions": [], "measures": ["revenue"],
        "inline_measures": [{"name": "revenue", "expr": "COUNT(*)"}]})
    total_rows = engine.run_query(models["sales"], {"dimensions": [], "measures": ["orders"]})
    assert r["rows"][0]["revenue"] == 60_000  # row count, not currency


def test_inline_measure_requires_name_and_expr(models):
    with pytest.raises(engine.QueryError, match="name and an expr"):
        engine.run_query(models["sales"], {
            "dimensions": [], "measures": ["x"], "inline_measures": [{"name": "x"}]})


# ── yaml append ──────────────────────────────────────────────

DOC = """# header comment stays
name: t
source: {format: parquet, path: s3://b/x.parquet}

measures:
  - name: rows
    expr: COUNT(*)

dimensions:
  - name: region
"""


def test_append_into_middle_measures_block():
    out = semantic.append_measure_yaml(DOC, {"name": "avg", "expr": "AVG(v)"})
    m = semantic.parse_model_text(out)
    assert list(m.measures) == ["rows", "avg"]
    assert out.startswith("# header comment stays")          # comments preserved
    assert out.index("avg") < out.index("dimensions:")       # inserted inside the block


def test_append_when_measures_missing():
    out = semantic.append_measure_yaml("name: t\nsource: {format: parquet, path: s3://b/x.parquet}\n",
                                       {"name": "rows", "expr": "COUNT(*)"})
    assert "rows" in semantic.parse_model_text(out).measures


def test_append_quotes_awkward_exprs():
    out = semantic.append_measure_yaml(DOC, {
        "name": "tricky", "expr": 'SUM(a) FILTER (WHERE b > 0)'})
    m = semantic.parse_model_text(out)
    assert m.measures["tricky"].sql() is not None


# ── API surface ──────────────────────────────────────────────

def test_schema_endpoint(client):
    cols = {c["name"]: c["dtype"] for c in client.get("/api/models/sales/schema").json()["columns"]}
    assert cols["unit_price"] == "DOUBLE"
    assert "supplier" in cols  # join columns included


def test_query_api_accepts_inline_measures(client):
    res = client.post("/api/query", json={
        "model": "sales", "dimensions": [], "measures": ["probe"],
        "inline_measures": [{"name": "probe", "expr": "COUNT(*)"}]})
    assert res.status_code == 200
    assert res.json()["rows"][0]["probe"] == 60_000


def test_save_measure_to_model(client):
    yaml_text = ("name: lab_probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: COUNT(*)\n")
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201
    try:
        res = client.post("/api/models/lab_probe/measures", json={
            "name": "avg_price", "expr": "AVG(unit_price)",
            "label": "Avg Price", "format": "currency"})
        assert res.status_code == 201
        assert any(m["name"] == "avg_price" and m["format"] == "currency"
                   for m in res.json()["measures"])
        # duplicates and junk rejected
        assert client.post("/api/models/lab_probe/measures", json={
            "name": "avg_price", "expr": "COUNT(*)"}).status_code == 409
        assert client.post("/api/models/lab_probe/measures", json={
            "name": "Bad Name", "expr": "COUNT(*)"}).status_code == 400
        assert client.post("/api/models/lab_probe/measures", json={
            "name": "b", "expr": "nope()"}).status_code == 400
        # the saved measure actually computes
        q = client.post("/api/query", json={"model": "lab_probe", "dimensions": [], "measures": ["avg_price"]})
        assert q.status_code == 200 and q.json()["rows"][0]["avg_price"] > 0
    finally:
        client.delete("/api/models/lab_probe")


def test_save_measure_to_a_datasets_shape_model(client):
    """A model written the general way carries its measures inside a dataset
    entry rather than in one top-level block, so the lab's create/update/delete
    goes through the spec instead of the comment-preserving text surgery. The
    endpoint contract is the same either way."""
    yaml_text = (
        "name: lab_ds_probe\n"
        "datasets:\n"
        "  - name: orders\n"
        "    source: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
        "    dimensions:\n      - name: region\n"
        "    measures:\n      - name: rows\n        expr: COUNT(*)\n"
    )
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201
    try:
        res = client.post("/api/models/lab_ds_probe/measures", json={
            "name": "avg_price", "expr": "AVG(unit_price)",
            "label": "Avg Price", "format": "currency"})
        assert res.status_code == 201, res.text
        # ...and it landed on the dataset, not in a stray top-level block the
        # parser would silently ignore
        text = client.get("/api/models/lab_ds_probe/yaml").json()["yaml"]
        assert "\nmeasures:" not in text
        assert "avg_price" in text

        q = client.post("/api/query", json={
            "model": "lab_ds_probe", "dimensions": [], "measures": ["avg_price", "rows"]})
        assert q.status_code == 200 and q.json()["rows"][0]["avg_price"] > 0

        upd = client.put("/api/models/lab_ds_probe/measures/avg_price", json={
            "name": "avg_price", "expr": "AVG(unit_cost)", "label": "Avg Cost", "format": "currency"})
        assert upd.status_code == 200
        assert next(m for m in upd.json()["measures"] if m["name"] == "avg_price")["label"] == "Avg Cost"
        # the sibling measure is untouched by the rewrite
        assert {m["name"] for m in upd.json()["measures"]} == {"rows", "avg_price"}

        assert client.delete("/api/models/lab_ds_probe/measures/avg_price").status_code == 204
        after = client.get("/api/models").json()
        probe = next(m for m in after if m["name"] == "lab_ds_probe")
        assert [m["name"] for m in probe["measures"]] == ["rows"]
    finally:
        client.delete("/api/models/lab_ds_probe")


def test_the_measure_lab_declines_a_model_with_several_fact_tables(client):
    """A measure belongs to one fact table, and the lab names a model — so it
    says which datasets to choose between rather than guessing."""
    res = client.post("/api/models/commercial_overview/measures", json={
        "name": "x", "expr": "COUNT(*)"})
    assert res.status_code == 400
    assert "3 unrelated fact tables" in res.json()["detail"]
    assert "orders, spend, subs" in res.json()["detail"]


def test_the_lab_promotes_a_complex_measure_whole(client):
    """What the lab can now author, it can also save: a `from:` measure keeps
    its block, its emits, its description and its synonyms through SAVE TO
    MODEL — the fields ASK AI writes (app/measurewriter.py) are exactly the
    ones a measure is worth having, and dropping them silently on the way to
    the yaml would waste them."""
    yaml_text = ("name: lab_complex\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "dimensions:\n  - name: region\n  - name: channel\n"
                 "measures:\n  - name: rows\n    expr: COUNT(*)\n")
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201
    try:
        res = client.post("/api/models/lab_complex/measures", json={
            "name": "avg_order_value", "label": "Average Order Value", "format": "currency",
            "description": "Mean value of a whole order, not of an order line.",
            "synonyms": ["basket size"],
            "expr": "AVG(order_total)",
            "from": "SELECT {dims}, SUM(unit_price * quantity) AS order_total\n"
                    "FROM {model}\nGROUP BY {dims}, order_id",
        })
        assert res.status_code == 201, res.text
        saved = next(m for m in res.json()["measures"] if m["name"] == "avg_order_value")
        assert saved["description"].startswith("Mean value")
        assert saved["synonyms"] == ["basket size"]
        assert "{dims}" in saved["from"]
        # and it computes, grouped by a dimension the block has to carry through
        q = client.post("/api/query", json={
            "model": "lab_complex", "dimensions": ["channel"], "measures": ["avg_order_value"]})
        assert q.status_code == 200, q.text
        assert all(row["avg_order_value"] > 0 for row in q.json()["rows"])
    finally:
        client.delete("/api/models/lab_complex")
