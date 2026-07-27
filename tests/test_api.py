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


# ── 010-parameter-type-generalization: type-aware visual save validation ───

def test_visual_with_float_typed_parameter_saves(client):
    spec = _visual_spec_with_parameter(
        parameters=[{"name": "threshold", "type": "float", "values": [10, 50.5, 100], "default": 50.5}],
        inline_measures=[{"name": "revenue_lag", "expr": "revenue > param('threshold')"}],
    )
    created = client.post("/api/visuals", json={"name": "t_float_param", "model": "sales", "spec": spec}).json()
    assert created["spec"]["query"]["parameters"][0]["type"] == "float"
    client.delete(f"/api/visuals/{created['id']}")


def test_visual_with_string_typed_parameter_saves(client):
    spec = _visual_spec_with_parameter(
        parameters=[{"name": "region_pick", "type": "string", "values": ["east", "west"], "default": "east"}],
        inline_measures=[{"name": "revenue_lag", "expr": "coalesce(revenue, 0)"}],
    )
    created = client.post("/api/visuals", json={"name": "t_string_param", "model": "sales", "spec": spec}).json()
    assert created["spec"]["query"]["parameters"][0]["type"] == "string"
    client.delete(f"/api/visuals/{created['id']}")


def test_visual_rejects_unsupported_parameter_type(client):
    spec = _visual_spec_with_parameter(
        parameters=[{"name": "bad", "type": "date", "values": ["2026-01-01"], "default": "2026-01-01"}],
        inline_measures=[{"name": "revenue_lag", "expr": "coalesce(revenue, 0)"}],
    )
    res = client.post("/api/visuals", json={"name": "t_bad_type", "model": "sales", "spec": spec})
    assert res.status_code == 400
    assert "unsupported type" in res.json()["detail"]


def test_visual_rejects_wrong_typed_value_in_list(client):
    spec = _visual_spec_with_parameter(
        parameters=[{"name": "threshold", "type": "int", "values": [1, 2.5, 3], "default": 1}],
        inline_measures=[{"name": "revenue_lag", "expr": "coalesce(revenue, 0)"}],
    )
    res = client.post("/api/visuals", json={"name": "t_wrong_value_type", "model": "sales", "spec": spec})
    assert res.status_code == 400
    assert "does not match declared type" in res.json()["detail"]


def test_visual_rejects_wrong_typed_default(client):
    spec = _visual_spec_with_parameter(
        parameters=[{"name": "region_pick", "type": "string", "values": ["east", "west"], "default": 1}],
        inline_measures=[{"name": "revenue_lag", "expr": "coalesce(revenue, 0)"}],
    )
    res = client.post("/api/visuals", json={"name": "t_wrong_default_type", "model": "sales", "spec": spec})
    assert res.status_code == 400
    assert "default is not one of its declared values" in res.json()["detail"]


def test_visual_rejects_non_int_parameter_used_as_lag_periods(client):
    # 010-parameter-type-generalization US3: caught at visual-save time
    # (structurally, without a full compile — see lag_period_param_names),
    # not only at query time (already covered in test_measure_dsl.py/
    # test_engine.py) or live-check time (test_measures_check_rejects_*).
    spec = _visual_spec_with_parameter(
        parameters=[{"name": "label", "type": "string", "values": ["a", "b"], "default": "a"}],
        inline_measures=[{"name": "revenue_lag", "expr": "lag(revenue, param('label'))"}],
    )
    res = client.post("/api/visuals", json={"name": "t_lag_type_mismatch", "model": "sales", "spec": spec})
    assert res.status_code == 400
    assert "must be int" in res.json()["detail"]


def test_visual_rejects_float_parameter_used_as_lag_periods_even_when_whole(client):
    spec = _visual_spec_with_parameter(
        parameters=[{"name": "n", "type": "float", "values": [2.0, 3.0], "default": 2.0}],
        inline_measures=[{"name": "revenue_lag", "expr": "lag(revenue, param('n'))"}],
    )
    res = client.post("/api/visuals", json={"name": "t_lag_float_mismatch", "model": "sales", "spec": spec})
    assert res.status_code == 400
    assert "must be int" in res.json()["detail"]


def test_visual_parameter_absent_type_defaults_to_int(client):
    # exact spec-009 shape, no "type" key at all
    spec = _visual_spec_with_parameter()
    created = client.post("/api/visuals", json={"name": "t_absent_type", "model": "sales", "spec": spec}).json()
    assert "type" not in created["spec"]["query"]["parameters"][0]
    client.delete(f"/api/visuals/{created['id']}")


def test_dashboard_publish_portal_flow(client):
    dash = client.post("/api/dashboards", json={
        "name": "flow", "items": [], "views": [{"name": "default", "filters": []}], "active_view": 0}).json()
    assert client.post("/api/publish", json={"dashboard_id": dash["id"], "folder": " a //b "}).json()["folder"] == "a/b"
    pubs = client.get("/api/portal").json()["publications"]
    assert any(p["dashboard_id"] == dash["id"] and p["folder"] == "a/b" for p in pubs)
    assert client.delete(f"/api/publish/{dash['id']}").status_code == 204
    assert client.delete(f"/api/dashboards/{dash['id']}").status_code == 204


# ── dashboards + visual parameters: save/load, sharing, conflicts ──────────

def _make_param_visual(client, name, values, default, param_name="period_list"):
    spec = _visual_spec_with_parameter(
        parameters=[{"name": param_name, "values": values, "default": default}],
        inline_measures=[{"name": "revenue_lag", "expr": f"lag(revenue, param('{param_name}'))"}],
    )
    return client.post("/api/visuals", json={"name": name, "model": "sales", "spec": spec}).json()


def _make_typed_param_visual(client, name, type_name, values, default, param_name="p"):
    # a comparison works for every type (unlike lag(), which requires int),
    # so this helper is what US4's type-conflict tests use to build a
    # non-int-typed visual without tripping US3's lag()-periods type check
    if type_name == "string":
        expr = f"count(where(channel, channel == param('{param_name}')))"
    else:
        expr = f"sum(if_(unit_price > param('{param_name}'), unit_price, 0))"
    spec = _visual_spec_with_parameter(
        parameters=[{"name": param_name, "type": type_name, "values": values, "default": default}],
        inline_measures=[{"name": "flagged", "expr": expr}],
    )
    return client.post("/api/visuals", json={"name": name, "model": "sales", "spec": spec}).json()


def test_dashboard_view_saves_and_loads_parameter_selection(client):
    v = _make_param_visual(client, "v_param_view", [1, 2, 3, 4], 1)
    try:
        dash = client.post("/api/dashboards", json={
            "name": "d_param_view",
            "items": [{"visual_id": v["id"], "w": 1}],
            "views": [{"name": "default", "filters": [], "parameters": {"period_list": 3}}],
            "active_view": 0,
        }).json()
        try:
            fetched = client.get(f"/api/dashboards/{dash['id']}").json()
            assert fetched["views"][0]["parameters"] == {"period_list": 3}
        finally:
            client.delete(f"/api/dashboards/{dash['id']}")
    finally:
        client.delete(f"/api/visuals/{v['id']}")


def test_dashboard_view_predating_parameter_falls_back_to_default(client):
    v = _make_param_visual(client, "v_param_predates", [1, 2, 3, 4], 1)
    try:
        dash = client.post("/api/dashboards", json={
            "name": "d_predates",
            "items": [{"visual_id": v["id"], "w": 1}],
            "views": [{"name": "default", "filters": []}],  # no "parameters" key at all
            "active_view": 0,
        }).json()
        try:
            fetched = client.get(f"/api/dashboards/{dash['id']}").json()
            assert fetched["views"][0]["filters"] == []
        finally:
            client.delete(f"/api/dashboards/{dash['id']}")
    finally:
        client.delete(f"/api/visuals/{v['id']}")


def test_dashboard_allows_two_visuals_with_identical_parameter(client):
    v1 = _make_param_visual(client, "v_shared_a", [1, 2, 3, 4], 1)
    v2 = _make_param_visual(client, "v_shared_b", [1, 2, 3, 4], 1)
    try:
        res = client.post("/api/dashboards", json={
            "name": "d_shared",
            "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
            "views": [{"name": "default", "filters": [], "parameters": {"period_list": 2}}],
            "active_view": 0,
        })
        assert res.status_code == 201
        client.delete(f"/api/dashboards/{res.json()['id']}")
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


def test_dashboard_absent_type_and_explicit_int_type_are_the_same_parameter(client):
    # 010-parameter-type-generalization US2: an untyped (spec-009-era)
    # parameter and an explicitly int-typed one must be treated as
    # identical for sharing purposes — proves "absent type == int" holds
    # all the way through dashboard definition-equality, not just
    # declaration validation.
    v1 = _make_param_visual(client, "v_untyped", [1, 2, 3, 4], 1)  # no "type" key at all
    spec = _visual_spec_with_parameter(
        parameters=[{"name": "period_list", "type": "int", "values": [1, 2, 3, 4], "default": 1}],
        inline_measures=[{"name": "revenue_lag", "expr": "lag(revenue, param('period_list'))"}],
    )
    v2 = client.post("/api/visuals", json={"name": "v_explicit_int", "model": "sales", "spec": spec}).json()
    try:
        res = client.post("/api/dashboards", json={
            "name": "d_untyped_vs_explicit_int",
            "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
            "views": [{"name": "default", "filters": [], "parameters": {"period_list": 3}}],
            "active_view": 0,
        })
        assert res.status_code == 201
        client.delete(f"/api/dashboards/{res.json()['id']}")
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


def test_dashboard_rejects_two_visuals_with_conflicting_values(client):
    v1 = _make_param_visual(client, "v_conflict_a", [1, 2, 3, 4], 1)
    v2 = _make_param_visual(client, "v_conflict_b", [1, 2, 3], 1)
    try:
        res = client.post("/api/dashboards", json={
            "name": "d_conflict",
            "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
            "views": [{"name": "default", "filters": []}],
            "active_view": 0,
        })
        assert res.status_code == 400
        assert "period_list" in res.json()["detail"]
        assert "v_conflict_a" in res.json()["detail"] and "v_conflict_b" in res.json()["detail"]
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


def test_dashboard_rejects_two_visuals_with_conflicting_default(client):
    v1 = _make_param_visual(client, "v_conflict_default_a", [1, 2, 3, 4], 1)
    v2 = _make_param_visual(client, "v_conflict_default_b", [1, 2, 3, 4], 2)
    try:
        res = client.post("/api/dashboards", json={
            "name": "d_conflict_default",
            "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
            "views": [{"name": "default", "filters": []}],
            "active_view": 0,
        })
        assert res.status_code == 400
        assert "period_list" in res.json()["detail"]
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


def test_dashboard_rejects_same_name_different_type(client):
    # 010-parameter-type-generalization US4: a type mismatch alone is
    # sufficient to conflict, even with values that "look" similar
    v1 = _make_typed_param_visual(client, "v_type_conflict_int", "int", [1, 2, 3], 1)
    v2 = _make_typed_param_visual(client, "v_type_conflict_string", "string", ["1", "2", "3"], "1")
    try:
        res = client.post("/api/dashboards", json={
            "name": "d_type_conflict",
            "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
            "views": [{"name": "default", "filters": []}],
            "active_view": 0,
        })
        assert res.status_code == 400
        assert "'p'" in res.json()["detail"]
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


def test_dashboard_allows_two_visuals_with_identical_float_parameter(client):
    v1 = _make_typed_param_visual(client, "v_float_share_a", "float", [10, 50.5, 100], 50.5)
    v2 = _make_typed_param_visual(client, "v_float_share_b", "float", [10, 50.5, 100], 50.5)
    try:
        res = client.post("/api/dashboards", json={
            "name": "d_float_share",
            "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
            "views": [{"name": "default", "filters": []}],
            "active_view": 0,
        })
        assert res.status_code == 201
        client.delete(f"/api/dashboards/{res.json()['id']}")
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


def test_dashboard_allows_two_visuals_with_identical_string_parameter(client):
    v1 = _make_typed_param_visual(client, "v_string_share_a", "string", ["EU", "US"], "EU")
    v2 = _make_typed_param_visual(client, "v_string_share_b", "string", ["EU", "US"], "EU")
    try:
        res = client.post("/api/dashboards", json={
            "name": "d_string_share",
            "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
            "views": [{"name": "default", "filters": []}],
            "active_view": 0,
        })
        assert res.status_code == 201
        client.delete(f"/api/dashboards/{res.json()['id']}")
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


def test_dashboard_conflict_resolved_by_renaming_parameter(client):
    v1 = _make_param_visual(client, "v_rename_a", [1, 2, 3, 4], 1)
    v2 = _make_param_visual(client, "v_rename_b", [1, 2, 3], 1, param_name="other_period_list")
    try:
        res = client.post("/api/dashboards", json={
            "name": "d_renamed",
            "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
            "views": [{"name": "default", "filters": []}],
            "active_view": 0,
        })
        assert res.status_code == 201
        client.delete(f"/api/dashboards/{res.json()['id']}")
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


def test_dashboard_update_also_enforces_conflict_check(client):
    v1 = _make_param_visual(client, "v_update_conflict_a", [1, 2, 3, 4], 1)
    v2 = _make_param_visual(client, "v_update_conflict_b", [9, 8], 9)
    try:
        dash = client.post("/api/dashboards", json={
            "name": "d_update", "items": [{"visual_id": v1["id"], "w": 1}],
            "views": [{"name": "default", "filters": []}], "active_view": 0,
        }).json()
        try:
            res = client.put(f"/api/dashboards/{dash['id']}", json={
                "name": "d_update",
                "items": [{"visual_id": v1["id"], "w": 1}, {"visual_id": v2["id"], "w": 1}],
                "views": [{"name": "default", "filters": []}],
                "active_view": 0,
            })
            assert res.status_code == 400
        finally:
            client.delete(f"/api/dashboards/{dash['id']}")
    finally:
        client.delete(f"/api/visuals/{v1['id']}")
        client.delete(f"/api/visuals/{v2['id']}")


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
    assert geo["locked"] is True
    assert {d["name"] for d in geo["datasets"]} == {"regions", "territories"}


def test_dimension_bundle_yaml_roundtrip(client):
    got = client.get("/api/dimensions/geography/yaml").json()
    assert got["file"] == "geography.yaml"
    assert "territories" in got["yaml"]


def test_locked_dimension_bundles_reject_structural_changes(client):
    """calendar/geography are the built-in common models the demo catalog's
    other locked models are wired against — editable through the app, they
    can drift out from under whatever imports them (which is exactly what
    happened: a live edit silently detached the shipped Calendar bundle from
    the models built on it). Structural edits are refused the same way a
    locked Model's are, even for an admin; the catalog only changes via a
    code change."""
    yaml_text = client.get("/api/dimensions/geography/yaml").json()["yaml"]
    assert client.put("/api/dimensions/geography/yaml", json={"yaml": yaml_text}).status_code == 403
    assert client.delete("/api/dimensions/geography").status_code == 403
    # still present and still resolving after the refused edits
    assert client.get("/api/dimensions/geography/yaml").status_code == 200
    sales = next(m for m in client.get("/api/models").json() if m["name"] == "sales")
    assert any(d["name"] == "territory_name" for d in sales["dimensions"])


def test_local_dimension_bundle_created_via_api_is_unlocked_and_editable(client):
    from app import config

    yaml = ("name: local_bundle_probe\ndatasets:\n"
            "  - name: regions\n    source: {format: csv, path: s3://cash-intel/ref/regions.csv}\n"
            "    dimensions: [{name: region}]\n")
    created = client.post("/api/dimensions", json={"yaml": yaml})
    assert created.status_code == 201
    assert created.json()["locked"] is False
    try:
        assert not (config.DIMENSIONS_DIR / "local_bundle_probe.yaml").exists()
        bundle = next(b for b in client.get("/api/dimensions").json() if b["name"] == "local_bundle_probe")
        assert bundle["locked"] is False
        assert bundle["file"] is None

        # unlocked — structural edits succeed here, unlike on a built-in bundle
        good = yaml + "\n# a comment\n"
        assert client.put("/api/dimensions/local_bundle_probe/yaml", json={"yaml": good}).status_code == 200
        assert "a comment" in client.get("/api/dimensions/local_bundle_probe/yaml").json()["yaml"]
    finally:
        assert client.delete("/api/dimensions/local_bundle_probe").status_code == 204
    assert client.get("/api/dimensions/local_bundle_probe/yaml").status_code == 404


def test_dimension_bundle_reload(client):
    assert client.post("/api/dimensions/reload").json()["loaded"] == [
        "calendar", "geography",
    ]


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
    assert created.json()["locked"] is False
    assert client.post("/api/dimensions", json={"yaml": yaml}).status_code == 409  # duplicate
    assert client.delete("/api/dimensions/throwaway_geo").status_code == 204
    assert client.get("/api/dimensions/throwaway_geo/yaml").status_code == 404


def test_delete_imported_bundle_refused(client):
    # a bundle imported by a model must be refused (naming the importer)
    # rather than breaking that model on reload — checked against a fresh,
    # unlocked bundle since a *locked* bundle is refused for that reason
    # first (see test_locked_dimension_bundles_reject_structural_changes)
    bundle_yaml = ("name: throwaway_geo2\ndatasets:\n"
                   "  - name: regions\n    source: {format: csv, path: s3://cash-intel/ref/regions.csv}\n"
                   "    dimensions: [{name: region}]\n")
    assert client.post("/api/dimensions", json={"yaml": bundle_yaml}).status_code == 201
    model_yaml = ("name: t_bundle_importer\n"
                  "source: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                  "dimension_imports:\n"
                  "  - bundle: throwaway_geo2\n"
                  "    anchor_dataset: regions\n"
                  "    on: region\n"
                  "measures:\n  - name: rows\n    expr: count()\n")
    assert client.post("/api/models", json={"yaml": model_yaml}).status_code == 201
    try:
        res = client.delete("/api/dimensions/throwaway_geo2")
        assert res.status_code == 409
        assert "t_bundle_importer" in res.json()["detail"]
        # still present and still resolving after the refused delete
        assert client.get("/api/dimensions/throwaway_geo2/yaml").status_code == 200
    finally:
        client.delete("/api/models/t_bundle_importer")
    assert client.delete("/api/dimensions/throwaway_geo2").status_code == 204


def test_local_dimension_bundle_survives_reload(client):
    yaml = ("name: bundle_reload_probe\ndatasets:\n"
            "  - name: regions\n    source: {format: csv, path: s3://cash-intel/ref/regions.csv}\n"
            "    dimensions: [{name: region}]\n")
    assert client.post("/api/dimensions", json={"yaml": yaml}).status_code == 201
    try:
        assert "bundle_reload_probe" in client.post("/api/dimensions/reload").json()["loaded"]
        assert any(b["name"] == "bundle_reload_probe" for b in client.get("/api/dimensions").json())
    finally:
        client.delete("/api/dimensions/bundle_reload_probe")


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


# ── locked (built-in) vs local models — app/localmodelstore.py ─────────────

def test_locked_models_reject_structural_changes(client):
    """The 7 built-in demo models can't be replaced or deleted through the
    app, even by an admin — the catalog itself only changes via a code
    change. Measure-lab edits on a locked model are still allowed (tested
    separately below) — the lock is structural only."""
    yaml_text = client.get("/api/models/logistics/yaml").json()["yaml"]
    assert client.put("/api/models/logistics/yaml", json={"yaml": yaml_text}).status_code == 403
    assert client.delete("/api/models/logistics").status_code == 403


def test_locked_model_still_accepts_measure_lab_edits(client):
    created = client.post("/api/models/logistics/measures",
                           json={"name": "temp_measure", "expr": "count()"})
    assert created.status_code == 201
    try:
        updated = client.put("/api/models/logistics/measures/temp_measure",
                              json={"name": "temp_measure", "expr": "count()", "label": "Temp"})
        assert updated.status_code == 200
    finally:
        assert client.delete("/api/models/logistics/measures/temp_measure").status_code == 204


def test_local_model_created_via_api_is_unlocked_and_not_a_file(client):
    from app import config

    yaml_text = ("name: local_probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: count()\n")
    created = client.post("/api/models", json={"yaml": yaml_text})
    assert created.status_code == 201
    assert created.json()["locked"] is False
    try:
        assert not (config.MODELS_DIR / "local_probe.yaml").exists()
        model = next(m for m in client.get("/api/models").json() if m["name"] == "local_probe")
        assert model["locked"] is False
        assert model["file"] is None

        # unlocked — structural edits succeed here, unlike on a built-in model
        good = yaml_text + "\n# a comment\n"
        assert client.put("/api/models/local_probe/yaml", json={"yaml": good}).status_code == 200
        assert "a comment" in client.get("/api/models/local_probe/yaml").json()["yaml"]
    finally:
        assert client.delete("/api/models/local_probe").status_code == 204
    assert "local_probe" not in client.get("/api/health").json()["models"]


def test_local_model_survives_reload(client):
    yaml_text = ("name: local_reload_probe\nsource: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
                 "dimensions:\n  - name: region\nmeasures:\n  - name: rows\n    expr: count()\n")
    assert client.post("/api/models", json={"yaml": yaml_text}).status_code == 201
    try:
        assert "local_reload_probe" in client.post("/api/models/reload").json()["loaded"]
        assert "local_reload_probe" in client.get("/api/health").json()["models"]
    finally:
        client.delete("/api/models/local_reload_probe")


def test_orphaned_local_model_dropped_not_fatal(client):
    """A local model can go stale on its own — it imports a bundle (or facts
    a model) that a later codebase change removes, exactly what happened when
    the demo catalog was pruned and left a stray local model pointing at a
    deleted bundle. That must not crash the whole app on the next reload —
    reload_all() drops just the broken model and keeps going. Writes the row
    directly (bypassing the API's importer-check, which only guards live
    deletes, not a codebase change out from under an existing local model)."""
    from app.registry import registry

    bad_yaml = (
        "name: orphan_probe\n"
        "source: {format: parquet, path: s3://cash-intel/sales/*.parquet}\n"
        "dimension_imports:\n  - bundle: nonexistent_bundle\n    anchor_dataset: x\n    on: y\n"
        "measures:\n  - name: rows\n    expr: count()\n"
    )
    registry.local_model_store.create("orphan_probe", bad_yaml)
    try:
        registry.reload_all()  # must not raise
        assert "orphan_probe" not in registry.models
        assert "sales" in registry.models  # everything else still loads
    finally:
        registry.local_model_store.delete("orphan_probe")
        registry.reload_all()


# ── local dataset upload — app/api/datasets.py ─────────────────────────────

def test_local_dataset_upload_appears_in_picker_and_delete_removes_it(client):
    from app import config

    csv_bytes = b"a,b\n1,2\n3,4\n"
    res = client.post("/api/datasets/local", data={"name": "my_upload"},
                       files=[("files", ("probe.csv", csv_bytes, "text/csv"))])
    assert res.status_code == 201
    body = res.json()
    assert body["format"] == "csv"
    assert body["path"] == "s3://cash-intel/local/my_upload/*.csv"
    assert {c["name"] for c in body["columns"]} == {"a", "b"}
    assert body["uploaded"] == [{"key": "local/my_upload/probe.csv",
                                  "path": "s3://cash-intel/local/my_upload/probe.csv", "format": "csv"}]
    assert body["skipped"] == []

    datasets = client.get("/api/datasets").json()["datasets"]
    keys = {o["key"] for ds in datasets for o in ds["objects"]}
    assert "local/my_upload/probe.csv" in keys

    # also cached on disk (outside the ephemeral bucket) so it survives a restart
    cache_file = config.LOCAL_DATA_DIR / "my_upload" / "probe.csv"
    assert cache_file.read_bytes() == csv_bytes

    assert client.delete("/api/datasets/local/my_upload").status_code == 204
    assert client.delete("/api/datasets/local/my_upload").status_code == 404

    datasets_after = client.get("/api/datasets").json()["datasets"]
    keys_after = {o["key"] for ds in datasets_after for o in ds["objects"]}
    assert "local/my_upload/probe.csv" not in keys_after
    assert not cache_file.exists()


def test_local_dataset_upload_rejects_bad_format(client):
    res = client.post("/api/datasets/local", data={"name": "bad_upload"},
                       files=[("files", ("probe.txt", b"hello", "text/plain"))])
    assert res.status_code == 400


def test_local_dataset_upload_rejects_unsafe_name(client):
    res = client.post("/api/datasets/local", data={"name": "../etc"},
                       files=[("files", ("probe.csv", b"a,b\n1,2\n", "text/csv"))])
    assert res.status_code == 400


def test_local_dataset_bulk_upload_multiple_files(client):
    """Several files uploaded under one name land under the same prefix and
    the representative path globs across all of them (one dataset, several
    parts — the same shape a multi-year sales glob already uses)."""
    res = client.post("/api/datasets/local", data={"name": "bulk_upload"}, files=[
        ("files", ("2024.csv", b"a,b\n1,2\n", "text/csv")),
        ("files", ("2025.csv", b"a,b\n3,4\n", "text/csv")),
    ])
    assert res.status_code == 201
    body = res.json()
    assert body["path"] == "s3://cash-intel/local/bulk_upload/*.csv"
    assert {u["key"] for u in body["uploaded"]} == {
        "local/bulk_upload/2024.csv", "local/bulk_upload/2025.csv",
    }
    assert body["skipped"] == []

    datasets = client.get("/api/datasets").json()["datasets"]
    keys = {o["key"] for ds in datasets for o in ds["objects"]}
    assert {"local/bulk_upload/2024.csv", "local/bulk_upload/2025.csv"} <= keys

    client.delete("/api/datasets/local/bulk_upload")


def test_local_dataset_folder_upload_preserves_structure_and_skips_bad_files(client):
    """A folder pick sends each file's path relative to the picked folder
    (formkit.js's uploadRow strips the folder's own top segment) — nested
    structure survives under local/<name>/, and a non-csv/parquet file in
    the mix is skipped rather than failing the whole upload."""
    res = client.post("/api/datasets/local", data={"name": "folder_upload"}, files=[
        ("files", ("2024/jan.csv", b"a,b\n1,2\n", "text/csv")),
        ("files", ("2024/feb.csv", b"a,b\n3,4\n", "text/csv")),
        ("files", ("README.md", b"not a dataset", "text/markdown")),
    ])
    assert res.status_code == 201
    body = res.json()
    assert {u["key"] for u in body["uploaded"]} == {
        "local/folder_upload/2024/jan.csv", "local/folder_upload/2024/feb.csv",
    }
    assert body["skipped"] == ["README.md"]
    # both files sit in the same subdirectory (2024/), so there's still one glob
    assert body["path"] == "s3://cash-intel/local/folder_upload/2024/*.csv"

    datasets = client.get("/api/datasets").json()["datasets"]
    keys = {o["key"] for ds in datasets for o in ds["objects"]}
    assert {"local/folder_upload/2024/jan.csv", "local/folder_upload/2024/feb.csv"} <= keys
    assert "local/folder_upload/README.md" not in keys

    from app import config
    assert (config.LOCAL_DATA_DIR / "folder_upload" / "2024" / "jan.csv").exists()
    client.delete("/api/datasets/local/folder_upload")


def test_local_dataset_upload_rejects_unsafe_relpath(client):
    res = client.post("/api/datasets/local", data={"name": "traversal_upload"}, files=[
        ("files", ("../../etc/passwd.csv", b"a,b\n1,2\n", "text/csv")),
    ])
    assert res.status_code == 400
    assert "no .csv/.parquet files" in res.json()["detail"]


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


def test_measure_mutation_requires_auth(client, anon_client, viewer_client):
    _probe_model(client)
    try:
        body = {"name": "probe", "expr": "sum(unit_price)"}
        # no credentials at all
        assert anon_client.post("/api/models/auth_probe/measures", json=body).status_code == 401
        # the retired spec-008 shared-secret headers grant nothing (spec 011)
        assert anon_client.post("/api/models/auth_probe/measures", json=body,
                                headers={"X-API-Key": "test-secret", "X-Author": "eve",
                                         "X-Requested-With": "fetch"}).status_code == 401
        # authenticated but below the author role
        assert viewer_client.post("/api/models/auth_probe/measures", json=body).status_code == 403
        # PUT/DELETE are gated the same way
        assert anon_client.put("/api/models/auth_probe/measures/rows", json={
            "name": "rows", "expr": "count()"}).status_code == 401
        assert viewer_client.put("/api/models/auth_probe/measures/rows", json={
            "name": "rows", "expr": "count()"}).status_code == 403
        assert anon_client.delete("/api/models/auth_probe/measures/rows").status_code == 401
        assert viewer_client.delete("/api/models/auth_probe/measures/rows").status_code == 403
    finally:
        client.delete("/api/models/auth_probe")


def test_measure_authoring_success_and_provenance(client, author_client):
    _probe_model(client)
    try:
        create = author_client.post("/api/models/auth_probe/measures", json={
            "name": "avg_price", "expr": "mean(unit_price)"})
        assert create.status_code == 201
        history = client.get("/api/models/auth_probe/measures/avg_price/history").json()
        assert len(history) == 1
        assert history[0]["version"] == 1 and history[0]["author"] == "Author Tester"
        assert history[0]["verified"] is True and history[0]["user_id"]
        assert history[0]["action"] == "create" and history[0]["expr"] == "mean(unit_price)"

        update = author_client.put("/api/models/auth_probe/measures/avg_price", json={
            "name": "avg_price", "expr": "mean(unit_cost)"})
        assert update.status_code == 200
        history = client.get("/api/models/auth_probe/measures/avg_price/history").json()
        assert [h["version"] for h in history] == [2, 1]
        assert history[0]["action"] == "update" and history[0]["expr"] == "mean(unit_cost)"

        # invalid expression on update is refused, nothing changes
        bad = author_client.put("/api/models/auth_probe/measures/avg_price", json={
            "name": "avg_price", "expr": "nope(unit_cost)"})
        assert bad.status_code == 400
        assert len(client.get("/api/models/auth_probe/measures/avg_price/history").json()) == 2

        delete = author_client.delete("/api/models/auth_probe/measures/avg_price")
        assert delete.status_code == 204
        model = next(m for m in client.get("/api/models").json() if m["name"] == "auth_probe")
        assert "avg_price" not in {m["name"] for m in model["measures"]}
        history = client.get("/api/models/auth_probe/measures/avg_price/history").json()
        assert history[0]["action"] == "delete" and history[0]["version"] == 3 and history[0]["expr"] is None
    finally:
        client.delete("/api/models/auth_probe")


def test_saved_measure_queryable_by_any_signed_in_role(client, author_client, viewer_client, anon_client):
    _probe_model(client)
    try:
        author_client.post("/api/models/auth_probe/measures", json={
            "name": "avg_price", "expr": "mean(unit_price)"})
        q = viewer_client.post("/api/query", json={
            "model": "auth_probe", "dimensions": [], "measures": ["avg_price"]})
        assert q.status_code == 200 and q.json()["rows"][0]["avg_price"] > 0
        # but never anonymously (spec 011: no anonymous access at all)
        assert anon_client.post("/api/query", json={
            "model": "auth_probe", "dimensions": [], "measures": ["avg_price"]}).status_code == 401
    finally:
        client.delete("/api/models/auth_probe")


# ── 008-safe-measure-compilation: framed-measure carve-out (US3) ──────────

def test_authenticated_frame_measure_saves_and_computes(client):
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
        res = client.post("/api/models/auth_probe/measures", json=body)
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


def test_frame_measure_mutation_requires_admin(client, anon_client, author_client):
    """The frame: escape hatch escalates beyond author — admin only
    (spec 011, Principle VI)."""
    _probe_model(client)
    try:
        body = {"name": "probe_frame", "expr": "count()", "frame": "frame = lf"}
        assert anon_client.post("/api/models/auth_probe/measures", json=body).status_code == 401
        res = author_client.post("/api/models/auth_probe/measures", json=body)
        assert res.status_code == 403
        assert "admin" in res.json()["detail"]
    finally:
        client.delete("/api/models/auth_probe")


def test_frame_emits_without_frame_rejected(client):
    _probe_model(client)
    try:
        body = {"name": "bad", "expr": "count()", "frame_emits": ["region"]}
        res = client.post("/api/models/auth_probe/measures", json=body)
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


def test_window_measure_saves_without_touching_the_live_source(client, author_client):
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
        res = author_client.post("/api/models/window_probe_unreachable/measures", json={
            "name": "revenue_running_total", "expr": "running_total(revenue)"})
        assert res.status_code == 201
    finally:
        client.delete("/api/models/window_probe_unreachable")


def test_window_measure_authoring_and_query_end_to_end(client, author_client):
    _probe_model_with_time(client)
    try:
        create = author_client.post("/api/models/window_probe/measures", json={
            "name": "revenue_running_total", "expr": "running_total(revenue)"})
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


def test_window_measure_unknown_sibling_rejected_on_save(client, author_client):
    _probe_model_with_time(client)
    try:
        res = author_client.post("/api/models/window_probe/measures", json={
            "name": "bad", "expr": "running_total(does_not_exist)"})
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


def test_query_parameterized_measure_promotion_to_model_blocked(client):
    res = client.post("/api/models/sales/measures", json={
        "name": "revenue_lag_bad", "expr": "lag(revenue, param('period_list'))",
    })
    assert res.status_code == 400
    assert "parameterized measures" in res.json()["detail"]


# ── 010-parameter-type-generalization: /api/query with non-int parameter_values ──
# Regression coverage for a real bug the pydantic layer alone can hide:
# QueryRequest.parameter_values was still typed dict[str, int] from spec 009,
# which pydantic rejects with a 422 for a float/string value before the
# request body ever reaches engine.resolve_parameter_values — caught via
# browser walkthrough, not by tests/test_engine.py (which calls
# engine.run_query() directly, bypassing QueryRequest entirely).

def test_query_endpoint_accepts_float_parameter_value(client):
    res = client.post("/api/query", json={
        "model": "sales", "dimensions": [], "measures": ["revenue", "flagged"],
        "inline_measures": [{"name": "flagged", "expr": "sum(if_(unit_price > param('threshold'), unit_price, 0))"}],
        "parameters": [{"name": "threshold", "type": "float", "values": [10, 50.5, 100], "default": 50.5}],
        "parameter_values": {"threshold": 10},
    })
    assert res.status_code == 200
    assert res.json()["rows"]


def test_query_endpoint_accepts_string_parameter_value(client):
    res = client.post("/api/query", json={
        "model": "sales", "dimensions": [], "measures": ["revenue", "flagged"],
        "inline_measures": [{"name": "flagged", "expr": "sum(where(unit_price, region == param('region_pick')))"}],
        "parameters": [{"name": "region_pick", "type": "string", "values": ["EU", "US", "APAC"], "default": "EU"}],
        "parameter_values": {"region_pick": "US"},
    })
    assert res.status_code == 200
    assert res.json()["rows"]


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


# ── 010-parameter-type-generalization: /api/measures/check across types ────
# (verification-only — check_measure() already passed parameter_values
# unconditionally, not gated on window mode, so it needed no code change)

def test_measures_check_resolves_float_param_in_comparison(client):
    res = client.post("/api/measures/check", json={
        "expr": "revenue > param('threshold')",
        "columns": ["revenue"],
        "parameters": [{"name": "threshold", "type": "float", "values": [10, 50.5, 100], "default": 50.5}],
    })
    body = res.json()
    assert body["ok"] is True
    assert body["window"] is False


def test_measures_check_resolves_string_param_in_coalesce(client):
    res = client.post("/api/measures/check", json={
        "expr": "coalesce(revenue, param('fallback'))",
        "columns": ["revenue"],
        "parameters": [{"name": "fallback", "type": "string", "values": ["n/a", "unknown"], "default": "n/a"}],
    })
    body = res.json()
    assert body["ok"] is True
    assert body["window"] is False


# ── 010-parameter-type-generalization US3: clear type-mismatch errors ──────

def test_measures_check_rejects_string_param_as_lag_periods(client):
    res = client.post("/api/measures/check", json={
        "expr": "lag(revenue, param('label'))",
        "measure_names": ["revenue"],
        "parameters": [{"name": "label", "type": "string", "values": ["a", "b"], "default": "a"}],
    })
    body = res.json()
    assert body["ok"] is False
    assert "int-typed param" in body["error"]


def test_measures_check_rejects_float_param_as_lag_periods_even_when_whole(client):
    res = client.post("/api/measures/check", json={
        "expr": "lag(revenue, param('n'))",
        "measure_names": ["revenue"],
        "parameters": [{"name": "n", "type": "float", "values": [2.0, 3.0], "default": 2.0}],
    })
    body = res.json()
    assert body["ok"] is False
    assert "int-typed param" in body["error"]
