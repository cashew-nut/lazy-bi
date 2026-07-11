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


def test_explorer_attributes_dimension_bundle_sources(client):
    # regions.csv/territories.csv back the `geography` bundle sales.yaml
    # imports — they must not show up as unmapped just because no model
    # declares them directly as a source or a plain join
    data = client.get("/api/explorer").json()
    by_key = {f["key"]: f for f in data["files"]}
    for key in ("ref/regions.csv", "ref/territories.csv"):
        hits = by_key[key]["models"]
        assert hits, f"{key} should not be unmapped"
        assert any(h["model"] == "sales" and h["role"].startswith("import:") for h in hits)


def test_dimension_bundles_list(client):
    bundles = client.get("/api/dimensions").json()
    geo = next(b for b in bundles if b["name"] == "geography")
    assert geo["file"] == "geography.yaml"
    assert {d["name"] for d in geo["datasets"]} == {"regions", "territories"}


def test_dimension_bundle_yaml_roundtrip(client):
    got = client.get("/api/dimensions/geography/yaml").json()
    assert got["file"] == "geography.yaml"
    assert "territories" in got["yaml"]

    # round-trip an unchanged save; a real edit is covered by parsing tests
    put = client.put("/api/dimensions/geography/yaml", json={"yaml": got["yaml"]})
    assert put.status_code == 200
    assert put.json()["name"] == "geography"

    # models that import this bundle must re-resolve after the reload the PUT triggers
    sales = next(m for m in client.get("/api/models").json() if m["name"] == "sales")
    assert any(d["name"] == "territory_name" for d in sales["dimensions"])


def test_dimension_bundle_reload(client):
    assert client.post("/api/dimensions/reload").json()["loaded"] == ["geography"]


def test_unknown_dimension_bundle_is_404(client):
    assert client.get("/api/dimensions/nope/yaml").status_code == 404


def test_dimension_bundle_validate(client):
    ok = client.post("/api/dimensions/validate", json={"yaml": (
        "name: probe\ndatasets:\n"
        "  - name: regions\n    source: {format: csv, path: s3://cash-intel/ref/regions.csv}\n"
        "    dimensions: [{name: region}]\n")}).json()
    assert ok["ok"]
    ds = ok["bundle"]["datasets"][0]
    assert ds["name"] == "regions"
    assert any(c["name"] == "region" for c in ds["columns"])  # source introspected

    bad = client.post("/api/dimensions/validate", json={"yaml": "name: x\ndatasets: []"}).json()
    assert not bad["ok"] and "no datasets" in bad["error"]


def test_dimension_bundle_create_and_delete(client):
    yaml = ("name: throwaway_geo\ndatasets:\n"
            "  - name: regions\n    source: {format: csv, path: s3://cash-intel/ref/regions.csv}\n"
            "    dimensions: [{name: region}]\n")
    created = client.post("/api/dimensions", json={"yaml": yaml})
    assert created.status_code == 201
    assert created.json()["name"] == "throwaway_geo"
    assert client.post("/api/dimensions", json={"yaml": yaml}).status_code == 409  # duplicate
    assert client.delete("/api/dimensions/throwaway_geo").status_code == 204
    assert client.get("/api/dimensions/throwaway_geo/yaml").status_code == 404


def test_delete_imported_bundle_refused(client):
    # geography is imported by sales + logistics — deleting it must be refused,
    # naming the importers, rather than breaking every importer on reload
    res = client.delete("/api/dimensions/geography")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "sales" in detail and "logistics" in detail
    # still present and still resolving after the refused delete
    assert client.get("/api/dimensions/geography/yaml").status_code == 200


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


# ── 007-modelling-workspace ──────────────────────────────────

def test_guided_import_roundtrip(client):
    """A model whose yaml carries a dimension_imports block (what the guided
    import affordance produces) gains the bundle's shared dimensions on load."""
    yaml_text = (
        "name: t_import_probe\n"
        "source: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
        "dimension_imports:\n"
        "  - bundle: geography\n"
        "    anchor_dataset: regions\n"
        "    on: region\n"
        "measures:\n  - name: rows\n    expr: pl.len()\n"
    )
    # validation surfaces the imported dims before any save
    ok = client.post("/api/models/validate", json={"yaml": yaml_text}).json()
    assert ok["ok"], ok
    created = client.post("/api/models", json={"yaml": yaml_text})
    assert created.status_code == 201
    try:
        model = next(m for m in client.get("/api/models").json() if m["name"] == "t_import_probe")
        dims = {d["name"] for d in model["dimensions"]}
        assert "region" in dims and "territory" in dims  # imported from geography
    finally:
        client.delete("/api/models/t_import_probe")


def test_raw_yaml_parity_and_invalid_not_persisted(client):
    """Raw-YAML editing keeps full parity: a valid PUT persists + reloads; an
    invalid PUT is rejected (400) and does not change the stored yaml."""
    base = ("name: t_parity\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
            "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: pl.len()\n")
    assert client.post("/api/models", json={"yaml": base}).status_code == 201
    try:
        good = base.replace("label: ", "") + "\n# a valid trailing comment\n"
        assert client.put("/api/models/t_parity/yaml", json={"yaml": good}).status_code == 200
        assert "valid trailing comment" in client.get("/api/models/t_parity/yaml").json()["yaml"]

        # invalid yaml (measure expr that cannot compile) must be refused + not stored
        bad = base.replace("expr: pl.len()", "expr: pl.col(")
        assert client.put("/api/models/t_parity/yaml", json={"yaml": bad}).status_code == 400
        assert "valid trailing comment" in client.get("/api/models/t_parity/yaml").json()["yaml"]
    finally:
        client.delete("/api/models/t_parity")
