"""API surface via TestClient (full lifespan: registry init against moto)."""
import pytest


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


def _visual_spec_with_parameter(parameters=None, inline_measures=None):
    return {
        "query": {
            "model": "sales",
            "measures": ["revenue", "revenue_lag"],
            "inline_measures": inline_measures if inline_measures is not None else [
                {"name": "revenue_lag", "expr": "lag(revenue, param('period_list'))"}
            ],
            "parameters": parameters if parameters is not None else [
                {"name": "period_list", "values": [1, 2, 3, 4], "default": 1}
            ],
        },
        "chartType": "auto",
    }


def test_visual_with_valid_parameter_saves(client):
    created = client.post("/api/visuals", json={
        "name": "t_param", "model": "sales", "spec": _visual_spec_with_parameter()}).json()
    assert created["spec"]["query"]["parameters"][0]["name"] == "period_list"
    client.delete(f"/api/visuals/{created['id']}")


def test_visual_duplicate_parameter_name_rejected(client):
    spec = _visual_spec_with_parameter(parameters=[
        {"name": "period_list", "values": [1, 2], "default": 1},
        {"name": "period_list", "values": [3, 4], "default": 3},
    ])
    res = client.post("/api/visuals", json={"name": "t_dup", "model": "sales", "spec": spec})
    assert res.status_code == 400
    assert "duplicate" in res.json()["detail"]


def test_visual_default_not_in_values_rejected(client):
    spec = _visual_spec_with_parameter(parameters=[
        {"name": "period_list", "values": [1, 2, 3], "default": 9}
    ])
    res = client.post("/api/visuals", json={"name": "t_bad_default", "model": "sales", "spec": spec})
    assert res.status_code == 400
    assert "declared values" in res.json()["detail"]


def test_visual_measure_referencing_undeclared_parameter_rejected(client):
    spec = _visual_spec_with_parameter(parameters=[])
    res = client.post("/api/visuals", json={"name": "t_undeclared", "model": "sales", "spec": spec})
    assert res.status_code == 400
    assert "undeclared parameter" in res.json()["detail"]


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
    assert client.post("/api/dimensions/reload").json()["loaded"] == ["clinical_ops", "geography"]


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
        "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: count()\n")}).json()
    assert ok["ok"] and any(c["name"] == "region" for c in ok["columns"])
    bad = client.post("/api/models/validate", json={"yaml": "name: x"}).json()
    assert not bad["ok"] and "source" in bad["error"]


def test_editor_create_and_delete_model(client, tmp_path):
    yaml_text = ("name: temp_probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: count()\n")
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
        "measures:\n  - name: rows\n    expr: count()\n"
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
            "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: count()\n")
    assert client.post("/api/models", json={"yaml": base}).status_code == 201
    try:
        good = base.replace("label: ", "") + "\n# a valid trailing comment\n"
        assert client.put("/api/models/t_parity/yaml", json={"yaml": good}).status_code == 200
        assert "valid trailing comment" in client.get("/api/models/t_parity/yaml").json()["yaml"]

        # invalid yaml (measure expr that cannot compile) must be refused + not stored
        bad = base.replace("expr: count()", "expr: sum(")
        assert client.put("/api/models/t_parity/yaml", json={"yaml": bad}).status_code == 400
        assert "valid trailing comment" in client.get("/api/models/t_parity/yaml").json()["yaml"]
    finally:
        client.delete("/api/models/t_parity")


# ── 008-safe-measure-compilation: auth-gated model-measure authoring ───────

def _probe_model(client):
    yaml_text = ("name: auth_probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: count()\n")
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201


def test_measure_mutation_requires_auth(client):
    _probe_model(client)
    try:
        body = {"name": "probe", "expr": "sum(unit_price)"}
        # no credentials at all
        assert client.post("/api/models/auth_probe/measures", json=body).status_code == 401
        # wrong key
        assert client.post("/api/models/auth_probe/measures", json=body,
                            headers={"X-API-Key": "nope", "X-Author": "eve"}).status_code == 401
        # correct key, missing author
        assert client.post("/api/models/auth_probe/measures", json=body,
                            headers={"X-API-Key": "test-secret"}).status_code == 400
        # correct key, empty author
        assert client.post("/api/models/auth_probe/measures", json=body,
                            headers={"X-API-Key": "test-secret", "X-Author": "  "}).status_code == 400
        # PUT/DELETE are gated the same way
        assert client.put("/api/models/auth_probe/measures/rows", json={
            "name": "rows", "expr": "count()"}).status_code == 401
        assert client.delete("/api/models/auth_probe/measures/rows").status_code == 401
    finally:
        client.delete("/api/models/auth_probe")


def test_measure_authoring_success_and_provenance(client, auth_headers):
    _probe_model(client)
    try:
        create = client.post("/api/models/auth_probe/measures", json={
            "name": "avg_price", "expr": "mean(unit_price)"}, headers=auth_headers)
        assert create.status_code == 201
        history = client.get("/api/models/auth_probe/measures/avg_price/history").json()
        assert len(history) == 1
        assert history[0]["version"] == 1 and history[0]["author"] == auth_headers["X-Author"]
        assert history[0]["action"] == "create" and history[0]["expr"] == "mean(unit_price)"

        update = client.put("/api/models/auth_probe/measures/avg_price", json={
            "name": "avg_price", "expr": "mean(unit_cost)"}, headers=auth_headers)
        assert update.status_code == 200
        history = client.get("/api/models/auth_probe/measures/avg_price/history").json()
        assert [h["version"] for h in history] == [2, 1]
        assert history[0]["action"] == "update" and history[0]["expr"] == "mean(unit_cost)"

        # invalid expression on update is refused, nothing changes
        bad = client.put("/api/models/auth_probe/measures/avg_price", json={
            "name": "avg_price", "expr": "nope(unit_cost)"}, headers=auth_headers)
        assert bad.status_code == 400
        assert len(client.get("/api/models/auth_probe/measures/avg_price/history").json()) == 2

        delete = client.delete("/api/models/auth_probe/measures/avg_price", headers=auth_headers)
        assert delete.status_code == 204
        model = next(m for m in client.get("/api/models").json() if m["name"] == "auth_probe")
        assert "avg_price" not in {m["name"] for m in model["measures"]}
        history = client.get("/api/models/auth_probe/measures/avg_price/history").json()
        assert history[0]["action"] == "delete" and history[0]["version"] == 3 and history[0]["expr"] is None
    finally:
        client.delete("/api/models/auth_probe")


def test_reading_saved_measure_needs_no_auth(client, auth_headers):
    _probe_model(client)
    try:
        client.post("/api/models/auth_probe/measures", json={
            "name": "avg_price", "expr": "mean(unit_price)"}, headers=auth_headers)
        q = client.post("/api/query", json={
            "model": "auth_probe", "dimensions": [], "measures": ["avg_price"]})
        assert q.status_code == 200 and q.json()["rows"][0]["avg_price"] > 0
    finally:
        client.delete("/api/models/auth_probe")


# ── 008-safe-measure-compilation: framed-measure carve-out (US3) ──────────

def test_authenticated_frame_measure_saves_and_computes(client, auth_headers):
    """A frame-bearing measure is an authenticated-model-measure-only
    construct: it's accepted here (with provenance), but never inline
    (see test_engine.py's inline-frame-rejected tests)."""
    _probe_model(client)
    try:
        body = {
            # a framed measure's `expr` still uses the pre-existing eval
            # syntax (it aggregates the frame's own output column, which
            # isn't part of the base schema) — only the scalar DSL path
            # (no `frame`) uses the new function-call grammar.
            "name": "distinct_regions_via_frame",
            "expr": 'pl.col("n").sum()',
            "frame": 'frame = lf.group_by(dims).agg(pl.len().alias("n"))',
        }
        res = client.post("/api/models/auth_probe/measures", json=body, headers=auth_headers)
        assert res.status_code == 201
        history = client.get(
            "/api/models/auth_probe/measures/distinct_regions_via_frame/history"
        ).json()
        assert history[0]["frame"] == body["frame"]

        q = client.post("/api/query", json={
            "model": "auth_probe", "dimensions": ["region"],
            "measures": ["distinct_regions_via_frame"]})
        assert q.status_code == 200
        assert q.json()["row_count"] > 0
    finally:
        client.delete("/api/models/auth_probe")


def test_frame_measure_mutation_still_requires_auth(client):
    _probe_model(client)
    try:
        body = {"name": "probe_frame", "expr": "count()", "frame": "frame = lf"}
        assert client.post("/api/models/auth_probe/measures", json=body).status_code == 401
    finally:
        client.delete("/api/models/auth_probe")


def test_frame_emits_without_frame_rejected(client, auth_headers):
    _probe_model(client)
    try:
        body = {"name": "bad", "expr": "count()", "frame_emits": ["region"]}
        res = client.post("/api/models/auth_probe/measures", json=body, headers=auth_headers)
        assert res.status_code == 400
        assert "frame_emits" in res.json()["detail"]
    finally:
        client.delete("/api/models/auth_probe")


# ── window measures: running_total()/lag() ──────────────────────────────────

def _probe_model_with_time(client):
    yaml_text = (
        "name: window_probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
        "dimensions:\n  - name: order_date\n    type: time\n  - name: region\n"
        "measures:\n  - name: revenue\n    expr: sum(unit_price)\n"
    )
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201


def test_window_measure_saves_without_touching_the_live_source(client, auth_headers):
    """Unlike a plain measure, a window measure's validation never needs to
    scan the source (it only checks sibling measure names) — a bogus source
    path shouldn't block saving one."""
    yaml_text = (
        "name: window_probe_unreachable\nsource: {format: parquet, path: s3://nope/does/not/exist/*.parquet}\n"
        "dimensions:\n  - name: order_date\n    type: time\n"
        "measures:\n  - name: revenue\n    expr: sum(unit_price)\n"
    )
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201
    try:
        res = client.post("/api/models/window_probe_unreachable/measures", json={
            "name": "revenue_running_total", "expr": "running_total(revenue)"}, headers=auth_headers)
        assert res.status_code == 201
    finally:
        client.delete("/api/models/window_probe_unreachable")


def test_window_measure_authoring_and_query_end_to_end(client, auth_headers):
    _probe_model_with_time(client)
    try:
        create = client.post("/api/models/window_probe/measures", json={
            "name": "revenue_running_total", "expr": "running_total(revenue)"}, headers=auth_headers)
        assert create.status_code == 201
        history = client.get("/api/models/window_probe/measures/revenue_running_total/history").json()
        assert history[0]["expr"] == "running_total(revenue)"

        q = client.post("/api/query", json={
            "model": "window_probe",
            "dimensions": [{"name": "order_date", "grain": "1q"}],
            "measures": ["revenue", "revenue_running_total"],
        })
        assert q.status_code == 200
        rows = sorted(q.json()["rows"], key=lambda r: r["order_date"])
        running = 0.0
        for row in rows:
            running += row["revenue"]
            assert row["revenue_running_total"] == pytest.approx(running)
    finally:
        client.delete("/api/models/window_probe")


def test_window_measure_unknown_sibling_rejected_on_save(client, auth_headers):
    _probe_model_with_time(client)
    try:
        res = client.post("/api/models/window_probe/measures", json={
            "name": "bad", "expr": "running_total(does_not_exist)"}, headers=auth_headers)
        assert res.status_code == 400
        assert "does_not_exist" in res.json()["detail"]
    finally:
        client.delete("/api/models/window_probe")


# ── visual parameters: param() references in lag(), via /api/query ─────────

def _param_query_body(parameter_values=None):
    return {
        "model": "sales",
        "dimensions": [{"name": "order_date", "grain": "1q"}],
        "measures": ["revenue", "revenue_lag"],
        "inline_measures": [{"name": "revenue_lag", "expr": "lag(revenue, param('period_list'))"}],
        "parameters": [{"name": "period_list", "values": [1, 2, 3, 4], "default": 1}],
        "parameter_values": parameter_values or {},
    }


def test_query_parameter_resolves_default_when_no_override(client):
    res = client.post("/api/query", json=_param_query_body())
    assert res.status_code == 200
    assert res.json()["rows"]


def test_query_parameter_override_used(client):
    default_rows = client.post("/api/query", json=_param_query_body()).json()["rows"]
    overridden_rows = client.post("/api/query", json=_param_query_body({"period_list": 2})).json()["rows"]
    default_lags = sorted(r["revenue_lag"] for r in default_rows if r["revenue_lag"] is not None)
    overridden_lags = sorted(r["revenue_lag"] for r in overridden_rows if r["revenue_lag"] is not None)
    assert default_lags != overridden_lags


def test_query_parameter_value_outside_declared_list_rejected(client):
    res = client.post("/api/query", json=_param_query_body({"period_list": 99}))
    assert res.status_code == 400
    assert "not a declared value" in res.json()["detail"]


def test_query_parameter_undeclared_name_rejected(client):
    res = client.post("/api/query", json=_param_query_body({"nope": 1}))
    assert res.status_code == 400
    assert "unknown parameter" in res.json()["detail"]


def test_query_parameterized_measure_promotion_to_model_blocked(client, auth_headers):
    res = client.post("/api/models/sales/measures", json={
        "name": "revenue_lag_bad", "expr": "lag(revenue, param('period_list'))",
    }, headers=auth_headers)
    assert res.status_code == 400
    assert "parameterized measures" in res.json()["detail"]


def test_measures_check_resolves_parameter_to_default(client):
    res = client.post("/api/measures/check", json={
        "expr": "lag(revenue, param('period_list'))",
        "measure_names": ["revenue"],
        "parameters": [{"name": "period_list", "values": [1, 2, 3, 4], "default": 1}],
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["window"] is True


def test_measures_check_rejects_undeclared_parameter(client):
    res = client.post("/api/measures/check", json={
        "expr": "lag(revenue, param('nope'))",
        "measure_names": ["revenue"],
        "parameters": [{"name": "period_list", "values": [1, 2, 3, 4], "default": 1}],
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert "nope" in body["error"]
