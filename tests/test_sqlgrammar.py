"""The SQL grammar boundary (specs/018-duckdb-sql-engine/).

Three halves, and the third is the point of the module: that compiled
expressions produce the values you'd compute by hand, that the window mode
means what it says, and that a red-team suite of things a measure must never
be able to reach is refused — with the author's own text never reaching the
statement the engine builds.
"""
import pytest

from app import duck
from app.sqlgrammar import (
    SqlCompileError, compile_expression, compile_relation, is_window_expr,
    lag_period_param_names, referenced_names, referenced_parameter_names,
)

ROWS = """
(VALUES (100.0, 50.0, 'EU', 1, NULL),
        (200.0, 80.0, 'US', 1, 5.0),
        (300.0, 90.0, 'EU', 2, NULL),
        (400.0, 500.0, 'APAC', 3, 10.0))
  AS t(revenue, cost, region, user_id, maybe_null)
"""

SCHEMA = {"revenue": "DOUBLE", "cost": "DOUBLE", "region": "VARCHAR",
          "user_id": "BIGINT", "maybe_null": "DOUBLE"}

AGG_ROWS = """
(VALUES (DATE '2024-01-01', 'a', 10.0), (DATE '2024-02-01', 'a', 30.0),
        (DATE '2024-03-01', 'a', 60.0), (DATE '2024-01-01', 'b', 5.0),
        (DATE '2024-02-01', 'b', 15.0), (DATE '2024-03-01', 'b', 20.0))
  AS t(d, channel, revenue)
"""
AGG_SCHEMA = {"d": "DATE", "channel": "VARCHAR", "revenue": "DOUBLE"}
WINDOW_SPEC = 'PARTITION BY "channel" ORDER BY "d"'


def value(text, schema=SCHEMA, **kwargs):
    """One measure expression's value over ROWS — compiled through the real
    validator, then executed, so a test that passes proves both."""
    sql = compile_expression(text, schema, **kwargs)
    return duck.cursor().execute(f"SELECT {sql} AS v FROM {ROWS}").fetchone()[0]


def window_values(text, **kwargs):
    sql = compile_expression(text, AGG_SCHEMA, window=True, window_spec=WINDOW_SPEC, **kwargs)
    rows = duck.cursor().execute(
        f'SELECT channel, d, {sql} AS v FROM {AGG_ROWS} ORDER BY channel, d').fetchall()
    return {(r[0], str(r[1])): r[2] for r in rows}


# ── correctness ──────────────────────────────────────────────────────────

def test_plain_aggregate():
    assert value("SUM(revenue)") == 1000.0


def test_ratio_of_aggregates():
    assert value("SUM(revenue) / SUM(cost)") == pytest.approx(1000.0 / 720.0)


def test_filtered_aggregate():
    """What `where(value, predicate)` used to spell, in the SQL every database
    already has."""
    assert value("SUM(revenue) FILTER (WHERE region = 'EU')") == 400.0


def test_case_conditional():
    assert value("SUM(CASE WHEN region = 'EU' THEN revenue ELSE 0 END)") == 400.0


def test_coalesce():
    assert value("SUM(COALESCE(maybe_null, 0))") == 15.0


def test_cast():
    assert value("SUM(CAST(revenue AS BIGINT))") == 1000


def test_count_distinct():
    assert value("COUNT(DISTINCT user_id)") == 3


def test_count_star():
    assert value("COUNT(*)") == 4


def test_boolean_predicate_in_filter():
    assert value("SUM(revenue) FILTER (WHERE region = 'EU' AND cost < 60)") == 100.0


def test_in_and_not_in():
    assert value("SUM(revenue) FILTER (WHERE region IN ('EU', 'US'))") == 600.0
    assert value("SUM(revenue) FILTER (WHERE region NOT IN ('EU'))") == 600.0


def test_quoted_identifier_reaches_its_column():
    assert value('SUM("revenue")') == 1000.0


def test_things_the_old_dsl_could_not_express():
    """The grammar got bigger, not just different — these are the reason a
    complex metric no longer needs a language of its own."""
    assert value("MEDIAN(revenue)") == 250.0
    assert value("QUANTILE_CONT(revenue, 0.5)") == 250.0
    assert value("ARG_MAX(region, revenue)") == "APAC"
    assert value("SUM(revenue) - SUM(cost)") == 280.0


def test_unknown_column_rejected():
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("SUM(nope)", SCHEMA)
    assert exc.value.kind == "unknown_column"


def test_unknown_function_rejected():
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("frobnicate(revenue)", SCHEMA)
    assert exc.value.kind == "unknown_function"


def test_non_aggregate_rejected():
    """A measure reduces a group of rows to one value. A bare column produces a
    binder error several layers down otherwise."""
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("revenue", SCHEMA)
    assert exc.value.kind == "not_aggregate"


# ── the author's text never reaches the statement ────────────────────────

def test_trailing_comment_cannot_comment_out_the_rest_of_a_select():
    """The reason the emitted SQL is rendered from the validated AST rather
    than from the author's own text: `SUM(x) --` parses cleanly, and pasting it
    into a select list would comment out everything after it."""
    assert compile_expression("SUM(revenue) -- , 1 AS injected", SCHEMA) == "sum(revenue)"
    sql = compile_expression("SUM(revenue) -- , 1 AS injected", SCHEMA)
    row = duck.cursor().execute(f"SELECT {sql} AS a, 2 AS b FROM {ROWS}").fetchone()
    assert row == (1000.0, 2)


def test_a_second_statement_is_refused():
    with pytest.raises(SqlCompileError):
        compile_expression("SUM(revenue); DROP TABLE t", SCHEMA)


def test_a_from_clause_is_refused():
    with pytest.raises(SqlCompileError):
        compile_expression("SUM(revenue) FROM read_csv('/etc/passwd')", SCHEMA)


# ── window mode ──────────────────────────────────────────────────────────

def test_is_window_expr_detects_window_functions():
    assert is_window_expr("SUM(revenue) OVER w")
    assert is_window_expr("(revenue - LAG(revenue) OVER w) / LAG(revenue) OVER w")
    assert is_window_expr("RANK() OVER (ORDER BY revenue DESC)")
    assert not is_window_expr("SUM(revenue)")
    assert not is_window_expr("SUM(revenue) FILTER (WHERE region = 'EU')")


def test_referenced_names_excludes_function_names():
    assert referenced_names("SUM(revenue) OVER w") == {"revenue"}


def test_running_total_over_partition():
    got = window_values("SUM(revenue) OVER w")
    assert got[("a", "2024-01-01")] == 10.0
    assert got[("a", "2024-02-01")] == 40.0
    assert got[("a", "2024-03-01")] == 100.0
    assert got[("b", "2024-03-01")] == 40.0, "each partition runs its own total"


def test_pct_change_from_previous_period():
    got = window_values("(revenue - LAG(revenue) OVER w) / LAG(revenue) OVER w")
    assert got[("a", "2024-01-01")] is None
    assert got[("a", "2024-02-01")] == pytest.approx(2.0)
    assert got[("a", "2024-03-01")] == pytest.approx(1.0)


def test_lag_with_an_explicit_offset():
    got = window_values("LAG(revenue, 2) OVER w")
    assert got[("a", "2024-03-01")] == 10.0


def test_the_engines_window_is_inlined_not_referenced():
    """`OVER w` is resolved at parse time against the window the engine
    supplies, so the emitted SQL carries the whole thing and depends on no
    named window downstream."""
    sql = compile_expression("SUM(revenue) OVER w", AGG_SCHEMA, window=True,
                             window_spec=WINDOW_SPEC)
    assert "OVER (PARTITION BY channel ORDER BY d)" in sql
    assert " w" not in sql.replace("OVER (PARTITION BY channel ORDER BY d)", "")


def test_window_structural_validation_without_query_context():
    """Load time has no query, so no partition or order — the expression still
    has to validate."""
    assert compile_expression("SUM(revenue) OVER w", None, window=True)


def test_window_measure_unknown_sibling_rejected():
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("SUM(nope) OVER w", AGG_SCHEMA, window=True, window_spec=WINDOW_SPEC)
    assert exc.value.kind == "unknown_column"


def test_a_window_expression_is_refused_in_aggregate_mode():
    """The engine routes by is_window_expr; compiling one the other way would
    emit an empty OVER () and silently compute a grand total."""
    with pytest.raises(SqlCompileError):
        compile_expression("SUM(revenue) OVER w", AGG_SCHEMA)


def test_an_aggregate_is_refused_in_window_mode():
    with pytest.raises(SqlCompileError):
        compile_expression("SUM(revenue)", AGG_SCHEMA, window=True, window_spec=WINDOW_SPEC)


def test_a_bare_window_function_says_what_is_missing():
    """`LAG(revenue, 1)` was a whole measure in the old DSL. In SQL it needs an
    OVER clause, and DuckDB only complains about that when it binds."""
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("LAG(revenue, 1)", AGG_SCHEMA)
    assert exc.value.kind == "legacy_dsl"
    assert "OVER w" in str(exc.value)


# ── visual parameters ────────────────────────────────────────────────────

def test_param_resolves_to_the_same_result_as_the_literal():
    with_param = window_values("LAG(revenue, param('p')) OVER w", parameter_values={"p": 2})
    assert with_param == window_values("LAG(revenue, 2) OVER w")


def test_param_in_a_comparison():
    assert value("SUM(revenue) FILTER (WHERE cost > param('t'))",
                 parameter_values={"t": 85.0}) == 700.0


def test_param_in_a_case_branch():
    assert value("SUM(CASE WHEN region = param('r') THEN revenue ELSE 0 END)",
                 parameter_values={"r": "EU"}) == 400.0


def test_param_string_equality():
    assert value("COUNT(*) FILTER (WHERE region = param('r'))",
                 parameter_values={"r": "EU"}) == 2


def test_param_value_with_a_quote_cannot_break_out():
    """A parameter's value is substituted as a literal and the whole
    expression is then re-parsed and re-walked, so a value that tried to be
    more than a literal fails the walk rather than reaching the statement."""
    assert value("COUNT(*) FILTER (WHERE region = param('r'))",
                 parameter_values={"r": "EU' OR 1=1 --"}) == 0


def test_param_unknown_name_rejected():
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("SUM(revenue) + param('nope')", SCHEMA, parameter_values={})
    assert exc.value.kind == "unknown_parameter"


def test_param_with_no_visual_context_rejected():
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("SUM(revenue) + param('p')", SCHEMA)
    assert exc.value.kind == "unknown_parameter"


def test_referenced_parameter_names():
    assert referenced_parameter_names("SUM(a) * param('x') + param('y')") == {"x", "y"}


def test_lag_period_param_names():
    """Only the offset-argument reference, even when the expression carries
    another parameter elsewhere."""
    assert lag_period_param_names(
        "LAG(a, param('p1')) OVER w + CASE WHEN b > param('p2') THEN 1 ELSE 0 END") == {"p1"}


# ── from: relations ──────────────────────────────────────────────────────

def test_a_from_block_may_read_the_model_and_its_own_ctes():
    sql = compile_relation(
        "WITH r AS (SELECT * FROM __model) SELECT plan, MIN(start_date) AS s FROM r GROUP BY plan",
        allowed_tables={"__model"})
    assert "__model" in sql


def test_a_from_block_may_not_name_another_table():
    with pytest.raises(SqlCompileError):
        compile_relation("SELECT * FROM secrets", allowed_tables={"__model"})


@pytest.mark.parametrize("payload", [
    "SELECT * FROM read_parquet('s3://someone-elses-bucket/*.parquet')",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM glob('/**')",
    "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM delta_scan('s3://x/y')",
    "SELECT * FROM iceberg_scan('s3://x/y')",
])
def test_a_from_block_may_not_use_a_table_function(payload):
    """The whole of the I/O boundary: with table functions gone there is no
    file, no HTTP and no catalog to reach."""
    with pytest.raises(SqlCompileError):
        compile_relation(payload, allowed_tables={"__model"})


def test_a_from_block_is_one_statement():
    with pytest.raises(SqlCompileError):
        compile_relation("SELECT * FROM __model; DROP TABLE __model", allowed_tables={"__model"})


# ── red team ─────────────────────────────────────────────────────────────

RED_TEAM_PAYLOADS = [
    # reading something the measure was never given
    "SUM(x) FROM read_csv('/etc/passwd')",
    "(SELECT secret FROM other_table)",
    "SUM((SELECT max(x) FROM secrets))",
    "SUM(read_csv('/etc/passwd'))",
    "SUM(read_parquet('s3://someone-elses-bucket/x.parquet'))",
    "SUM(read_text('/etc/passwd'))",
    "SUM(read_blob('/etc/shadow'))",
    # reaching the engine's own configuration or catalog
    "SUM(current_setting('threads'))",
    "SUM(getvariable('x'))",
    "SUM(which_secret('s3://x', 's3'))",
    "MAX(version())",
    "SUM(pg_typeof(revenue))",
    # side effects and nondeterminism
    "SUM(nextval('seq'))",
    "SUM(random())",
    "MAX(uuid())",
    "SUM(sleep_ms(10000))",
    "SUM(error('boom'))",
    # structure the profile does not allow
    "SUM(revenue); DROP TABLE t",
    "SUM(revenue) UNION SELECT 1",
    "$1",
    "?",
    "*",
    # the old DSL, which must be reported rather than reinterpreted
    "count_distinct(user_id)",
    "where(revenue, region == 'EU')",
    "if_(region == 'EU', revenue, 0)",
    "col('revenue')",
    "running_total(revenue)",
    'cast(revenue, "int")',
]


@pytest.mark.parametrize("payload", RED_TEAM_PAYLOADS)
def test_red_team_suite_rejected(payload):
    with pytest.raises(SqlCompileError):
        compile_expression(payload, SCHEMA, parameter_values={})


def test_legacy_dsl_errors_name_their_replacement():
    for payload, expected in [
        ("count_distinct(user_id)", "COUNT(DISTINCT"),
        ("where(revenue, region == 'EU')", "FILTER"),
        ("if_(a, b, c)", "CASE WHEN"),
        ("running_total(revenue)", "OVER w"),
        ('cast(revenue, "int")', "CAST(x AS BIGINT)"),
    ]:
        with pytest.raises(SqlCompileError) as exc:
            compile_expression(payload, None, parameter_values={})
        assert exc.value.kind == "legacy_dsl"
        assert expected in str(exc.value), payload


# `generate_series`, `range` and `repeat` are catalogued as *both* a table
# function and a scalar one. In expression position DuckDB binds the scalar
# overload (a LIST, a string) — there is no way to spell the table one there,
# and a from: block's FROM clause is refused by node class, not by name. Every
# other table function is table-only and therefore ineligible.
DUAL_TYPED = {"generate_series", "range", "repeat"}


def test_table_functions_are_excluded_structurally_not_by_name():
    """The deny list is a backstop. What actually keeps read_parquet and the
    duckdb_* family out of an expression is that only scalar, aggregate and
    macro functions are eligible at all."""
    from app.sqlgrammar import allowed_functions

    catalogued = {name for (name,) in duck.cursor().execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() "
        "WHERE function_type = 'table'").fetchall()}
    assert catalogued & {"read_parquet", "read_csv", "glob", "duckdb_settings"}
    assert (catalogued & allowed_functions()) <= DUAL_TYPED


def test_the_dual_typed_names_bind_their_scalar_overload():
    """The three names that are both — proving the expression profile reaches
    the harmless half, not the row-producing one."""
    cursor = duck.cursor()
    assert cursor.execute(f"SELECT {compile_expression('MAX(len(range(3)))', {})}"
                          ).fetchone()[0] == 3
    repeated = compile_expression("MAX(repeat('ab', 2))", {})
    assert cursor.execute(f"SELECT {repeated}").fetchone()[0] == "abab"


def test_denied_scalar_functions_are_still_in_the_catalog():
    """A rename in DuckDB should surface here as a failing test rather than as
    a silently reopened hole."""
    from app.sqlgrammar import _DENY_NAMES, allowed_functions

    catalogued = {name for (name,) in duck.cursor().execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() "
        "WHERE function_type IN ('scalar', 'aggregate', 'macro')").fetchall()}
    still_there = _DENY_NAMES & catalogued
    assert "current_setting" in still_there and "nextval" in still_there
    assert not (still_there & allowed_functions())


# ── bounds ───────────────────────────────────────────────────────────────

def test_measure_text_length_limit_rejected():
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("SUM(revenue) + " * 400 + "1", SCHEMA)
    assert exc.value.kind == "limit_exceeded"


def test_excessive_nesting_depth_rejected():
    """Real nesting, not redundant parentheses — the parser folds those away
    before this module ever sees them."""
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("SUM(" + "abs(" * 60 + "revenue" + ")" * 60 + ")", SCHEMA)
    assert exc.value.kind == "limit_exceeded"


def test_excessive_node_count_rejected():
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("SUM(" + " + ".join(["revenue"] * 300) + ")", SCHEMA)
    assert exc.value.kind == "limit_exceeded"


def test_error_message_is_not_a_python_traceback():
    with pytest.raises(SqlCompileError) as exc:
        compile_expression("SUM(nope)", SCHEMA)
    message = str(exc.value)
    assert "Traceback" not in message and "\n" not in message
