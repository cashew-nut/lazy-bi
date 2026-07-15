"""Guided model form backend (007-modelling-workspace redesign): the
structured-spec endpoints the form drives — GET /models/{name}/spec,
POST /models/generate, GET /datasets/schema — plus the pure spec<->yaml
helpers in app.semantic. The form itself never hand-writes YAML; these
round-trips are what guarantee it cannot produce a file the loader rejects."""
from app import semantic

SALES_SPEC = None  # cached across tests via the client fixture


# ── pure helpers: spec -> yaml -> Model ─────────────────────────

def _sales_spec():
    text = open("models/sales.yaml").read()
    return semantic.model_to_spec(semantic.parse_model_text(text))


def test_spec_yaml_round_trip_is_semantically_lossless():
    spec = _sales_spec()
    reparsed = semantic.parse_model_text(semantic.spec_to_yaml(spec))
    original = semantic.parse_model_text(open("models/sales.yaml").read())
    assert list(reparsed.dimensions) == list(original.dimensions)
    assert [m.expr_source for m in reparsed.measures.values()] == \
        [m.expr_source for m in original.measures.values()]
    assert [(j.name, j.left_on, j.right_on, j.how) for j in reparsed.joins] == \
        [(j.name, j.left_on, j.right_on, j.how) for j in original.joins]
    assert [(i.bundle, i.anchor_dataset, i.left_on, i.right_on) for i in reparsed.imports] == \
        [(i.bundle, i.anchor_dataset, i.left_on, i.right_on) for i in original.imports]


def test_spec_to_yaml_collapses_matching_keys_to_on():
    spec = _sales_spec()
    text = semantic.spec_to_yaml(spec)
    # sales joins products on the shared 'product' column -> terse `on:` form
    assert "on: product" in text
    assert "left_on" not in text


def test_spec_to_yaml_emits_differing_relationship_columns():
    """The redesign's core case: relationship columns that do NOT share a name
    must survive as left_on/right_on."""
    spec = _sales_spec()
    spec["joins"][0]["left_on"] = ["product"]
    spec["joins"][0]["right_on"] = ["sku"]
    text = semantic.spec_to_yaml(spec)
    parsed = semantic.parse_model_text(text)
    assert parsed.joins[0].left_on == ["product"]
    assert parsed.joins[0].right_on == ["sku"]


def test_spec_preserves_spine_and_geo():
    """Fields the form does not surface must still round-trip untouched, so a
    form save never silently strips advanced yaml."""
    text = (
        "name: t\nsource: {format: csv, path: s3://b/x.csv}\n"
        "dimensions:\n"
        "  - name: active\n    type: time\n    spine: {start: from_c, end: to_c}\n"
        "  - name: site\n    geo: {lat: la, lon: lo}\n"
        "measures: []\n"
    )
    spec = semantic.model_to_spec(semantic.parse_model_text(text))
    reparsed = semantic.parse_model_text(semantic.spec_to_yaml(spec))
    assert reparsed.dimensions["active"].spine.start == "from_c"
    assert reparsed.dimensions["active"].spine.end == "to_c"
    assert reparsed.dimensions["site"].geo.lat == "la"


# ── API surface ─────────────────────────────────────────────────

def test_model_spec_endpoint(client):
    res = client.get("/api/models/sales/spec")
    assert res.status_code == 200
    body = res.json()
    assert body["file"] == "sales.yaml"
    spec = body["spec"]
    assert spec["source"]["path"].endswith("sales/*.parquet")
    assert spec["joins"][0] == {
        "name": "products", "path": "s3://cash-intel/ref/products.csv",
        "format": "csv", "left_on": ["product"], "right_on": ["product"], "how": "left",
    }
    imp = spec["dimension_imports"][0]
    assert (imp["bundle"], imp["anchor_dataset"]) == ("geography", "regions")
    # native dimensions only — imported region/territory dims live in the bundle
    assert "region" not in [d["name"] for d in spec["dimensions"]]


def test_generate_returns_valid_yaml_and_columns(client):
    spec = client.get("/api/models/sales/spec").json()["spec"]
    res = client.post("/api/models/generate", json=spec)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["model"]["name"] == "sales"
    cols = [c["name"] for c in body["columns"]]
    assert "unit_price" in cols and "supplier" in cols  # post-join scan columns
    # the yaml it returns is exactly what save persists — must itself validate
    check = client.post("/api/models/validate", json={"yaml": body["yaml"]})
    assert check.json()["ok"] is True


def test_generate_reports_bad_spec_with_yaml(client):
    spec = client.get("/api/models/sales/spec").json()["spec"]
    spec["dimension_imports"][0]["bundle"] = "nope"
    body = client.post("/api/models/generate", json=spec).json()
    assert body["ok"] is False
    assert "nope" in body["error"]
    assert body["yaml"]  # the document still comes back for EDIT YAML DIRECTLY


def test_generate_join_needs_relationship_columns(client):
    spec = client.get("/api/models/sales/spec").json()["spec"]
    spec["joins"][0]["left_on"] = []
    spec["joins"][0]["right_on"] = []
    body = client.post("/api/models/generate", json=spec).json()
    assert body["ok"] is False
    assert "left_on" in body["error"] or "'on'" in body["error"]


def test_form_save_flow_creates_model_with_unmatched_key_names(client):
    """End-to-end backend path of the wizard: spec (differing join column
    names) -> generate -> POST /models -> queryable -> delete."""
    spec = {
        "name": "form_smoke", "label": "Form Smoke", "description": "",
        "source": {"path": "s3://cash-intel/marketing/spend.parquet", "format": "parquet"},
        "joins": [], "dimension_imports": [],
        "dimensions": [{"name": "channel", "column": "channel", "label": "Channel",
                        "type": "categorical", "description": "", "spine": None, "geo": None}],
        "measures": [{"name": "rows", "expr": "count()", "label": "Rows",
                      "format": "number", "description": ""}],
    }
    gen = client.post("/api/models/generate", json=spec).json()
    assert gen["ok"] is True
    created = client.post("/api/models", json={"yaml": gen["yaml"]})
    assert created.status_code == 201
    try:
        q = client.post("/api/query", json={
            "model": "form_smoke", "dimensions": [{"name": "channel"}],
            "measures": ["rows"], "filters": [], "limit": 10,
        })
        assert q.status_code == 200 and q.json()["rows"]
    finally:
        assert client.delete("/api/models/form_smoke").status_code == 204


def test_dataset_schema_endpoint(client):
    res = client.get("/api/datasets/schema", params={
        "path": "s3://cash-intel/ref/products.csv", "format": "csv"})
    assert res.status_code == 200
    assert "supplier" in [c["name"] for c in res.json()["columns"]]


def test_dataset_schema_unreachable_is_400(client):
    res = client.get("/api/datasets/schema", params={
        "path": "s3://cash-intel/nope/*.parquet", "format": "parquet"})
    assert res.status_code == 400
    assert "not reachable" in res.json()["detail"]


def test_dataset_schema_bad_format_is_400(client):
    res = client.get("/api/datasets/schema", params={"path": "s3://x", "format": "xlsx"})
    assert res.status_code == 400


# ── framed measures survive the guided form (regression: MeasureSpec used to
#    drop `frame`/`frame_emits`, so opening/regenerating a model with a framed
#    measure through the form silently stripped it and the reconstituted
#    yaml then failed to compile — "the form says the model is invalid") ──

def test_clinical_ops_spec_includes_frame(client):
    spec = client.get("/api/models/clinical_ops_recruitment/spec").json()["spec"]
    framed = next(m for m in spec["measures"] if m["name"] == "median_months_to_75pct_randomised")
    assert framed["frame"] and "group_by" in framed["frame"]
    assert framed["frame_emits"] == ["event_date"]


def test_clinical_ops_generate_round_trips_frame(client):
    """The exact form flow: GET .../spec -> POST /models/generate — must stay
    ok and keep the frame block, not silently regenerate a broken measure."""
    spec = client.get("/api/models/clinical_ops_recruitment/spec").json()["spec"]
    body = client.post("/api/models/generate", json=spec).json()
    assert body["ok"] is True, body.get("error")
    assert "frame:" in body["yaml"]
    assert "frame_emits:" in body["yaml"]
    check = client.post("/api/models/validate", json={"yaml": body["yaml"]}).json()
    assert check["ok"] is True, check.get("error")


# ── synonyms survive the guided form (same regression class as frame/
#    frame_emits above: toSpec()'s measures .map() used to reconstruct a
#    plain object with an explicit field allowlist, which would silently
#    drop any field — like synonyms — it doesn't know about) ─────────────

def test_sales_spec_includes_dimension_and_measure_synonyms(client):
    spec = client.get("/api/models/sales/spec").json()["spec"]
    order_date = next(d for d in spec["dimensions"] if d["name"] == "order_date")
    assert set(order_date["synonyms"]) == {"date", "purchase date"}
    revenue = next(m for m in spec["measures"] if m["name"] == "revenue")
    assert set(revenue["synonyms"]) == {"sales", "turnover", "income"}


def test_sales_generate_round_trips_synonyms(client):
    """GET .../spec -> POST /models/generate must keep declared synonyms —
    proves the backend spec models AND modelform.js's toSpec() (mirrored
    here by posting the spec straight back) don't drop the field."""
    spec = client.get("/api/models/sales/spec").json()["spec"]
    body = client.post("/api/models/generate", json=spec).json()
    assert body["ok"] is True, body.get("error")
    assert "synonyms:" in body["yaml"]
    assert "turnover" in body["yaml"]
    check = client.post("/api/models/validate", json={"yaml": body["yaml"]}).json()
    assert check["ok"] is True, check.get("error")


def test_generate_without_synonyms_key_still_works(client):
    """A hand-built spec that predates this feature (no 'synonyms' key at
    all, like a caller that never saw the new field) must still be accepted
    — synonyms is optional, not required."""
    spec = {
        "name": "form_smoke_no_synonyms", "label": "", "description": "",
        "source": {"path": "s3://cash-intel/marketing/spend.parquet", "format": "parquet"},
        "joins": [], "dimension_imports": [],
        "dimensions": [{"name": "channel", "column": "channel", "label": "Channel",
                        "type": "categorical", "description": "", "spine": None, "geo": None}],
        "measures": [{"name": "rows", "expr": "count()", "label": "Rows",
                      "format": "number", "description": ""}],
    }
    body = client.post("/api/models/generate", json=spec).json()
    assert body["ok"] is True, body.get("error")
    assert "synonyms:" not in body["yaml"]


# ── /api/measures/check: the form's live per-row validation ─────

def test_measure_check_valid_dsl(client):
    res = client.post("/api/measures/check", json={"expr": "sum(revenue)", "columns": ["revenue"]})
    assert res.json() == {"ok": True, "error": None, "window": False}


def test_measure_check_unknown_column(client):
    res = client.post("/api/measures/check", json={"expr": "sum(nope)", "columns": ["revenue"]}).json()
    assert res["ok"] is False
    assert "nope" in res["error"]


def test_measure_check_window_expr_uses_measure_names(client):
    res = client.post("/api/measures/check", json={
        "expr": "running_total(revenue)", "columns": ["revenue"], "measure_names": ["revenue"],
    }).json()
    assert res == {"ok": True, "error": None, "window": True}
    # a raw column that isn't also a sibling measure name is rejected in window mode
    bad = client.post("/api/measures/check", json={
        "expr": "running_total(cost)", "columns": ["cost"], "measure_names": ["revenue"],
    }).json()
    assert bad["ok"] is False


def test_measure_check_frame_ok_and_bad_syntax(client):
    ok = client.post("/api/measures/check", json={
        "expr": "pl.col(\"x\").median()",
        "frame": "frame = lf.group_by(dims).agg(pl.col('x').sum())",
    }).json()
    assert ok == {"ok": True, "error": None, "window": False}
    bad = client.post("/api/measures/check", json={"expr": "x", "frame": "frame = ("}).json()
    assert bad["ok"] is False
    assert "syntax" in bad["error"]


def test_measure_check_frame_emits_without_frame(client):
    res = client.post("/api/measures/check", json={"expr": "sum(x)", "frame_emits": ["event_date"]}).json()
    assert res["ok"] is False
    assert "frame_emits" in res["error"]


def test_measure_check_framed_requires_an_expr(client):
    """A framed measure with valid frame syntax but a blank aggregation expr
    must not be reported ok — the real load path (Measure.expr() ->
    compile_expr) always requires one, even though validate_frame alone
    (an empty snippet compiles fine as a no-op) wouldn't catch it."""
    res = client.post("/api/measures/check", json={
        "expr": "", "frame": "frame = lf.group_by(dims).agg(pl.col('x').sum())",
    }).json()
    assert res["ok"] is False
    assert "expression" in res["error"]


# ── redesign IA guards (static) ─────────────────────────────────

def test_modelform_view_present(client):
    html = client.get("/").text
    assert 'id="modelform-view"' in html
    assert 'id="mf-yaml"' in html      # the raw-yaml escape hatch stays one click away
    assert client.get("/static/js/modelform.js").status_code == 200


def test_new_model_opens_the_form_not_the_editor(client):
    """+ MODEL routes (via the router) to the guided form, not straight to the
    yaml editor; the raw yaml escape hatch stays reachable separately."""
    main = client.get("/static/js/main.js").text
    assert '$("#mk-new-model").addEventListener("click", () => navigate(paths.modellingNewModel()))' in main
    router = client.get("/static/js/router.js").text
    assert 'modellingNewModel: () => "/modelling/model/new"' in router
    assert 'return hooks.openModelForm && hooks.openModelForm(isNew ? null : name);' in router
    modelling = client.get("/static/js/modelling.js").text
    assert "navigate(paths.modellingModel(m.name))" in modelling        # ✎ edit -> guided form
    assert "navigate(paths.modellingModelYaml(m.name))" in modelling    # { } yaml editing still reachable


# ── bundle form backend (guided common-model authoring) ─────────

def _geography_spec():
    text = open("dimensions/geography.yaml").read()
    return semantic.bundle_to_spec(semantic.parse_bundle_text(text))


def test_bundle_spec_yaml_round_trip():
    spec = _geography_spec()
    reparsed = semantic.parse_bundle_text(semantic.bundle_spec_to_yaml(spec))
    original = semantic.parse_bundle_text(open("dimensions/geography.yaml").read())
    assert list(reparsed.datasets) == list(original.datasets)
    regions = reparsed.datasets["regions"]
    assert (regions.joins[0].to, regions.joins[0].left_on) == ("territories", ["territory"])
    assert regions.dimensions["region"].geo.lat == "region_lat"           # geo survives
    assert reparsed.datasets["territories"].dimensions["territory_name"].column == "name"


def test_bundle_spec_to_yaml_differing_relationship_columns():
    spec = _geography_spec()
    spec["datasets"][0]["joins"][0]["right_on"] = ["terr_code"]
    parsed = semantic.parse_bundle_text(semantic.bundle_spec_to_yaml(spec))
    assert parsed.datasets["regions"].joins[0].left_on == ["territory"]
    assert parsed.datasets["regions"].joins[0].right_on == ["terr_code"]


def test_bundle_spec_endpoint(client):
    res = client.get("/api/dimensions/geography/spec")
    assert res.status_code == 200
    body = res.json()
    assert body["file"] == "geography.yaml"
    ds = {d["name"]: d for d in body["spec"]["datasets"]}
    assert ds["regions"]["joins"][0] == {
        "to": "territories", "left_on": ["territory"], "right_on": ["territory"], "how": "left"}
    assert ds["territories"]["dimensions"][0]["column"] == "name"


def test_bundle_generate_returns_valid_yaml_and_columns(client):
    spec = client.get("/api/dimensions/geography/spec").json()["spec"]
    body = client.post("/api/dimensions/generate", json=spec).json()
    assert body["ok"] is True
    regions = next(d for d in body["bundle"]["datasets"] if d["name"] == "regions")
    assert "region_lat" in [c["name"] for c in regions["columns"]]
    check = client.post("/api/dimensions/validate", json={"yaml": body["yaml"]})
    assert check.json()["ok"] is True


def test_bundle_generate_reports_bad_spec_with_yaml(client):
    spec = client.get("/api/dimensions/geography/spec").json()["spec"]
    # same dimension name declared by two datasets -> load-time collision
    spec["datasets"][1]["dimensions"][0]["name"] = spec["datasets"][0]["dimensions"][0]["name"]
    body = client.post("/api/dimensions/generate", json=spec).json()
    assert body["ok"] is False
    assert "declared by both" in body["error"]
    assert body["yaml"]  # still returned for EDIT YAML DIRECTLY


def test_bundle_form_save_flow_creates_importable_bundle(client):
    """Wizard backend path: spec -> generate -> POST /dimensions -> importable
    by a fact model -> delete."""
    spec = {
        "name": "catalog", "label": "Catalog", "description": "",
        "datasets": [{
            "name": "products_ref",
            "source": {"path": "s3://cash-intel/ref/products.csv", "format": "csv"},
            "dimensions": [{"name": "supplier", "column": "supplier", "label": "Supplier",
                            "type": "categorical", "description": "", "spine": None, "geo": None}],
            "joins": [],
        }],
    }
    gen = client.post("/api/dimensions/generate", json=spec).json()
    assert gen["ok"] is True
    created = client.post("/api/dimensions", json={"yaml": gen["yaml"]})
    assert created.status_code == 201
    try:
        model_yaml = (
            "name: catalog_probe\n"
            "source: {format: parquet, path: s3://cash-intel/marketing/spend.parquet}\n"
            "dimension_imports:\n"
            "  - bundle: catalog\n    anchor_dataset: products_ref\n"
            "    left_on: channel\n    right_on: supplier\n"
            "measures:\n  - name: rows\n    expr: count()\n"
        )
        check = client.post("/api/models/validate", json={"yaml": model_yaml})
        assert check.json()["ok"] is True
    finally:
        assert client.delete("/api/dimensions/catalog").status_code == 204


def test_bundleform_view_present(client):
    html = client.get("/").text
    assert 'id="bundleform-view"' in html
    assert 'id="bf-yaml"' in html
    assert client.get("/static/js/bundleform.js").status_code == 200
    main = client.get("/static/js/main.js").text
    assert '$("#mk-new-bundle").addEventListener("click", () => navigate(paths.modellingNewBundle()))' in main
    router = client.get("/static/js/router.js").text
    assert 'modellingNewBundle: () => "/modelling/bundle/new"' in router
    assert 'return hooks.openBundleForm && hooks.openBundleForm(isNew ? null : name);' in router
    modelling = client.get("/static/js/modelling.js").text
    assert "navigate(paths.modellingBundle(b.name))" in modelling         # ✎ edit -> guided form
    assert "navigate(paths.modellingBundleYaml(b.name))" in modelling     # { } yaml editing still reachable
