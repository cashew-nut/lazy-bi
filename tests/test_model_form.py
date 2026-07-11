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
        "measures": [{"name": "rows", "expr": "pl.len()", "label": "Rows",
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


# ── redesign IA guards (static) ─────────────────────────────────

def test_modelform_view_present(client):
    html = client.get("/").text
    assert 'id="modelform-view"' in html
    assert 'id="mf-yaml"' in html      # the raw-yaml escape hatch stays one click away
    assert client.get("/static/js/modelform.js").status_code == 200


def test_new_model_opens_the_form_not_the_editor(client):
    main = client.get("/static/js/main.js").text
    assert '$("#mk-new-model").addEventListener("click", () => openModelForm(null))' in main
    modelling = client.get("/static/js/modelling.js").text
    assert "openModelForm" in modelling
    assert 'openEditor("model", m.name)' in modelling  # raw yaml editing still reachable
