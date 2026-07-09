"""API surface via TestClient (full lifespan: registry init against moto)."""


def test_health(client):
    data = client.get("/api/health").json()
    assert data["ok"] and "sales" in data["models"]


def test_models_public_shape(client):
    models = client.get("/api/models").json()
    sales = next(m for m in models if m["name"] == "sales")
    assert sales["file"] == "sales.yaml"
    assert any(d["type"] == "time" for d in sales["dimensions"])
    assert any(m["format"] == "percent" for m in sales["measures"])


def test_query_endpoint(client):
    res = client.post("/api/query", json={
        "model": "sales", "dimensions": ["category"], "measures": ["revenue", "margin_pct"]})
    assert res.status_code == 200
    body = res.json()
    assert body["row_count"] == 4
    assert {c["kind"] for c in body["columns"]} == {"dimension", "measure"}


def test_query_error_is_400(client):
    res = client.post("/api/query", json={"model": "sales", "dimensions": [], "measures": ["nope"]})
    assert res.status_code == 400


def test_unknown_model_is_404(client):
    assert client.post("/api/query", json={"model": "x", "measures": ["y"]}).status_code == 404


def test_visuals_roundtrip(client):
    created = client.post("/api/visuals", json={
        "name": "t", "model": "sales", "spec": {"query": {}, "chartType": "auto"}}).json()
    assert client.get("/api/visuals").json()
    assert client.delete(f"/api/visuals/{created['id']}").status_code == 204


def test_dashboard_publish_portal_flow(client):
    dash = client.post("/api/dashboards", json={
        "name": "flow", "items": [], "views": [{"name": "default", "filters": []}], "active_view": 0}).json()
    assert client.post("/api/publish", json={"dashboard_id": dash["id"], "folder": " a //b "}).json()["folder"] == "a/b"
    pubs = client.get("/api/portal").json()["publications"]
    assert any(p["dashboard_id"] == dash["id"] and p["folder"] == "a/b" for p in pubs)
    assert client.delete(f"/api/publish/{dash['id']}").status_code == 204
    assert client.delete(f"/api/dashboards/{dash['id']}").status_code == 204


def test_explorer_maps_files_to_models(client):
    data = client.get("/api/explorer").json()
    by_key = {f["key"]: f for f in data["files"]}
    assert any(k.startswith("sales/") for k in by_key)
    csv_hit = by_key["ref/products.csv"]["models"]
    assert any(h["model"] == "sales" and h["role"].startswith("join") for h in csv_hit)


def test_editor_validate(client):
    ok = client.post("/api/models/validate", json={"yaml": (
        "name: probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
        "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: pl.len()\n")}).json()
    assert ok["ok"] and any(c["name"] == "region" for c in ok["columns"])
    bad = client.post("/api/models/validate", json={"yaml": "name: x"}).json()
    assert not bad["ok"] and "source" in bad["error"]


def test_editor_create_and_delete_model(client, tmp_path):
    yaml_text = ("name: temp_probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: pl.len()\n")
    created = client.post("/api/models", json={"yaml": yaml_text})
    assert created.status_code == 201
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 409
    assert client.delete("/api/models/temp_probe").status_code == 204
    assert "temp_probe" not in client.get("/api/health").json()["models"]
