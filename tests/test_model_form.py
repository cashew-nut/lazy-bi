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


def _dataset(spec, name):
    return next(d for d in spec["datasets"] if d["name"] == name)


def test_spec_yaml_round_trip_is_semantically_lossless():
    """sales.yaml is written the terse `source:`/`joins:` way; the form
    rewrites it as datasets + relations, and nothing about the model changes."""
    spec = _sales_spec()
    reparsed = semantic.parse_model_text(semantic.spec_to_yaml(spec))
    original = semantic.parse_model_text(open("models/sales.yaml").read())
    assert list(reparsed.dimensions) == list(original.dimensions)
    assert [m.expr_source for m in reparsed.measures.values()] == \
        [m.expr_source for m in original.measures.values()]
    assert list(reparsed.datasets) == list(original.datasets)
    assert [(j.name, j.left_on, j.right_on, j.how) for j in reparsed.joins] == \
        [(j.name, j.left_on, j.right_on, j.how) for j in original.joins]
    assert [(i.bundle, i.from_dataset, i.anchor_dataset, i.left_on, i.right_on)
            for i in reparsed.imports] == \
        [(i.bundle, i.from_dataset, i.anchor_dataset, i.left_on, i.right_on)
         for i in original.imports]


def test_spec_to_yaml_collapses_matching_keys_to_on():
    spec = _sales_spec()
    text = semantic.spec_to_yaml(spec)
    # sales joins products on the shared 'product' column -> terse `on:` form
    assert "on: product" in text
    assert "on: region" in text          # ...and so does its geography import
    # ...while its calendar import, whose keys differ (order_date -> date),
    # keeps the explicit pair rather than being collapsed to a wrong `on:`
    assert "left_on: order_date" in text
    assert "right_on: date" in text


def test_spec_to_yaml_emits_differing_relationship_columns():
    """The redesign's core case: relationship columns that do NOT share a name
    must survive as left_on/right_on."""
    spec = _sales_spec()
    relation = _dataset(spec, "sales")["joins"][0]
    relation["left_on"] = ["product"]
    relation["right_on"] = ["sku"]
    text = semantic.spec_to_yaml(spec)
    parsed = semantic.parse_model_text(text)
    assert parsed.datasets["sales"].joins[0].left_on == ["product"]
    assert parsed.datasets["sales"].joins[0].right_on == ["sku"]


def test_spec_preserves_spine_and_geo():
    """A hand-authored spine or geo dimension (spine now also has a guided
    creation/edit UI; geo still does not) must still round-trip untouched
    through model_to_spec/spec_to_yaml, so an unrelated form save never
    silently strips it."""
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
    # the file says `source:` + one `joins:` entry; the form sees two datasets
    # and the relation between them
    assert [d["name"] for d in spec["datasets"]] == ["sales", "products"]
    assert _dataset(spec, "sales")["source"]["path"].endswith("sales/*.parquet")
    assert _dataset(spec, "sales")["joins"][0] == {
        "to": "products", "left_on": ["product"], "right_on": ["product"], "how": "left"}
    assert _dataset(spec, "products")["source"]["path"] == "s3://cash-intel/ref/products.csv"
    imp = spec["dimension_imports"][0]
    assert (imp["bundle"], imp["from_dataset"], imp["anchor_dataset"]) == \
        ("geography", "sales", "regions")
    # native dimensions only — imported region/territory dims live in the bundle
    assert "region" not in [d["name"] for d in _dataset(spec, "sales")["dimensions"]]


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
    relation = _dataset(spec, "sales")["joins"][0]
    relation["left_on"] = []
    relation["right_on"] = []
    body = client.post("/api/models/generate", json=spec).json()
    assert body["ok"] is False
    assert "left_on" in body["error"] or "'on'" in body["error"]


def test_form_save_flow_creates_model_with_unmatched_key_names(client):
    """End-to-end backend path of the wizard: spec (differing relation column
    names) -> generate -> POST /models -> queryable -> delete."""
    spec = {
        "name": "form_smoke", "label": "Form Smoke", "description": "",
        "dimension_imports": [],
        "datasets": [{
            "name": "spend",
            "source": {"path": "s3://cash-intel/marketing/spend.parquet", "format": "parquet"},
            "joins": [],
            "dimensions": [{"name": "channel", "column": "channel", "label": "Channel",
                            "type": "categorical", "description": "", "spine": None, "geo": None}],
            "measures": [{"name": "rows", "expr": "count()", "label": "Rows",
                          "format": "number", "description": ""}],
        }],
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


def test_form_save_flow_creates_two_unrelated_fact_tables(client):
    """The shape the redesign exists for: one model, two fact tables that
    relate to the same common model and to nothing else — readable on the
    axis that common model provides, and on nothing either of them lacks."""
    dimension = lambda name: {"name": name, "column": name, "label": name.title(),
                              "type": "categorical", "description": "",
                              "spine": None, "geo": None, "synonyms": []}
    spec = {
        "name": "pair_smoke", "label": "Pair Smoke", "description": "",
        "datasets": [
            {"name": "orders",
             "source": {"path": "s3://cash-intel/sales/*.parquet", "format": "parquet"},
             "joins": [], "dimensions": [dimension("region"), dimension("channel")],
             "measures": [{"name": "order_lines", "expr": "count()", "label": "Order Lines",
                           "format": "number", "description": ""}]},
            {"name": "spend",
             "source": {"path": "s3://cash-intel/marketing/*.parquet", "format": "parquet"},
             "joins": [], "dimensions": [dimension("region")],
             "measures": [{"name": "ad_spend", "expr": "sum(spend)", "label": "Ad Spend",
                           "format": "currency", "description": ""}]},
        ],
        "dimension_imports": [
            {"bundle": "calendar", "from_dataset": "orders", "anchor_dataset": "days",
             "left_on": ["order_date"], "right_on": ["date"], "datasets": None},
            {"bundle": "calendar", "from_dataset": "spend", "anchor_dataset": "days",
             "left_on": ["month"], "right_on": ["date"], "datasets": None},
        ],
    }
    gen = client.post("/api/models/generate", json=spec).json()
    assert gen["ok"] is True, gen.get("error")
    assert gen["model"]["kind"] == "composite"
    assert [p["name"] for p in gen["parts"]] == ["orders", "spend"]
    # region is declared on both; the calendar is imported into both — so both
    # survive the conform, and channel (orders only) does not
    assert "region" in gen["shared_dimensions"]
    assert "calendar_quarter" in gen["shared_dimensions"]
    assert "channel" not in gen["shared_dimensions"]

    assert client.post("/api/models", json={"yaml": gen["yaml"]}).status_code == 201
    try:
        q = client.post("/api/query", json={
            "model": "pair_smoke", "dimensions": [{"name": "calendar_quarter"}],
            "measures": ["order_lines", "ad_spend"], "filters": [], "limit": 50,
        })
        assert q.status_code == 200, q.text
        rows = q.json()["rows"]
        assert rows and any(r["order_lines"] and r["ad_spend"] for r in rows)
    finally:
        assert client.delete("/api/models/pair_smoke").status_code == 204


def test_spec_round_trip_preserves_interval_import(client):
    """The form opens `subscriptions`, which imports the calendar on a date
    range: `how` and the [start, end] key pair must survive spec -> yaml."""
    spec = client.get("/api/models/subscriptions/spec").json()["spec"]
    imp = spec["dimension_imports"][0]
    assert imp["how"] == "between"
    assert imp["left_on"] == ["start_date", "end_date"] and imp["right_on"] == ["date"]
    body = client.post("/api/models/generate", json=spec).json()
    assert body["ok"] is True
    assert "how: between" in body["yaml"]
    assert client.post("/api/models/validate", json={"yaml": body["yaml"]}).json()["ok"] is True


def test_form_save_flow_creates_model_with_interval_import(client):
    """Backend path behind the form's "relate on a date range" mode: a model
    whose only relation to the calendar is the window each of its rows covers."""
    spec = {
        "name": "interval_smoke", "label": "Interval Smoke", "description": "",
        "datasets": [{
            "name": "subs",
            "source": {"path": "s3://cash-intel/subscriptions/*.parquet", "format": "parquet"},
            "joins": [],
            "dimensions": [{"name": "plan", "column": "plan", "label": "Plan",
                            "type": "categorical", "description": "", "spine": None, "geo": None}],
            "measures": [{"name": "actives", "expr": "count_distinct(customer_id)",
                          "label": "Actives", "format": "number", "description": ""}],
        }],
        "dimension_imports": [{
            "bundle": "calendar", "anchor_dataset": "days", "how": "between",
            "left_on": ["start_date", "end_date"], "right_on": ["date"], "datasets": None,
        }],
    }
    gen = client.post("/api/models/generate", json=spec).json()
    assert gen["ok"] is True
    # the form's column list is the post-join scan, so it sees the calendar side
    # — under the bundle's dimension names, which is how a dimension declared on
    # this model would have to address them
    assert "calendar_quarter" in [c["name"] for c in gen["columns"]]
    created = client.post("/api/models", json={"yaml": gen["yaml"]})
    assert created.status_code == 201
    try:
        q = client.post("/api/query", json={
            "model": "interval_smoke", "dimensions": [{"name": "calendar_quarter"}],
            "measures": ["actives"], "filters": [], "limit": 20,
        })
        assert q.status_code == 200
        rows = q.json()["rows"]
        assert rows and all(r["actives"] > 0 for r in rows)
    finally:
        assert client.delete("/api/models/interval_smoke").status_code == 204


def test_generate_reports_incomplete_interval_import(client):
    """A half-filled date-range relation must come back as a problem, not as a
    silently broken join."""
    spec = client.get("/api/models/subscriptions/spec").json()["spec"]
    spec["dimension_imports"][0]["left_on"] = []
    spec["dimension_imports"][0]["right_on"] = []
    body = client.post("/api/models/generate", json=spec).json()
    assert body["ok"] is False
    assert "left_on" in body["error"] or "'on'" in body["error"]


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

def test_subscriptions_spec_includes_frame(client):
    spec = client.get("/api/models/subscriptions/spec").json()["spec"]
    framed = next(m for m in _dataset(spec, "subscriptions")["measures"]
                  if m["name"] == "median_tenure_days")
    assert framed["frame"] and "group_by" in framed["frame"]
    assert framed["frame_emits"] == ["churn_month"]


def test_subscriptions_generate_round_trips_frame(client):
    """The exact form flow: GET .../spec -> POST /models/generate — must stay
    ok and keep the frame block, not silently regenerate a broken measure."""
    spec = client.get("/api/models/subscriptions/spec").json()["spec"]
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
    sales = _dataset(client.get("/api/models/sales/spec").json()["spec"], "sales")
    order_date = next(d for d in sales["dimensions"] if d["name"] == "order_date")
    assert set(order_date["synonyms"]) == {"date", "purchase date"}
    revenue = next(m for m in sales["measures"] if m["name"] == "revenue")
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
        "dimension_imports": [],
        "datasets": [{
            "name": "spend",
            "source": {"path": "s3://cash-intel/marketing/spend.parquet", "format": "parquet"},
            "joins": [],
            "dimensions": [{"name": "channel", "column": "channel", "label": "Channel",
                            "type": "categorical", "description": "", "spine": None, "geo": None}],
            "measures": [{"name": "rows", "expr": "count()", "label": "Rows",
                          "format": "number", "description": ""}],
        }],
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
    """+ MODEL opens the create chooser, whose every path lands on a guided
    form (fact model — blank or seeded from a common model — or common
    model), never straight on the yaml editor; the raw yaml escape hatch
    stays reachable separately."""
    main = client.get("/static/js/main.js").text
    assert '$("#mk-new-model").addEventListener("click", () => openCreateChooser())' in main
    modelling = client.get("/static/js/modelling.js").text
    assert "go(paths.modellingNewModel())" in modelling            # blank fact model
    assert "go(paths.modellingNewModel(), b.name)" in modelling    # seeded from a common model
    assert "go(paths.modellingNewBundle())" in modelling           # common dimension model
    router = client.get("/static/js/router.js").text
    assert 'modellingNewModel: () => "/modelling/model/new"' in router
    assert 'return hooks.openModelForm && hooks.openModelForm(isNew ? null : name);' in router
    # every model card opens the guided form — including one holding several
    # fact tables, which is just datasets the form didn't relate to each other
    assert "navigate(paths.modellingModel(m.name))" in modelling
    assert "paths.modellingModelYaml" not in modelling
    modelform_src = client.get("/static/js/modelform.js").text
    assert '$("#mf-yaml").addEventListener("click", editAsYaml)' in modelform_src   # { } yaml editing reachable from the form itself


def test_the_form_names_the_fact_tables_the_relations_produce(client):
    """The redesign's central affordance: the RELATIONS section says how many
    separate fact tables the current relations add up to, so "these two aren't
    related to each other" is a visible choice rather than a silent one."""
    modelform = client.get("/static/js/modelform.js").text
    assert "renderFactTableSummary" in modelform
    assert "separate fact tables" in modelform
    # ...and the sections themselves mirror the common-model form's shape
    assert '{ id: "datasets", label: "DATASETS" }' in modelform
    assert '{ id: "relations", label: "RELATIONS" }' in modelform


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


def test_renamed_local_bundle_delete_and_recreate_under_old_name(client):
    """Regression: same key-drift bug as the model store (see
    test_api.py::test_renamed_local_model_delete_and_recreate_under_old_name)
    — LocalBundleStore rows are keyed by the name a bundle was created under,
    and put_dimension_bundle_yaml only rewrote the row's yaml, not its key,
    on a rename. That stranded the row under its old key, so deleting the
    renamed bundle silently deleted nothing and re-creating under the
    vacated old name hit an unhandled UNIQUE constraint failure."""
    y1 = "name: bundle_probe_a\ndatasets:\n  - name: ds1\n    source: {format: csv, path: s3://cash-intel/ref/products.csv}\n"
    assert client.post("/api/dimensions", json={"yaml": y1}).status_code == 201

    y2 = y1.replace("name: bundle_probe_a", "name: bundle_probe_b")
    assert client.put("/api/dimensions/bundle_probe_a/yaml", json={"yaml": y2}).status_code == 200
    assert "bundle_probe_b" in client.get("/api/dimensions/bundle_probe_b/yaml").json()["yaml"]

    assert client.delete("/api/dimensions/bundle_probe_b").status_code == 204
    assert "bundle_probe_b" not in [b["name"] for b in client.get("/api/dimensions").json()]

    try:
        assert client.post("/api/dimensions", json={"yaml": y1}).status_code == 201
    finally:
        client.delete("/api/dimensions/bundle_probe_a")


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
    assert "navigate(paths.modellingBundle(b.name))" in modelling         # card click -> guided form
    bundleform_src = client.get("/static/js/bundleform.js").text
    assert '$("#bf-yaml").addEventListener("click", editAsYaml)' in bundleform_src   # { } yaml editing reachable from the form itself
