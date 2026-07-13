"""Measure lab: inline measures, yaml append, schema + save-to-model API."""
import pytest

from app import engine, semantic


# ── inline measures in the engine ────────────────────────────

def test_inline_measure_resolves(models):
    r = engine.run_query(models["sales"], {
        "dimensions": ["region"],
        "measures": ["revenue", "avg_price_probe"],
        "inline_measures": [{"name": "avg_price_probe", "expr": "mean(unit_price)",
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
        "inline_measures": [{"name": "revenue", "expr": "count()"}]})
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
    expr: count()

dimensions:
  - name: region
"""


def test_append_into_middle_measures_block():
    out = semantic.append_measure_yaml(DOC, {"name": "avg", "expr": "mean(v)"})
    m = semantic.parse_model_text(out)
    assert list(m.measures) == ["rows", "avg"]
    assert out.startswith("# header comment stays")          # comments preserved
    assert out.index("avg") < out.index("dimensions:")       # inserted inside the block


def test_append_when_measures_missing():
    out = semantic.append_measure_yaml("name: t\nsource: {format: parquet, path: s3://b/x.parquet}\n",
                                       {"name": "rows", "expr": "count()"})
    assert "rows" in semantic.parse_model_text(out).measures


def test_append_quotes_awkward_exprs():
    out = semantic.append_measure_yaml(DOC, {
        "name": "tricky", "expr": 'sum(where(a, b > 0))'})
    m = semantic.parse_model_text(out)
    assert m.measures["tricky"].expr() is not None


# ── API surface ──────────────────────────────────────────────

def test_schema_endpoint(client):
    cols = {c["name"]: c["dtype"] for c in client.get("/api/models/sales/schema").json()["columns"]}
    assert cols["unit_price"] == "Float64"
    assert "supplier" in cols  # join columns included


def test_query_api_accepts_inline_measures(client):
    res = client.post("/api/query", json={
        "model": "sales", "dimensions": [], "measures": ["probe"],
        "inline_measures": [{"name": "probe", "expr": "count()"}]})
    assert res.status_code == 200
    assert res.json()["rows"][0]["probe"] == 60_000


def test_save_measure_to_model(client):
    yaml_text = ("name: lab_probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: count()\n")
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201
    try:
        res = client.post("/api/models/lab_probe/measures", json={
            "name": "avg_price", "expr": "mean(unit_price)",
            "label": "Avg Price", "format": "currency"})
        assert res.status_code == 201
        assert any(m["name"] == "avg_price" and m["format"] == "currency"
                   for m in res.json()["measures"])
        # duplicates and junk rejected
        assert client.post("/api/models/lab_probe/measures", json={
            "name": "avg_price", "expr": "count()"}).status_code == 409
        assert client.post("/api/models/lab_probe/measures", json={
            "name": "Bad Name", "expr": "count()"}).status_code == 400
        assert client.post("/api/models/lab_probe/measures", json={
            "name": "b", "expr": "nope()"}).status_code == 400
        # the saved measure actually computes
        q = client.post("/api/query", json={"model": "lab_probe", "dimensions": [], "measures": ["avg_price"]})
        assert q.status_code == 200 and q.json()["rows"][0]["avg_price"] > 0
    finally:
        client.delete("/api/models/lab_probe")
