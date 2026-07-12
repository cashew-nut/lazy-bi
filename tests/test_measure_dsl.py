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


# --- Window measures: running_total() / lag() ------------------------------
# These compile against the *aggregated* schema (dims + sibling measure
# names), not raw columns — the engine passes partition_by/order_by once it
# knows the query's dimensions (see app/engine.py). Here we exercise the
# compiler directly against a stand-in "already aggregated" frame.

@pytest.fixture()
def agg_df():
    return pl.DataFrame({
        "region": ["east", "east", "east", "west", "west", "west"],
        "quarter": ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q1", "2025-Q2", "2025-Q3"],
        "revenue": [100.0, 150.0, 120.0, 200.0, 180.0, 220.0],
    })


@pytest.fixture()
def agg_schema(agg_df):
    return agg_df.lazy().collect_schema()


def test_is_window_expr_detects_running_total_and_lag():
    from app.measure_dsl import is_window_expr
    assert is_window_expr("running_total(revenue)")
    assert is_window_expr("(revenue - lag(revenue, 1)) / lag(revenue, 1)")
    assert not is_window_expr("sum(revenue)")
    assert not is_window_expr('if_(sum(revenue) > 0, 1, 0)')  # if_ alone isn't a window signal


def test_referenced_names_excludes_function_names():
    from app.measure_dsl import referenced_names
    assert referenced_names("(revenue - lag(revenue, 1)) / lag(revenue, 1)") == {"revenue"}


def test_running_total_over_partition(agg_df, agg_schema):
    expr = compile_measure(
        "running_total(revenue)", agg_schema, alias="rt", partition_by=["region"], order_by="quarter",
    )
    out = agg_df.sort("region", "quarter").select("region", "quarter", expr)
    east = out.filter(pl.col("region") == "east")["rt"].to_list()
    west = out.filter(pl.col("region") == "west")["rt"].to_list()
    assert east == [100.0, 250.0, 370.0]
    assert west == [200.0, 380.0, 600.0]


def test_running_total_with_no_partition_is_global(agg_df, agg_schema):
    # a query with only the time dimension (no breakout) -> partition_by=[]
    expr = compile_measure(
        "running_total(revenue)", agg_schema, alias="rt", partition_by=[], order_by="quarter",
    )
    out = agg_df.filter(pl.col("region") == "east").sort("quarter").select(expr)["rt"].to_list()
    assert out == [100.0, 250.0, 370.0]


def test_pct_change_from_previous_period(agg_df, agg_schema):
    text = "(revenue - lag(revenue, 1)) / lag(revenue, 1)"
    expr = compile_measure(text, agg_schema, alias="qoq", partition_by=["region"], order_by="quarter")
    out = agg_df.sort("region", "quarter").select("region", "quarter", expr)
    east = out.filter(pl.col("region") == "east")["qoq"].to_list()
    assert east[0] is None  # no prior quarter
    assert east[1] == pytest.approx(0.5)     # 150 vs 100
    assert east[2] == pytest.approx(-0.2)    # 120 vs 150


def test_lag_default_period_is_one(agg_df, agg_schema):
    e1 = compile_measure("lag(revenue)", agg_schema, alias="l", partition_by=["region"], order_by="quarter")
    e2 = compile_measure("lag(revenue, 1)", agg_schema, alias="l", partition_by=["region"], order_by="quarter")
    out1 = agg_df.sort("region", "quarter").select(e1)["l"].to_list()
    out2 = agg_df.sort("region", "quarter").select(e2)["l"].to_list()
    assert out1 == out2


def test_window_structural_validation_without_query_context():
    """Model-yaml load time: no live partition_by/order_by yet — the bare
    reduction must still compile (schema=None skips column checks, same
    convention as aggregate measures at load time)."""
    expr = compile_measure("running_total(revenue)", None, alias="rt")
    assert expr is not None
    expr2 = compile_measure("coalesce(lag(revenue, 1), 0)", None, alias="l")
    assert expr2 is not None


def test_window_measure_rejects_aggregate_functions(agg_schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("running_total(sum(revenue))", agg_schema, alias="bad",
                         partition_by=["region"], order_by="quarter")
    assert exc.value.kind == "disallowed"


def test_aggregate_measure_rejects_window_functions_leaking_in(schema):
    # running_total/lag anywhere flips the whole expr to window mode, so an
    # aggregate call inside it is then rejected as "aggregate-inside-window",
    # not silently accepted
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("sum(running_total(revenue))", schema, alias="bad")
    assert exc.value.kind == "disallowed"


def test_window_measure_col_function_not_available(agg_schema):
    # col() is a raw-source-column escape hatch; window measures only ever
    # see sibling measures, so col() has nothing to point at
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure('running_total(col("revenue"))', agg_schema, alias="bad",
                         partition_by=["region"], order_by="quarter")
    assert exc.value.kind == "disallowed"


def test_window_measure_unknown_sibling_rejected(agg_schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("running_total(doesnotexist)", agg_schema, alias="bad",
                         partition_by=["region"], order_by="quarter")
    assert exc.value.kind == "unknown_column"
    assert "measure" in str(exc.value)


def test_lag_requires_positive_integer_periods(agg_schema):
    with pytest.raises(MeasureCompileError):
        compile_measure("lag(revenue, 0)", agg_schema, alias="bad", partition_by=[], order_by="quarter")
    with pytest.raises(MeasureCompileError):
        compile_measure("lag(revenue, -1)", agg_schema, alias="bad", partition_by=[], order_by="quarter")
    with pytest.raises(MeasureCompileError):
        compile_measure("lag(revenue, 1.5)", agg_schema, alias="bad", partition_by=[], order_by="quarter")


# --- param(): visual-declared parameters referenced from lag() -------------

def test_lag_param_resolves_to_same_result_as_equivalent_literal(agg_df, agg_schema):
    text_literal = "lag(revenue, 2)"
    text_param = "lag(revenue, param('period_list'))"
    e1 = compile_measure(text_literal, agg_schema, alias="l", partition_by=["region"], order_by="quarter")
    e2 = compile_measure(
        text_param, agg_schema, alias="l", partition_by=["region"], order_by="quarter",
        parameter_values={"period_list": 2},
    )
    out1 = agg_df.sort("region", "quarter").select(e1)["l"].to_list()
    out2 = agg_df.sort("region", "quarter").select(e2)["l"].to_list()
    assert out1 == out2


def test_lag_param_unknown_name_rejected(agg_schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure(
            "lag(revenue, param('nope'))", agg_schema, alias="bad",
            partition_by=["region"], order_by="quarter", parameter_values={"period_list": 2},
        )
    assert exc.value.kind == "unknown_parameter"


def test_lag_param_with_no_parameter_values_rejected(agg_schema):
    # structural-only validation (parameter_values=None) fails closed, same
    # posture as partition_by/order_by=None for the window .over() step
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("lag(revenue, param('period_list'))", agg_schema, alias="bad")
    assert exc.value.kind == "unknown_parameter"


def test_lag_param_resolved_value_must_still_be_positive(agg_schema):
    with pytest.raises(MeasureCompileError):
        compile_measure(
            "lag(revenue, param('period_list'))", agg_schema, alias="bad",
            partition_by=["region"], order_by="quarter", parameter_values={"period_list": 0},
        )


def test_param_wrong_arity_or_type_rejected(agg_schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure(
            "lag(revenue, param(1))", agg_schema, alias="bad",
            partition_by=["region"], order_by="quarter", parameter_values={"period_list": 2},
        )
    assert exc.value.kind == "disallowed"
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure(
            "lag(revenue, param('a', 'b'))", agg_schema, alias="bad",
            partition_by=["region"], order_by="quarter", parameter_values={"period_list": 2},
        )
    assert exc.value.kind == "disallowed"


@pytest.mark.parametrize("text", [
    "param('period_list') > 0",
    "running_total(param('period_list'))",
    "if_(param('period_list') > 1, revenue, 0)",
])
def test_param_outside_lag_periods_rejected(agg_schema, text):
    # param() is not in any function table — it's recognized only while
    # parsing lag()'s second argument, so anywhere else it hits the
    # pre-existing generic "unknown function" rejection
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure(
            text, agg_schema, alias="bad",
            partition_by=["region"], order_by="quarter", parameter_values={"period_list": 2},
        )
    assert exc.value.kind == "unknown_function"


def test_referenced_parameter_names():
    from app.measure_dsl import referenced_parameter_names
    assert referenced_parameter_names("lag(revenue, param('period_list'))") == {"period_list"}
    assert referenced_parameter_names("sum(revenue)") == set()
    assert referenced_parameter_names(
        "lag(a, param('p1')) + lag(b, param('p2'))"
    ) == {"p1", "p2"}


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
    # window mode must be exactly as closed as aggregate mode
    "running_total(__import__('os').system('id'))",
    "lag(revenue.__class__, 1)",
    "running_total(getattr(revenue, '__globals__'))",
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


# --- US4: disallowed (security) vs merely-unsupported errors are distinguishable ---

def test_disallowed_construct_has_disallowed_kind(schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("revenue.__class__", schema, alias="v")
    assert exc.value.kind == "disallowed"


def test_unknown_function_has_unknown_function_kind(schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("foobar(revenue)", schema, alias="v")
    assert exc.value.kind == "unknown_function"


def test_unknown_column_has_unknown_column_kind(schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("sum(does_not_exist)", schema, alias="v")
    assert exc.value.kind == "unknown_column"


def test_oversized_input_has_limit_exceeded_kind(schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("x" * 2500, schema, alias="v")
    assert exc.value.kind == "limit_exceeded"


def test_error_message_is_not_a_python_traceback(schema):
    with pytest.raises(MeasureCompileError) as exc:
        compile_measure("foobar(revenue)", schema, alias="v")
    message = str(exc.value)
    assert "Traceback" not in message and "File \"" not in message
    assert "foobar" in message  # names the specific unsupported construct
