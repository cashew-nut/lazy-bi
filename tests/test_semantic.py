"""Semantic layer: yaml parsing and validation."""
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
    expr: pl.len()
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
    bad = VALID.replace("pl.len()", "pl.nope()")
    with pytest.raises(semantic.ModelError, match="rows"):
        semantic.parse_model_text(bad)


def test_measure_must_be_expr():
    bad = VALID.replace("pl.len()", '"42"')  # evaluates, but to an int
    with pytest.raises(semantic.ModelError, match="not a polars Expr"):
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
