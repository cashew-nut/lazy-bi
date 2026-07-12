"""Safe measure compiler: correctness (compiled expressions match hand-computed
values) and a red-team suite (every known Python-escape payload must raise
MeasureCompileError with zero execution — never eval/exec on measure text)."""
import builtins

import polars as pl
import pytest

from app.measure_dsl import MeasureCompileError, compile_measure


@pytest.fixture()
def df():
    return pl.DataFrame({
        "revenue": [100.0, 200.0, 300.0, 400.0],
        "cost": [50.0, 80.0, 90.0, 500.0],
        "region": ["EU", "US", "EU", "APAC"],
        "user_id": [1, 1, 2, 3],
        "maybe_null": [None, 5.0, None, 10.0],
    })


@pytest.fixture()
def schema(df):
    return df.lazy().collect_schema()


def _compile(text, schema, alias="v"):
    return compile_measure(text, schema, alias=alias)


# --- Correctness suite ------------------------------------------------------

def test_plain_aggregate(df, schema):
    assert df.select(_compile("sum(revenue)", schema))["v"][0] == 1000.0


def test_ratio_of_aggregates(df, schema):
    result = df.select(_compile("sum(revenue) / sum(cost)", schema))["v"][0]
    assert result == pytest.approx(1000.0 / 720.0)


def test_filtered_aggregate_via_where(df, schema):
    result = df.select(_compile('sum(where(revenue, region == "EU"))', schema))["v"][0]
    assert result == 400.0  # EU rows: 100 + 300


def test_if_conditional(df, schema):
    result = df.select(_compile("if_(sum(revenue) > 500, 1, 0)", schema))["v"][0]
    assert result == 1


def test_coalesce(df, schema):
    result = df.select(_compile("sum(coalesce(maybe_null, 0))", schema))["v"][0]
    assert result == 15.0  # nulls -> 0; 5 + 10


def test_cast(df, schema):
    result = df.select(_compile('cast(count(), "float")', schema))["v"][0]
    assert result == 4.0


def test_count_distinct(df, schema):
    result = df.select(_compile("count_distinct(user_id)", schema))["v"][0]
    assert result == 3


def test_bare_name_resolves_to_column(df, schema):
    result = df.select(_compile("sum(revenue - cost)", schema))["v"][0]
    assert result == pytest.approx(1000.0 - 720.0)


def test_boolean_and_or_in_predicate(df, schema):
    result = df.select(
        _compile('sum(where(revenue, region == "EU" and revenue > 100))', schema)
    )["v"][0]
    assert result == 300.0
    result2 = df.select(
        _compile('sum(where(revenue, region == "EU" or region == "US"))', schema)
    )["v"][0]
    assert result2 == 600.0


def test_in_and_not_in(df, schema):
    result = df.select(_compile('sum(where(revenue, region in ("EU", "US")))', schema))["v"][0]
    assert result == 600.0
    result2 = df.select(_compile('sum(where(revenue, region not in ("EU", "US")))', schema))["v"][0]
    assert result2 == 400.0


def test_col_function_explicit_form(df, schema):
    result = df.select(_compile('sum(col("revenue"))', schema))["v"][0]
    assert result == 1000.0


def test_unknown_column_rejected(schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("sum(does_not_exist)", schema, alias="v")
    assert exc.value.kind == "unknown_column"


def test_unknown_function_rejected(schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("foobar(revenue)", schema, alias="v")
    assert exc.value.kind == "unknown_function"


# --- Red-team suite: every payload must raise, and must never execute ------

RED_TEAM_PAYLOADS = [
    "__import__('os').system('id')",
    "().__class__.__bases__[0].__subclasses__()",
    "open('/etc/passwd').read()",
    "getattr(revenue, '__globals__')",
    "revenue.__class__",
    "scan_parquet('s3://evil/x')",
    "read_csv('/etc/passwd')",
    "map_elements(lambda x: x)",
    "apply(revenue)",
    "[x for x in range(10)]",
    "{x for x in range(10)}",
    "{x: x for x in range(10)}",
    "(x for x in range(10))",
    "lambda x: x",
    "revenue[0]",
    "f'{revenue}'",
    "sum(revenue) if revenue > 0 else 0",  # IfExp — use if_() instead
    "(yield revenue)",
    "(x := revenue)",
]


@pytest.fixture(autouse=True)
def _forbid_eval_exec(monkeypatch):
    """Sentinel: compile_measure must never call eval/exec on measure text.
    (compile()/ast.parse's internal AST-only use is a different, safe
    operation and is deliberately not patched here.)"""
    def _boom(*a, **k):
        raise AssertionError("compile_measure must never call eval/exec")
    monkeypatch.setattr(builtins, "eval", _boom)
    monkeypatch.setattr(builtins, "exec", _boom)
    yield


@pytest.mark.parametrize("payload", RED_TEAM_PAYLOADS)
def test_red_team_suite_rejected(payload, schema):
    with pytest.raises(MeasureCompileError):
        compile_measure(payload, schema, alias="v")


def test_measure_text_length_limit_rejected(schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("x" * 2500, schema, alias="v")
    assert exc.value.kind == "limit_exceeded"


def test_excessive_node_count_rejected(schema):
    wide = " + ".join(["revenue"] * 150)
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure(wide, schema, alias="v")
    assert exc.value.kind == "limit_exceeded"


def test_excessive_nesting_depth_rejected(schema):
    deep = "-" * 40 + "revenue"  # 40 nested unary minus: few nodes, deep chain
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure(deep, schema, alias="v")
    assert exc.value.kind == "limit_exceeded"


def test_dunder_column_name_rejected(schema):
    with pytest.raises(MeasureCompileError):
        compile_measure("sum(__class__)", schema, alias="v")
