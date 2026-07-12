"""Semantic layer: yaml parsing and validation."""
import polars as pl
import pytest

from app import semantic

VALID = """
name: t
source: {format: parquet, path: s3://b/x/*.parquet}
dimensions:
  - name: region
  - name: day
    type: time
measures:
  - name: rows
    expr: count()
"""


def test_parse_minimal_model():
    m = semantic.parse_model_text(VALID)
    assert m.name == "t"
    assert list(m.dimensions) == ["region", "day"]
    assert m.dimensions["day"].type == "time"
    assert m.measures["rows"].expr() is not None


def test_invalid_yaml_rejected():
    with pytest.raises(semantic.ModelError):
        semantic.parse_model_text("name: [unclosed")


def test_missing_source_rejected():
    with pytest.raises(semantic.ModelError, match="source"):
        semantic.parse_model_text("name: x")


def test_bad_measure_expr_rejected():
    bad = VALID.replace("count()", "nope()")
    with pytest.raises(semantic.ModelError, match="rows"):
        semantic.parse_model_text(bad)


def test_join_on_shorthand_survives_yaml11_bool_quirk():
    # YAML 1.1 parses a bare `on:` key as boolean True
    text = VALID + """
joins:
  - name: lookup
    source: {format: csv, path: s3://b/lk.csv}
    on: region
"""
    m = semantic.parse_model_text(text)
    assert m.joins[0].left_on == ["region"]
    assert m.joins[0].right_on == ["region"]
    assert m.joins[0].how == "left"


def test_spine_requires_time_type():
    bad = VALID.replace("  - name: region", """  - name: region
  - name: active_at
    spine: {start: s, end: e}""")
    with pytest.raises(semantic.ModelError, match="type: time"):
        semantic.parse_model_text(bad)


def test_unsupported_format_rejected():
    with pytest.raises(semantic.ModelError, match="format"):
        semantic.parse_model_text(VALID.replace("parquet,", "orc,").replace("format: parquet", "format: orc"))


def test_bundled_models_load(models):
    assert {"sales", "marketing", "logistics", "subscriptions", "taxi"} <= set(models)
    sales = models["sales"]
    assert sales.joins and sales.joins[0].source.format == "csv"
    assert models["subscriptions"].dimensions["active_at"].spine is not None
    assert models["marketing"].dimensions["region"].geo is not None


# --- Framed measures (aggregations over an intermediary derived frame) -----

FRAMED = """
name: t
source: {format: parquet, path: s3://b/x/*.parquet}
dimensions:
  - name: cohort
measures:
  - name: median_days
    frame: |
      per_study = lf.group_by(["study_id", *dims]).agg(pl.col("days").min())
      frame = per_study
    expr: pl.col("days").median()
"""


def test_framed_measure_parses():
    m = semantic.parse_model_text(FRAMED)
    meas = m.measures["median_days"]
    assert "per_study" in meas.frame_source
    assert meas.expr() is not None


def test_frame_emits_parses_and_requires_frame():
    withemits = FRAMED.replace("    expr:", "    frame_emits: [event_date]\n    expr:")
    m = semantic.parse_model_text(withemits)
    assert m.measures["median_days"].frame_emits == ["event_date"]
    no_frame = VALID.replace("    expr: count()", "    frame_emits: [event_date]\n    expr: count()")
    with pytest.raises(semantic.ModelError, match="frame_emits"):
        semantic.parse_model_text(no_frame)


def test_framed_measure_bad_syntax_rejected():
    bad = FRAMED.replace("frame = per_study", "frame = = per_study")
    with pytest.raises(semantic.ModelError, match="frame syntax"):
        semantic.parse_model_text(bad)


def test_compile_frame_single_expression_form():
    lf = pl.LazyFrame({"study_id": ["a", "a", "b"], "days": [1, 3, 5]})
    out = semantic.compile_frame(
        'lf.group_by(["study_id", *dims]).agg(pl.col("days").min())', lf, [], "measure 'm'"
    )
    assert isinstance(out, pl.LazyFrame)


def test_compile_frame_statements_must_assign_frame():
    lf = pl.LazyFrame({"a": [1]})
    with pytest.raises(semantic.ModelError, match="named 'frame'"):
        semantic.compile_frame("x = lf", lf, [], "measure 'm'")


def test_compile_frame_must_produce_lazyframe():
    lf = pl.LazyFrame({"a": [1]})
    with pytest.raises(semantic.ModelError, match="LazyFrame"):
        semantic.compile_frame("frame = 42", lf, [], "measure 'm'")


def test_framed_measure_survives_spec_yaml_roundtrip():
    model = semantic.parse_model_text(FRAMED)
    text = semantic.spec_to_yaml(semantic.model_to_spec(model))
    assert "frame: |" in text  # literal block, not an escaped one-liner
    again = semantic.parse_model_text(text)
    assert again.measures["median_days"].frame_source.strip() == \
        model.measures["median_days"].frame_source.strip()


def test_append_measure_yaml_renders_frame_as_block():
    text = semantic.append_measure_yaml(VALID, {
        "name": "m2", "frame": "step = lf.filter(pl.col('x') > 0)\nframe = step",
        "expr": "pl.col('x').median()",
    })
    model = semantic.parse_model_text(text)
    assert model.measures["m2"].frame_source.strip().endswith("frame = step")


# --- Dimension bundles (common dimensional models) -------------------------

BUNDLE = """
name: geo
label: Geo
datasets:
  - name: regions
    source: {format: csv, path: s3://b/regions.csv}
    dimensions:
      - name: region
        label: Region
      - name: territory
        label: Territory Code
    joins:
      - to: territories
        on: territory
  - name: territories
    source: {format: csv, path: s3://b/territories.csv}
    dimensions:
      - name: territory_name
        column: name
        label: Territory
"""

FACT = """
name: fact
source: {format: parquet, path: s3://b/fact/*.parquet}
measures:
  - name: rows
    expr: count()
"""


def test_bundle_parses_datasets_and_internal_join():
    bundle = semantic.parse_bundle_text(BUNDLE)
    assert set(bundle.datasets) == {"regions", "territories"}
    assert bundle.datasets["regions"].joins[0].to == "territories"
    assert bundle.datasets["regions"].joins[0].left_on == ["territory"]


def test_bundle_rejects_cyclical_joins():
    # a-b alone would just collapse to one undirected edge (not a cycle) —
    # a genuine cycle needs a third dataset closing the loop: a -> b -> c -> a
    cyclic = """
name: bad
datasets:
  - {name: a, source: {format: csv, path: s3://b/a.csv}, joins: [{to: b, on: k}]}
  - {name: b, source: {format: csv, path: s3://b/b.csv}, joins: [{to: c, on: k}]}
  - {name: c, source: {format: csv, path: s3://b/c.csv}, joins: [{to: a, on: k}]}
"""
    with pytest.raises(semantic.ModelError, match="cyclical"):
        semantic.parse_bundle_text(cyclic)


def test_bundle_rejects_cross_dataset_dimension_collision():
    collide = """
name: bad
datasets:
  - {name: a, source: {format: csv, path: s3://b/a.csv}, dimensions: [{name: owner, label: Owner}]}
  - {name: b, source: {format: csv, path: s3://b/b.csv}, dimensions: [{name: owner, label: Owner}]}
"""
    with pytest.raises(semantic.ModelError, match="owner"):
        semantic.parse_bundle_text(collide)


def test_bundle_rejects_join_to_unknown_dataset():
    bad = """
name: bad
datasets:
  - {name: a, source: {format: csv, path: s3://b/a.csv}, joins: [{to: nope, on: k}]}
"""
    with pytest.raises(semantic.ModelError, match="nope"):
        semantic.parse_bundle_text(bad)


def test_bundle_rejects_empty_datasets():
    with pytest.raises(semantic.ModelError, match="no datasets"):
        semantic.parse_bundle_text("name: empty\ndatasets: []\n")


def test_import_resolves_transitively_by_default():
    bundle = semantic.parse_bundle_text(BUNDLE)
    model = semantic.parse_model_text(
        FACT + "dimension_imports:\n  - {bundle: geo, anchor_dataset: regions, on: region}\n"
    )
    semantic.resolve_imports(model, {"geo": bundle})
    assert {"region", "territory", "territory_name"} <= set(model.dimensions)


def test_import_subset_excludes_unlisted_datasets():
    bundle = semantic.parse_bundle_text(BUNDLE)
    model = semantic.parse_model_text(
        FACT + "dimension_imports:\n"
        "  - {bundle: geo, anchor_dataset: regions, on: region, datasets: [regions]}\n"
    )
    semantic.resolve_imports(model, {"geo": bundle})
    assert "territory" in model.dimensions
    assert "territory_name" not in model.dimensions


def test_import_subset_omitted_matches_explicit_whole_bundle():
    bundle = semantic.parse_bundle_text(BUNDLE)
    whole_default = semantic.parse_model_text(
        FACT + "dimension_imports:\n  - {bundle: geo, anchor_dataset: regions, on: region}\n"
    )
    whole_explicit = semantic.parse_model_text(
        FACT + "dimension_imports:\n"
        "  - {bundle: geo, anchor_dataset: regions, on: region, datasets: [regions, territories]}\n"
    )
    semantic.resolve_imports(whole_default, {"geo": bundle})
    semantic.resolve_imports(whole_explicit, {"geo": bundle})
    assert set(whole_default.dimensions) == set(whole_explicit.dimensions)


def test_import_subset_rejects_unknown_dataset():
    bundle = semantic.parse_bundle_text(BUNDLE)
    model = semantic.parse_model_text(
        FACT + "dimension_imports:\n"
        "  - {bundle: geo, anchor_dataset: regions, on: region, datasets: [nope]}\n"
    )
    with pytest.raises(semantic.ModelError, match="nope"):
        semantic.resolve_imports(model, {"geo": bundle})


def test_import_unknown_bundle_rejected():
    model = semantic.parse_model_text(
        FACT + "dimension_imports:\n  - {bundle: nope, anchor_dataset: x, on: region}\n"
    )
    with pytest.raises(semantic.ModelError, match="nope"):
        semantic.resolve_imports(model, {})


def test_import_unknown_anchor_rejected():
    bundle = semantic.parse_bundle_text(BUNDLE)
    model = semantic.parse_model_text(
        FACT + "dimension_imports:\n  - {bundle: geo, anchor_dataset: nope, on: region}\n"
    )
    with pytest.raises(semantic.ModelError, match="nope"):
        semantic.resolve_imports(model, {"geo": bundle})


def test_native_dimension_shadows_imported():
    bundle = semantic.parse_bundle_text(BUNDLE)
    model = semantic.parse_model_text(
        FACT + "dimension_imports:\n  - {bundle: geo, anchor_dataset: regions, on: region}\n"
        "dimensions:\n  - {name: region, label: My Own Region}\n"
    )
    semantic.resolve_imports(model, {"geo": bundle})
    assert model.dimensions["region"].label == "My Own Region"


def test_two_imports_with_colliding_dimension_rejected():
    bundle_a = semantic.parse_bundle_text(
        "name: a\ndatasets:\n"
        "  - {name: x, source: {format: csv, path: s3://b/x.csv}, dimensions: [{name: shared, label: Shared}]}\n"
    )
    bundle_b = semantic.parse_bundle_text(
        "name: b\ndatasets:\n"
        "  - {name: y, source: {format: csv, path: s3://b/y.csv}, dimensions: [{name: shared, label: Shared}]}\n"
    )
    model = semantic.parse_model_text(
        FACT + "dimension_imports:\n"
        "  - {bundle: a, anchor_dataset: x, on: k}\n"
        "  - {bundle: b, anchor_dataset: y, on: k}\n"
    )
    with pytest.raises(semantic.ModelError, match="shared"):
        semantic.resolve_imports(model, {"a": bundle_a, "b": bundle_b})


def test_real_geography_bundle_resolves_into_sales(models):
    # `models` fixture resolves imports against the real dimensions/*.yaml
    sales = models["sales"]
    assert "territory_name" in sales.dimensions
    assert sales.dimensions["region"].geo is not None
    assert {"bundle": "geography", "anchor_dataset": "regions", "datasets": None} in sales.to_public()["imports"]


# --- 008-safe-measure-compilation: non-framed measures never eval ----------

def test_non_framed_measure_expr_never_calls_eval(monkeypatch):
    import builtins

    def _boom(*a, **k):
        raise AssertionError("non-framed Measure.expr() must never call eval")
    monkeypatch.setattr(builtins, "eval", _boom)

    m = semantic.parse_model_text(VALID)
    assert m.measures["rows"].expr() is not None  # count() compiles fine without eval ever firing


def test_framed_measure_expr_still_uses_eval_path(monkeypatch):
    # the framed-measure carve-out is unaffected: it's still the pre-existing
    # eval-based compile_expr/compile_frame path, gated by auth at the API
    # layer rather than by the compiler itself.
    m = semantic.parse_model_text(FRAMED)
    assert m.measures["median_days"].expr() is not None
