"""Query engine: turns a semantic-layer query into one DuckDB statement.

One statement, not one per fact table and not one per measure. That is the
whole shape of this module: the joins, the point-in-time timelines, the
intermediary relations a complex measure aggregates over, and the merge across
several unrelated fact tables are all clauses of a single query the planner
sees whole. Against a real object store that is the difference between paying
for one round of I/O and paying for six.

Nothing an author wrote is ever concatenated into that statement directly.
Measure expressions and `from:` blocks arrive as text rendered from their own
validated AST (see app/sqlgrammar.py), filter values are bound as prepared
parameters, and every identifier the engine emits is quoted by _q. What is
left to interpolate is source paths and column names this module resolved
itself.

See specs/018-duckdb-sql-engine/contracts/engine-sql.md for the shape of the
statement, layer by layer.
"""
from __future__ import annotations

import calendar
import json
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

import duckdb

from . import cache, config, duck, semantic, sqlgrammar
from .semantic import (
    Dimension, ImportBinding, Model, ModelError, Source, TIME_GRAINS,
    MODEL_RELATION, render_from_block,
)

FILTER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains"}

# stand-in for a null interval end ("still open"), shared by spine dimensions
# and `how: between` dimension imports
FAR_FUTURE = date(9999, 1, 1)

_COMPARE_SQL = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

# the five grains the builder exposes, as DuckDB date_trunc parts. A quarter
# is a real date_trunc part; as an *interval* it is not, hence _GRAIN_INTERVAL.
GRAIN_PART = {"1d": "day", "1w": "week", "1mo": "month", "1q": "quarter", "1y": "year"}
GRAIN_INTERVAL = {
    "1d": "INTERVAL 1 DAY", "1w": "INTERVAL 1 WEEK", "1mo": "INTERVAL 1 MONTH",
    "1q": "INTERVAL 3 MONTH", "1y": "INTERVAL 1 YEAR",
}
GRAIN_ORDER = list(TIME_GRAINS)  # finest to coarsest: 1d, 1w, 1mo, 1q, 1y

# ── dynamic ("relative") date filter values ──────────────────────
# A time filter's value may be a relative token like "today" or
# "start_of_month" instead of a fixed ISO date. It's resolved against the
# current date on every query, so a saved "today" keeps meaning today on
# every future run.
#
# The grammar is small and closed, and this module is its only definition —
# everything that describes it to a human or an LLM (the tool schema and
# system prompt in app/llm.py, the builder's filter control in
# static/js/filters.js) is built from the constants below rather than
# restating them, so the accepted vocabulary can't drift from the enforced
# one. See RELATIVE_DATE_SYNTAX for the prose form.

def _end_of_month(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _start_of_quarter(d: date) -> date:
    return d.replace(month=(d.month - 1) // 3 * 3 + 1, day=1)


def _end_of_quarter(d: date) -> date:
    start = _start_of_quarter(d)
    return _end_of_month(start.replace(month=start.month + 2))


RELATIVE_DATE_KEYWORDS = {
    "today": lambda d: d,
    "yesterday": lambda d: d - timedelta(days=1),
    "tomorrow": lambda d: d + timedelta(days=1),
    "start_of_week": lambda d: d - timedelta(days=d.weekday()),
    "end_of_week": lambda d: d - timedelta(days=d.weekday()) + timedelta(days=6),
    "start_of_month": lambda d: d.replace(day=1),
    "end_of_month": _end_of_month,
    "start_of_quarter": _start_of_quarter,
    "end_of_quarter": _end_of_quarter,
    "start_of_year": lambda d: d.replace(month=1, day=1),
    "end_of_year": lambda d: d.replace(month=12, day=31),
}

# units for the optional offset suffix, written as <n><unit> ("90d", "1mo")
RELATIVE_OFFSET_UNITS = {
    "d": "days", "w": "weeks", "mo": "months", "q": "quarters", "y": "years",
}

# <keyword><+|-><n><unit>. Every keyword takes an offset, not just "today":
# "start of last year" has no expressible form otherwise (today-1y is the
# same day last year, not 1 January), so an LLM asked for one had to either
# hardcode an ISO date — which freezes a saved query to the day it was
# written — or invent syntax the engine then choked on.
_KEYWORD_ALT = "|".join(sorted(RELATIVE_DATE_KEYWORDS, key=len, reverse=True))
_UNIT_ALT = "|".join(sorted(RELATIVE_OFFSET_UNITS, key=len, reverse=True))
_RELATIVE_OFFSET_RE = re.compile(rf"^({_KEYWORD_ALT})([+-])(\d+)({_UNIT_ALT})$")

RELATIVE_DATE_SYNTAX = (
    "A relative date is one keyword, optionally followed by exactly one "
    "offset: <keyword> or <keyword><+|-><n><unit>. Keywords: "
    + ", ".join(RELATIVE_DATE_KEYWORDS)
    + ". Units: " + ", ".join(f"{u} ({name})" for u, name in RELATIVE_OFFSET_UNITS.items())
    + ". The offset shifts today first and the keyword then takes that "
    "shifted date's period edge, so 'start_of_year-1y' is 1 January last "
    "year, 'end_of_quarter-1q' is the last day of last quarter, and "
    "'start_of_month-1mo' is the 1st of last month. Anything outside that "
    "grammar is not a relative date and must be written as an ISO date "
    "(YYYY-MM-DD): there is no 'last_year'/'last_month'/'ytd'/'mtd' keyword, "
    "no spelled-out arithmetic ('start_of_year - 1 year'), no second offset "
    "('today-1y+2d'), and no units beyond the five above. A whole period is "
    "two filters — gte its first day and lte its last, e.g. last year is "
    "gte 'start_of_year-1y' and lte 'end_of_year-1y'."
)


def _add_months(d: date, n: int) -> date:
    month0 = d.month - 1 + n
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _shift(d: date, n: int, unit: str) -> date:
    """`d` moved by `n` whole `unit`s (a key of RELATIVE_OFFSET_UNITS)."""
    if unit == "d":
        return d + timedelta(days=n)
    if unit == "w":
        return d + timedelta(weeks=n)
    if unit == "mo":
        return _add_months(d, n)
    if unit == "q":
        return _add_months(d, n * 3)
    return _add_months(d, n * 12)  # "y"


def resolve_relative_date(value: Any, today: Optional[date] = None) -> Optional[date]:
    """Resolve a relative-date token (RELATIVE_DATE_SYNTAX) to a concrete
    date, or return None if `value` isn't one — the caller then falls back to
    parsing a fixed date.

    An offset is applied to `today` *before* its keyword, never after, so a
    composed token always lands on a real period edge: "end_of_month+1mo" is
    the last day of next month, which shifting this month's last day forward
    would get wrong whenever the two months differ in length."""
    key = str(value).strip().lower()
    base = today or date.today()
    keyword = RELATIVE_DATE_KEYWORDS.get(key)
    if keyword:
        return keyword(base)
    m = _RELATIVE_OFFSET_RE.match(key)
    if not m:
        return None
    sign, n, unit = m.group(2), int(m.group(3)), m.group(4)
    return RELATIVE_DATE_KEYWORDS[m.group(1)](_shift(base, n if sign == "+" else -n, unit))


def parse_date_value(value: Any, today: Optional[date] = None) -> Optional[date]:
    """The date a filter value denotes — a relative token resolved against
    today, or a fixed ISO date — or None if it's neither."""
    relative = resolve_relative_date(value, today)
    if relative is not None:
        return relative
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _bad_date_message(value: Any) -> str:
    return f"'{value}' isn't a valid date filter value. {RELATIVE_DATE_SYNTAX}"


def date_value_error(value: Any) -> Optional[str]:
    """Why `value` can't be a time filter's value, or None if it can be.

    Lets a caller *upstream* of the engine — nlq's re-validation of an LLM
    proposal — reject a bad value by the engine's own rule and in the
    engine's own words, rather than letting it reach _coerce and surface as
    a failed query. Deliberately more permissive than _resolve_date_value
    about a time component ('2025-01-31T09:00'), which is valid against a
    datetime column and whose dtype isn't known this far up."""
    if value is None or parse_date_value(value) is not None:
        return None
    try:
        datetime.fromisoformat(str(value))
    except ValueError:
        return _bad_date_message(value)
    return None



# ── identifiers, types, values ───────────────────────────────────────────

def _q(name: str) -> str:
    """One identifier, quoted. Every column and alias this module emits goes
    through here — a source column is whatever the file happened to call it,
    including a keyword, a space or a capital."""
    return '"' + str(name).replace('"', '""') + '"'


def _lit(text: str) -> str:
    """A string literal for something this module resolved itself (a source
    path). Filter values never come through here — they are bound."""
    return "'" + str(text).replace("'", "''") + "'"


_INTEGER_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
                  "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}
_FLOAT_TYPES = {"FLOAT", "DOUBLE", "REAL"}


def _base_type(dtype: Optional[str]) -> str:
    """A DuckDB type name without its parameters: DECIMAL(18,3) -> DECIMAL."""
    return (dtype or "").split("(")[0].strip().upper()


def _is_date(dtype: Optional[str]) -> bool:
    return _base_type(dtype) == "DATE"


def _is_timestamp(dtype: Optional[str]) -> bool:
    return _base_type(dtype).startswith("TIMESTAMP")


def _is_temporal(dtype: Optional[str]) -> bool:
    return _is_date(dtype) or _is_timestamp(dtype)


def _json_safe(value: Any) -> Any:
    """One result value as something json.dumps accepts, matching what the
    browser has always received: dates as ISO strings, decimals as numbers."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class QueryError(Exception):
    pass


# ── reading a source ─────────────────────────────────────────────────────

def scan_source(source: Source) -> str:
    """SQL naming one source's rows — a pinned local table when it is small
    enough to have been held, else the table function that reads it from the
    object store. app/duck.py owns that decision; this is the seam every byte
    a query reads still enters through."""
    try:
        return duck.relation(source.path, source.format)
    except duck.DuckError as exc:
        raise QueryError(str(exc)) from exc
    except Exception as exc:
        raise QueryError(f"cannot read {source.format} source '{source.path}': {exc}") from exc


def source_schema(source: Source) -> dict:
    """Cached column schema for one source (its own, unjoined) — the
    footer-only read source introspection needs (dataset picker, dimension
    bundle editor), without re-hitting S3 for it on every keystroke."""
    try:
        return duck.source_schema(source.path, source.format)
    except duck.DuckError as exc:
        raise QueryError(str(exc)) from exc
    except Exception as exc:
        raise QueryError(f"cannot read {source.format} source '{source.path}': {exc}") from exc


def _scan_bundle(binding: ImportBinding) -> tuple[str, list[str]]:
    """One imported dimension bundle as a projected subquery, plus the column
    names it exposes.

    Scan the anchor dataset, join in every other dataset the import resolved as
    reachable via the bundle's own declared DatasetJoins, then project down.
    Each join is applied with the already-accumulated side as the left operand
    and `how` taken from the edge as declared — so an import always preserves
    the anchor (and anything already pulled in) in full, gaining nullable
    columns for anything only reachable in the reverse of how the bundle's
    author happened to declare that particular edge.

    The projection is the part that matters downstream: every included
    dimension arrives under its *dimension* name rather than the bundle's own
    column name, so a model whose source has a `month` of its own can import a
    calendar that also has one."""
    bundle = binding.bundle
    included = set(binding.included_datasets)
    edge_by_pair = {(ds.name, j.to): j
                    for ds in bundle.datasets.values() for j in ds.joins}

    anchor = binding.import_spec.anchor_dataset
    alias_of = {anchor: f"__b_{anchor}"}
    sql = f"{scan_source(bundle.datasets[anchor].source)} AS {_q(alias_of[anchor])}"
    joined, remaining = {anchor}, included - {anchor}
    while remaining:
        progressed = False
        for name in list(remaining):
            edge, reversed_edge, other = None, False, None
            for joined_name in joined:
                if (joined_name, name) in edge_by_pair:
                    edge, other = edge_by_pair[(joined_name, name)], joined_name
                    break
                if (name, joined_name) in edge_by_pair:
                    edge, reversed_edge, other = edge_by_pair[(name, joined_name)], True, joined_name
                    break
            if edge is None:
                continue
            left_on, right_on = ((edge.right_on, edge.left_on) if reversed_edge
                                 else (edge.left_on, edge.right_on))
            alias_of[name] = f"__b_{name}"
            on = " AND ".join(
                f"{_q(alias_of[other])}.{_q(l)} = {_q(alias_of[name])}.{_q(r)}"
                for l, r in zip(left_on, right_on))
            how = "INNER" if edge.how == "inner" else "LEFT"
            sql += (f" {how} JOIN {scan_source(bundle.datasets[name].source)} "
                    f"AS {_q(alias_of[name])} ON {on}")
            joined.add(name)
            remaining.discard(name)
            progressed = True
        if not progressed:
            # _resolve_part_imports() computes `included` via the same
            # reachability rules, so everything in it must connect to the anchor
            raise ModelError(
                f"dimension bundle '{bundle.name}': internal error resolving join "
                f"order for datasets {sorted(remaining)}")

    projected: list[str] = []
    columns: list[str] = []

    def keep(dataset: str, column: str, alias: str) -> None:
        if alias not in columns:
            columns.append(alias)
            projected.append(f"{_q(alias_of[dataset])}.{_q(column)} AS {_q(alias)}")

    for name in binding.included_datasets:      # the list, for a stable column order
        for dim in bundle.datasets[name].dimensions.values():
            keep(name, dim.column, dim.name)
            if dim.geo:
                keep(name, dim.geo.lat, dim.geo.lat)
                keep(name, dim.geo.lon, dim.geo.lon)
    for key in binding.import_spec.right_on:
        keep(anchor, key, key)
    return f"(SELECT {', '.join(projected)} FROM {sql})", columns


# hidden columns carrying a reporting period's span alongside the row that
# represents it; dropped again as soon as the join that needs them is built
PERIOD_FROM = "__period_from"
PERIOD_TO = "__period_to"
# a constant carried into every `from:` block's {dims}, so the placeholder is
# never empty and `SELECT {dims}, x` stays legal when the query groups by
# nothing at all
GROUP_CONST = "__all"


def match_predicate(start: str, end: str, lo: str, hi: str, match: str) -> str:
    """The predicate matching a row's [start, end] interval against a reporting
    period spanning [lo, hi].

    Three readings of "was this row active in this period", and this is the
    only definition of them — both point-in-time mechanisms (Dimension.spine
    and a `how: between` import) build their join from here, so they always
    answer the same question:

      overlap       the interval touches the period at all — a row opened on
                    Feb 2nd and closed Feb 15th counts in February, in Q1 and
                    in the year, which is what "active during" normally means
      period_start  it was already open on the period's first day
      period_end    it was still open on the period's last day

    The last two are snapshots, so a row that opens and closes inside a period
    without spanning its boundary is not counted; `overlap` is the default for
    that reason. At day grain the period is one day wide and all three agree.
    """
    if match == "period_start":
        return f"{start} <= {lo} AND {end} >= {lo}"
    if match == "period_end":
        return f"{start} <= {hi} AND {end} >= {hi}"
    return f"{start} <= {hi} AND {end} >= {lo}"


def _period_rows(relation: str, point: str, grain: str) -> str:
    """A date table collapsed to one row per `grain` bucket, carrying that
    bucket's span in PERIOD_FROM/PERIOD_TO.

    This is what makes an interval import grain-correct. The table stores days,
    but a query grouping at month grain wants each model row counted *once* for
    the month, not once for each of its 30 days — one row per bucket means the
    join produces one row per (model row, bucket), so an additive measure sums
    each row once at whatever grain is being asked for.

    The span is the days the table actually holds for that bucket, not the
    calendar-arithmetic bounds: a table that starts mid-month, or one that only
    lists business days, reports the period it really covers. At grain 1d each
    bucket is a single day and the span collapses to it.
    """
    bucket = f"date_trunc('{GRAIN_PART[grain]}', {_q(point)})"
    return (
        f"(SELECT * FROM ("
        f"SELECT *, min({_q(point)}) OVER (PARTITION BY {bucket}) AS {_q(PERIOD_FROM)}, "
        f"max({_q(point)}) OVER (PARTITION BY {bucket}) AS {_q(PERIOD_TO)} "
        f"FROM {relation}) WHERE {_q(point)} = {_q(PERIOD_FROM)})"
    )


def _interval_grain(model: Model, binding: ImportBinding, dimensions: dict) -> str:
    """The bucket size a date table has to be thinned to for this query: the
    finest grain among the import's dimensions in play.

    Each contributes the grain the query asked it for (a time dimension read
    through the builder's grain picker), else the grain it declares (a quarter
    or month label column is constant across that bucket), else the table's own
    row grain. Finest wins: grouping by both a day and its quarter needs a row
    per day, and the quarter is then just an attribute of that day.
    """
    grains = []
    for name, query_grain in dimensions.items():
        if name not in binding.dimension_owners:
            continue
        declared = model.dimensions[name].grain if name in model.dimensions else None
        grains.append(query_grain or declared or GRAIN_ORDER[0])
    return min(grains, key=GRAIN_ORDER.index) if grains else GRAIN_ORDER[0]


# ── the fact relation ────────────────────────────────────────────────────

FACT_ALIAS = "__f"


def _projection(parts: list[tuple[str, list[str]]]) -> tuple[list[str], list[str]]:
    """An explicit select list over a chain of joined relations, and the column
    names it produces.

    Explicit rather than `SELECT *` because a join whose two sides share a
    column name would otherwise produce two columns with that name, and every
    reference to it afterwards is ambiguous. First occurrence keeps the name;
    a later one is suffixed — the same convention polars applied, so a model
    whose dimension reads a `_right` column keeps reading the same one."""
    select: list[str] = []
    names: list[str] = []
    for alias, columns in parts:
        for column in columns:
            name, n = column, 1
            while name in names:
                n += 1
                name = f"{column}_right" if n == 2 else f"{column}_right_{n - 1}"
            names.append(name)
            select.append(f"{_q(alias)}.{_q(column)} AS {_q(name)}")
    return select, names


def _source_columns(source: Source) -> list[str]:
    return list(source_schema(source))


def scan(model: Model, dimensions: Optional[dict] = None) -> str:
    """The model's base source plus its joins and imported dimension bundles,
    as one projected subquery — usable as `FROM <this> AS alias`.

    `dimensions` maps each dimension name the caller is about to use to the
    grain it wants it at (None for "the dimension's own"). It matters only for
    interval (`how: between`) imports, which are

      - applied only when one of their dimensions is in play, since that join
        puts a model row in every bucket it spans and a query that never reads
        the date table would otherwise see every measure multiply; and
      - thinned to the finest grain among those dimensions, so the join lands
        one row per bucket rather than one per day.

    None means "unknown — apply everything at day grain", the right answer for
    schema introspection.
    """
    if model.is_composite:
        raise QueryError(
            f"'{model.name}' holds {len(model.parts)} unrelated fact tables "
            f"({', '.join(p.name for p in model.parts)}) — there is no single relation to "
            f"scan; each part is scanned on its own (see _run_parts)")

    from_sql = f"{scan_source(model.source)} AS {_q(FACT_ALIAS)}"
    parts: list[tuple[str, list[str]]] = [(FACT_ALIAS, _source_columns(model.source))]

    for index, join in enumerate(model.joins):
        alias = f"__j{index}"
        on = " AND ".join(
            f"{_q(FACT_ALIAS)}.{_q(l)} = {_q(alias)}.{_q(r)}"
            for l, r in zip(join.left_on, join.right_on))
        how = "INNER" if join.how == "inner" else "LEFT"
        from_sql += f" {how} JOIN {scan_source(join.source)} AS {_q(alias)} ON {on}"
        parts.append((alias, _source_columns(join.source)))

    for index, binding in enumerate(model.import_bindings):
        alias = f"__i{index}"
        spec = binding.import_spec
        bundle_sql, bundle_columns = _scan_bundle(binding)
        if spec.is_interval:
            if dimensions is not None and not set(dimensions) & set(binding.dimension_owners):
                continue
            grain = (GRAIN_ORDER[0] if dimensions is None
                     else _interval_grain(model, binding, dimensions))
            from_sql += _interval_join(
                bundle_sql, bundle_columns, binding, grain, alias,
                {c for _, cols in parts for c in cols})
            parts.append((alias, bundle_columns))
            continue
        # A `how: left` import only ever adds columns, so a query using none of
        # them gets the same answer without it — and paying for a join it can't
        # read from is how a model that imports a calendar purely to conform
        # with its neighbours (models/sales.yaml) would slow down every query
        # that has nothing to do with dates. `inner` also *filters* the model's
        # rows, so it has to be applied whether or not its dimensions are read.
        if (dimensions is not None and spec.how == "left"
                and not set(dimensions) & set(binding.dimension_owners)):
            continue
        on = " AND ".join(
            f"{_q(FACT_ALIAS)}.{_q(l)} = {_q(alias)}.{_q(r)}"
            for l, r in zip(spec.left_on, spec.right_on))
        how = "INNER" if spec.how == "inner" else "LEFT"
        from_sql += f" {how} JOIN {bundle_sql} AS {_q(alias)} ON {on}"
        parts.append((alias, bundle_columns))

    select, _ = _projection(parts)
    return f"(SELECT {', '.join(select)} FROM {from_sql})"


def _interval_join(bundle_sql: str, bundle_columns: list[str], binding: ImportBinding,
                   grain: str, alias: str, fact_columns: set) -> str:
    """Apply a `how: between` dimension import: an interval join against a date
    table, thinned to one row per `grain` bucket first.

    This is the "disconnected calendar table" shape — the imported dataset
    declares no relation to the model's other data, and this join is the only
    thing that connects them. Once joined, its columns are ordinary dimensions:
    grouping by the calendar's date (or its year/quarter/month attributes)
    yields point-in-time aggregation, because a model row is present in every
    bucket it was active for rather than only the one it started in.

    Because the table is collapsed to one row per bucket at the query's grain
    before the join, the result matches a spine dimension bucket for bucket at
    *every* grain, for additive measures as much as for distinct counts.

    A null end column means "still open" (same convention as Dimension.spine),
    and the join is inner: buckets with nothing active drop out rather than
    appearing as zero rows.
    """
    spec = binding.import_spec
    start, end = spec.left_on
    point = spec.right_on[0]
    for column, what in ((start, "interval start"), (end, "interval end")):
        if column not in fact_columns:
            raise QueryError(f"{what} column '{column}' not found in source")
    if point not in bundle_columns:
        raise QueryError(f"'{spec.bundle}' date column '{point}' not found in source")

    # the date table's own point column, normalized to DATE so the comparison
    # lines up whether the table stored dates or timestamps
    dated = (f"(SELECT * EXCLUDE ({_q(point)}), CAST({_q(point)} AS DATE) AS {_q(point)} "
             f"FROM {bundle_sql})")
    right = _period_rows(dated, point, grain)

    on = match_predicate(
        f"CAST({_q(FACT_ALIAS)}.{_q(start)} AS DATE)",
        f"COALESCE(CAST({_q(FACT_ALIAS)}.{_q(end)} AS DATE), DATE '{FAR_FUTURE}')",
        f"{_q(alias)}.{_q(PERIOD_FROM)}", f"{_q(alias)}.{_q(PERIOD_TO)}", spec.match)
    return f" INNER JOIN {right} AS {_q(alias)} ON {on}"


def _relation_schema(from_sql: str) -> dict:
    """Column -> type for a relation expressed as a FROM clause. Metadata only
    (LIMIT 0), and memoized on the SQL text itself — which is exactly what
    determines the answer."""
    return cache.get_or_set(("relation_schema", from_sql), config.SCHEMA_CACHE_TTL,
                            lambda: duck.relation_schema(f"(SELECT * FROM {from_sql})"))


def _model_cache_key(model: Model) -> tuple:
    """Cache-key component identifying this exact Model object, not just its
    name — a model name isn't a stable identity while it's being edited (the
    guided form/YAML editor re-parses a fresh, unregistered Model on every
    keystroke, sometimes reusing an existing model's own name before it's ever
    saved), so anything cached under the name alone could leak a not-yet-saved
    edit's schema to an unrelated caller, or hand the editor back a stale one.
    Folding in id(model) means only repeat calls against the very same object
    ever hit the cache; a fresh reparse always misses and computes its own true
    answer."""
    return (model.name, id(model))


def scan_schema(model: Model, dimensions: Optional[dict] = None) -> dict:
    """Cached column schema for scan(model, dimensions) — same "which joins are
    in play" rules as scan() (see its docstring), just memoized so the
    model/measure editors and every query's own schema lookup don't each
    re-resolve the whole join plan's schema from S3."""
    key = ("scan_schema", _model_cache_key(model),
           None if dimensions is None else tuple(sorted(dimensions)))
    return cache.get_or_set(key, config.SCHEMA_CACHE_TTL,
                            lambda: _relation_schema(scan(model, dimensions)))


def _interval_binding_for(model: Model, dimension: str) -> Optional[ImportBinding]:
    """The interval import owning `dimension`, if any — lets callers that only
    want the imported side's own values read it without the fan-out join."""
    for binding in model.import_bindings:
        if binding.import_spec.is_interval and dimension in binding.dimension_owners:
            return binding
    return None


# ── filters ──────────────────────────────────────────────────────────────

def _resolve_date_value(value: Any) -> date:
    """A filter value as a concrete date: a relative token resolved against
    today, or else a fixed ISO date.

    Raises QueryError — never a bare ValueError — because nothing above the
    engine handles one: it went all the way up as an unhandled exception, which
    on the SSE ask endpoint means a mid-stream ASGI crash with no error event
    for the client."""
    resolved = parse_date_value(value)
    if resolved is None:
        raise QueryError(_bad_date_message(value))
    return resolved


def _coerce(value: Any, dtype: Optional[str]) -> Any:
    """A JSON filter value as the Python object to bind for a column of
    `dtype`. Bound, never interpolated — so this is about making the comparison
    mean the right thing, not about escaping."""
    if value is None:
        return None
    if _is_date(dtype):
        return _resolve_date_value(value)
    if _is_timestamp(dtype):
        relative = resolve_relative_date(value)
        if relative is not None:
            return datetime.combine(relative, datetime.min.time())
        try:    # QueryError, not ValueError — see _resolve_date_value
            return datetime.fromisoformat(str(value))
        except ValueError:
            raise QueryError(_bad_date_message(value)) from None
    base = _base_type(dtype)
    if base in _INTEGER_TYPES:
        return int(value)
    if base in _FLOAT_TYPES or base == "DECIMAL":
        return float(value)
    if base == "BOOLEAN":
        return value in (True, "true", "True", 1)
    return str(value)


def _filter_sql(model: Model, spec: dict, schema: dict, alias: str = "") -> tuple[str, list]:
    """One filter as (predicate SQL, bound values)."""
    dim = model.dimension(spec.get("field", ""))
    op = spec.get("op", "eq")
    if op not in FILTER_OPS:
        raise QueryError(f"unsupported filter op '{op}'")
    dtype = schema.get(dim.column)
    if dtype is None:
        raise QueryError(f"column '{dim.column}' not found in source")
    col = f"{alias}.{_q(dim.column)}" if alias else _q(dim.column)

    if op in ("in", "not_in"):
        values = [_coerce(v, dtype) for v in spec.get("values", [])]
        if not values:
            # an empty IN list matches nothing, which is what the caller asked
            return ("FALSE" if op == "in" else "TRUE"), []
        placeholders = ", ".join("?" for _ in values)
        return f"{col} {'NOT ' if op == 'not_in' else ''}IN ({placeholders})", values
    if op == "contains":
        # case-insensitive regex, as it has always been: the value is a pattern,
        # not a literal substring
        return f"regexp_matches(CAST({col} AS VARCHAR), ?)", ["(?i)" + str(spec.get("value", ""))]
    return f"{col} {_COMPARE_SQL[op]} ?", [_coerce(spec.get("value"), dtype)]


def _referenced_dimensions(query: dict) -> dict:
    """Every dimension name a query touches -> the grain it asked for (None if
    it didn't). Drives which interval imports scan() brings in, and how far it
    thins them — see scan()."""
    names: dict = {}
    for entry in query.get("dimensions", []):
        if isinstance(entry, str):
            entry = {"name": entry}
        if entry.get("name"):
            names[entry["name"]] = entry.get("grain")
    for spec in query.get("filters", []):
        if spec.get("field"):
            names.setdefault(spec["field"], None)
    return names


# ── building one fact table's query ──────────────────────────────────────

class _Build:
    """Accumulates the CTEs and bound values of one statement."""

    def __init__(self) -> None:
        self.ctes: list[tuple[str, str]] = []
        self.params: list = []

    def cte(self, name: str, sql: str, params: Optional[list] = None) -> str:
        self.ctes.append((name, sql))
        if params:
            self.params.extend(params)
        return _q(name)

    def wrap(self, body: str) -> str:
        if not self.ctes:
            return body
        parts = ",\n".join(f"{_q(name)} AS (\n{sql}\n)" for name, sql in self.ctes)
        return f"WITH {parts}\n{body}"


def _dimension_sql(dim: Dimension, grain: Optional[str], schema: dict, alias: str = "") -> str:
    """One dimension as the expression that produces its column. A time
    dimension read through the grain picker is truncated; everything else is
    the column itself under its semantic name.

    date_trunc always returns a TIMESTAMP, whatever it was given, so a DATE
    column is cast back — a day column should not start reporting itself as
    midnight, and the chart axes and the JSON both show the difference."""
    ref = f"{alias}.{_q(dim.column)}" if alias else _q(dim.column)
    if dim.type == "time" and grain:
        truncated = f"date_trunc('{GRAIN_PART[grain]}', {ref})"
        return f"CAST({truncated} AS DATE)" if _is_date(schema.get(dim.column)) else truncated
    return ref


def _group_column(dim: Dimension, grain: Optional[str], schema: dict, emitted: set) -> str:
    """One dimension in the aggregate's select list.

    Ordinarily the model relation already carries it under its semantic name.
    A dimension some framed measure emits is the exception: its raw column was
    left alone there so the `from:` block could read real dates, so the query's
    own bucketing is applied here instead."""
    if dim.name in emitted:
        return f"{_dimension_sql(dim, grain, schema)} AS {_q(dim.name)}"
    return _q(dim.name)


def _spine_cte(build: _Build, model: Model, sdim: Dimension, grain: str,
               lo: date, hi: date) -> str:
    """The generated timeline a spine dimension groups by: one row per bucket
    between `lo` and `hi`, carrying that bucket's span so the same three
    readings of "active in this period" apply here as to a real date table."""
    interval = GRAIN_INTERVAL[grain]
    body = (
        f"SELECT CAST(b AS DATE) AS {_q(sdim.name)}, "
        f"CAST(b AS DATE) AS {_q(PERIOD_FROM)}, "
        f"CAST(b + {interval} - INTERVAL 1 DAY AS DATE) AS {_q(PERIOD_TO)} "
        f"FROM range(CAST(? AS DATE), CAST(? AS DATE) + INTERVAL 1 DAY, {interval}) t(b)"
    )
    return build.cte("__spine", body, [lo, hi])


def _spine_bounds(model: Model, dims_in_play: dict, filters: list, sdim: Dimension,
                  relation: str, where: str, params: list) -> tuple:
    """Cached (min start, max end) over the spine's columns — the natural
    window an unbounded spine query (no explicit date filter) falls back to.

    `relation`/`where` at this point already reflect every filter and optional
    join that shapes the row set, so those — not just the model and spine
    dimension — are exactly what have to key the cache; leaving one out would
    risk serving one query's bounds to a differently-filtered one. The source
    data itself is the one input this can't see change, hence the short TTL
    rather than trusting the entry forever."""
    key = ("spine_bounds", _model_cache_key(model), sdim.name,
           tuple(sorted(dims_in_play)), json.dumps(filters, sort_keys=True, default=str))
    start, end = _spine_columns(sdim, scan_schema(model, dims_in_play))
    sql = (f"SELECT min({start}) AS lo, max({end}) AS hi "
           f"FROM {relation} AS {_q(FACT_ALIAS)}{where}")
    row = cache.get_or_set(key, config.BOUNDS_CACHE_TTL,
                           lambda: _fetch_one(sql, params))
    return row[0], row[1]


def _fetch_one(sql: str, params: list) -> tuple:
    try:
        return duck.cursor().execute(sql, params).fetchone()
    except duckdb.Error as exc:
        raise QueryError(_duck_message(exc)) from exc


def _duck_message(exc: Exception) -> str:
    """A DuckDB error as one line a person can act on. DuckDB appends the
    offending statement and a caret line, which is noise in a JSON error
    field — the first line carries the actual complaint."""
    return str(exc).strip().split("\n")[0]


def _build_single(model: Model, query: dict, row_cap: Optional[int] = None) -> tuple[str, list, list[dict]]:
    """One fact table's query, as (sql, bound values, column metadata).

    This is the whole of the engine's aggregation path. run_query wraps it,
    and _build_parts embeds it once per fact table."""
    resolved_params = resolve_parameter_values(
        query.get("parameters") or [], query.get("parameter_values") or {})
    dims_in_play = _referenced_dimensions(query)
    relation = scan(model, dims_in_play)
    schema = scan_schema(model, dims_in_play)
    build = _Build()

    # ── dimensions, and the one spine among them ──
    dim_entries: list[tuple[Dimension, Optional[str]]] = []
    spine_entry: Optional[tuple[Dimension, str]] = None
    for entry in query.get("dimensions", []):
        if isinstance(entry, str):
            entry = {"name": entry}
        dim = model.dimension(entry["name"])
        grain = entry.get("grain")
        if grain and grain not in TIME_GRAINS:
            raise QueryError(f"unsupported grain '{grain}'")
        if dim.spine:
            if spine_entry:
                raise QueryError("only one spine dimension per query")
            spine_entry = (dim, grain or "1mo")
        dim_entries.append((dim, grain))

    # ── filters: plain ones narrow the scan, spine ones also bound the timeline ──
    where_parts: list[str] = []
    where_params: list = []
    spine_lo = spine_hi = None
    spine_dims: dict[str, Dimension] = {}
    for spec in query.get("filters", []):
        dim = model.dimension(spec.get("field", ""))
        if not dim.spine:
            sql, values = _filter_sql(model, spec, schema, _q(FACT_ALIAS))
            where_parts.append(sql)
            where_params.extend(values)
            continue
        spine_dims[dim.name] = dim
        op = spec.get("op", "eq")
        value = _resolve_date_value(spec.get("value"))
        start, end = _spine_columns(dim, schema)
        if op in ("gte", "gt"):
            where_parts.append(f"{end} >= ?")
            where_params.append(value)
            spine_lo = max(spine_lo, value) if spine_lo else value
        elif op in ("lte", "lt"):
            where_parts.append(f"{start} <= ?")
            where_params.append(value)
            spine_hi = min(spine_hi, value) if spine_hi else value
        elif op == "eq":
            where_parts.append(f"{start} <= ? AND {end} >= ?")
            where_params.extend([value, value])
            spine_lo = spine_hi = value
        else:
            raise QueryError(
                f"filter op '{op}' not supported on spine dimension '{dim.name}'")
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # Measures are split before the model relation is built because a framed
    # measure's `emits:` decides what that relation may overwrite — see
    # `emitted` below.
    plain, window_specs, framed = _split_measures(
        model, query, schema, resolved_params, dim_entries)
    # Dimensions a `from:` block computes itself. Their *raw* source column has
    # to reach the block untouched: materializing the query's bucketing over it
    # first would hand the block dates already rounded to the month, and every
    # interval it derived from them would be wrong rather than merely coarse.
    emitted = {name for _, _, emits, _ in framed for name in emits}

    # ── the model relation: filtered rows, dimension columns materialized ──
    # This is what a `from:` block reads as {model}, so it carries both the raw
    # source columns and the query's dimensions under their semantic names.
    if spine_entry:
        sdim, grain = spine_entry
        lo, hi = spine_lo, spine_hi
        if lo is None or hi is None:
            data_lo, data_hi = _spine_bounds(
                model, dims_in_play, query.get("filters") or [], sdim,
                relation, where, where_params)
            lo = lo or _as_python_date(data_lo)
            hi = hi or min(_as_python_date(data_hi) or date.today(), date.today())
        if lo is None or hi < lo:
            raise QueryError("no rows in the timeline window")
        lo = _truncate(lo, grain)
        spine = _spine_cte(build, model, sdim, grain, lo, hi)
        start, end = _spine_columns(sdim, schema)
        on = match_predicate(start, end,
                             f"{spine}.{_q(PERIOD_FROM)}", f"{spine}.{_q(PERIOD_TO)}",
                             sdim.spine.match)
        from_sql = (f"FROM {relation} AS {_q(FACT_ALIAS)} "
                    f"INNER JOIN {spine} ON {on}{where}")
        extra_select = [f"{spine}.{_q(sdim.name)}"]
    else:
        from_sql = f"FROM {relation} AS {_q(FACT_ALIAS)}{where}"
        extra_select = []

    materialized, excluded = [], []
    for dim, grain in dim_entries:
        if dim.spine or dim.name in emitted:
            continue        # the timeline supplies one; a from: block the other
        expr = _dimension_sql(dim, grain, schema, _q(FACT_ALIAS))
        if expr == f"{_q(FACT_ALIAS)}.{_q(dim.column)}" and dim.name == dim.column:
            continue        # already there under the right name
        materialized.append(f"{expr} AS {_q(dim.name)}")
        if dim.name in schema:
            excluded.append(dim.name)
    exclude = f" EXCLUDE ({', '.join(_q(c) for c in excluded)})" if excluded else ""
    select = ", ".join([
        *extra_select,
        f"{_q(FACT_ALIAS)}.*{exclude}",
        *materialized,
        f"TRUE AS {_q(GROUP_CONST)}",
    ])
    model_cte = build.cte(MODEL_RELATION, f"SELECT {select} {from_sql}", where_params)

    # ── measures ──
    dim_names = [dim.name for dim, _ in dim_entries]

    group_select = [_group_column(dim, grain, schema, emitted) for dim, grain in dim_entries]
    for dim, _ in dim_entries:
        if dim.geo:
            group_select.append(f"avg({_q(dim.geo.lat)}) AS {_q('__lat_' + dim.name)}")
            group_select.append(f"avg({_q(dim.geo.lon)}) AS {_q('__lon_' + dim.name)}")
    group_select.extend(f"{sql} AS {_q(name)}" for name, sql in plain)

    if plain or not framed:
        if not plain:
            raise QueryError("query needs at least one measure")
        agg = build.cte("__agg", f"SELECT {', '.join(group_select)} FROM {model_cte} GROUP BY ALL")
    else:
        # every measure framed: the derived relations alone define which
        # dimension groups exist (an emitted timeline shouldn't inherit raw-row
        # buckets), so there is no aggregate over the fact rows to start from
        agg = None

    result = agg
    if window_specs:
        result = _window_cte(build, model, agg, dim_entries, dim_names,
                             window_specs, resolved_params)
    if framed:
        result = _framed_ctes(build, model, model_cte, result, dim_entries,
                              dim_names, framed, schema, resolved_params)

    measure_names = list(query.get("measures") or [])
    columns = _columns(model, dim_entries, measure_names, query)
    geo_cols = [f"__{side}_{dim.name}" for dim, _ in dim_entries if dim.geo
                for side in ("lat", "lon")]
    projection = ", ".join(_q(c) for c in [*dim_names, *geo_cols, *measure_names])
    order = _order_by(query, dim_entries, measure_names)
    limit = min(int(query.get("limit") or 1000), row_cap or config.MAX_ROWS)
    body = f"SELECT {projection} FROM {result}{order} LIMIT {int(limit)}"
    return build.wrap(body), build.params, columns
# Visual parameter types (specs/010-parameter-type-generalization). An
# absent "type" field on a declaration always means "int" — the type spec
# 009 shipped exclusively — so every visual/dashboard saved before this
# feature existed keeps working unchanged (FR-004).
PARAM_TYPES = {"int", "float", "string"}


def param_type_ok(value: object, type_name: str) -> bool:
    """Is `value` (a JSON-decoded Python object) a legitimate member of
    declared type `type_name`? "float" deliberately also accepts a genuine
    Python int: JSON (and JavaScript, which has one numeric type) cannot
    distinguish a whole float from an int syntactically, so a float
    parameter's declared values/default routinely arrive as JSON integers
    from a well-behaved frontend — see specs/010-parameter-type-
    generalization/research.md §5. "int" does NOT accept a float in the
    other direction: declared type governs eligibility, not incidental
    JSON shape."""
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    raise QueryError(f"unsupported parameter type '{type_name}' (expected one of {sorted(PARAM_TYPES)})")


def coerce_param_value(value: object, type_name: str):
    """Canonicalize a value already known to pass param_type_ok(value,
    type_name) into type_name's one true Python representation — in
    particular, a "float" parameter's value is always a genuine Python
    float afterward, never an int that merely happens to be whole. Every
    value handed to the SQL grammar as a param() substitution, and every
    value compared for dashboard definition-equality, passes through this
    first, so a LAG() offset check (which requires a real int) is never
    fooled by an int-shaped JSON float."""
    return float(value) if type_name == "float" else value


def resolve_parameter_values(parameters: list, parameter_values: dict) -> dict:
    """Validate a query's declared parameters and the caller's selected
    values, returning {name: value} with every declared parameter present
    — the caller's pick where given and in-list, else that parameter's
    declared default, each coerced to its declared (or implicit int) type.
    This is the only allowlist-membership check a parameter value ever
    passes through; the result is the only thing the SQL grammar's param()
    measure ever sees (see its parameter_values arg)."""
    declared: dict[str, dict] = {}
    for p in parameters or []:
        name = p.get("name")
        values = p.get("values") or []
        default = p.get("default")
        type_name = p.get("type") or "int"
        if not name:
            raise QueryError("parameter needs a name")
        if name in declared:
            raise QueryError(f"duplicate parameter '{name}'")
        if type_name not in PARAM_TYPES:
            raise QueryError(f"parameter '{name}' has unsupported type '{type_name}' (expected one of {sorted(PARAM_TYPES)})")
        if not values:
            raise QueryError(f"parameter '{name}' needs a non-empty list of values")
        bad = [v for v in values if not param_type_ok(v, type_name)]
        if bad:
            raise QueryError(f"parameter '{name}': value {bad[0]!r} does not match declared type '{type_name}'")
        coerced_values = {coerce_param_value(v, type_name) for v in values}
        if not param_type_ok(default, type_name) or coerce_param_value(default, type_name) not in coerced_values:
            raise QueryError(f"parameter '{name}' default {default!r} is not one of its declared values")
        declared[name] = {"type": type_name, "values": coerced_values, "default": coerce_param_value(default, type_name)}
    resolved = {name: decl["default"] for name, decl in declared.items()}
    for name, value in (parameter_values or {}).items():
        if name not in declared:
            raise QueryError(f"unknown parameter '{name}'")
        decl = declared[name]
        if not param_type_ok(value, decl["type"]) or coerce_param_value(value, decl["type"]) not in decl["values"]:
            raise QueryError(f"value {value!r} is not a declared value of parameter '{name}'")
        resolved[name] = coerce_param_value(value, decl["type"])
    return resolved



def _spine_columns(dim: Dimension, schema: dict) -> tuple[str, str]:
    """A spine dimension's interval columns, normalized: datetimes cast to
    dates so the comparison against a generated day-grain timeline lines up,
    and a null end read as open-ended (still active)."""
    refs = []
    for column in (dim.spine.start, dim.spine.end):
        if column not in schema:
            raise QueryError(f"spine column '{column}' not found in source")
        ref = f"{_q(FACT_ALIAS)}.{_q(column)}"
        refs.append(f"CAST({ref} AS DATE)" if _is_timestamp(schema.get(column)) else ref)
    return refs[0], f"COALESCE({refs[1]}, DATE '{FAR_FUTURE}')"


def _as_python_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _truncate(value: date, grain: str) -> date:
    """A date at the start of its `grain` bucket — the same boundaries
    date_trunc uses, computed here because the timeline's lower bound is a
    bound parameter rather than something in the query."""
    if grain == "1w":
        return value - timedelta(days=value.weekday())
    if grain == "1mo":
        return value.replace(day=1)
    if grain == "1q":
        return _start_of_quarter(value)
    if grain == "1y":
        return value.replace(month=1, day=1)
    return value


# ── measures ─────────────────────────────────────────────────────────────

def _measure_schema(model: Model, schema: dict, dim_entries: list) -> dict:
    """What a measure expression may name: the fact scan's own columns, plus
    the query's dimensions under their semantic names (which is how a measure
    referencing an imported dimension works), plus the constant grouping
    column."""
    out = dict(schema)
    for dim, _ in dim_entries:
        out.setdefault(dim.name, "VARCHAR")
    out[GROUP_CONST] = "BOOLEAN"
    return out


def _split_measures(model: Model, query: dict, schema: dict, resolved_params: dict,
                    dim_entries: Optional[list] = None) -> tuple:
    """The query's measures sorted into the three ways they are computed:

      - **plain** aggregates, applied in one GROUP BY over the fact scan;
      - **framed** measures, whose expression aggregates a `from:` relation
        instead;
      - **window** measures, computed after the group-by because they read
        sibling measures' already-aggregated values — "the previous quarter"
        only means something once the data has been grouped down to one row
        per quarter.
    """
    measure_schema = _measure_schema(model, schema, dim_entries or [])
    inline: dict[str, dict] = {}
    for m in query.get("inline_measures") or []:
        if not m.get("name") or not m.get("expr"):
            raise QueryError("inline measures need a name and an expr")
        if m.get("frame") or m.get("frame_emits"):
            raise QueryError(
                f"measure '{m['name']}': 'frame:' was the python intermediary-frame "
                "construct and is gone — write the same step as SQL under 'from:'")
        inline[m["name"]] = m

    plain: list[tuple[str, str]] = []
    plain_names: set = set()
    framed: list[tuple[str, str, set, str]] = []
    window_specs: list[tuple[str, str]] = []
    measure_names = list(query.get("measures") or [])
    if not measure_names:
        raise QueryError("query needs at least one measure")

    def add_plain(name: str, text: str) -> None:
        if name in plain_names:
            return
        plain_names.add(name)
        plain.append((name, _compile(name, text, measure_schema, resolved_params)))

    def resolve(name: str, *, is_dependency: bool) -> None:
        spec = inline.get(name)
        text = spec["expr"] if spec else None
        from_source = spec.get("from") if spec else None
        emits: set = set(spec.get("emits") or []) if spec else set()
        if spec is None:
            meas = model.measure(name)
            text, from_source, emits = meas.expr_source, meas.from_source, set(meas.emits)
        if from_source:
            if is_dependency:
                raise QueryError(
                    f"measure '{name}' aggregates a from: relation and can't be used as "
                    "another window measure's dependency")
            framed.append((name, from_source, emits, text))
            return
        if sqlgrammar.is_window_expr(text):
            if is_dependency:
                raise QueryError(
                    f"measure '{name}' is itself a window measure and can't be used as "
                    "another window measure's dependency")
            window_specs.append((name, text))
            return
        add_plain(name, text)

    try:
        for name in measure_names:
            resolve(name, is_dependency=False)
        # a window measure's sibling references (e.g. SUM(revenue) OVER w) must
        # be computed even if the caller didn't request them directly — they're
        # trimmed from the final projection below if so
        for _, text in window_specs:
            for dep in sqlgrammar.referenced_names(text):
                if dep not in plain_names and (dep in inline or dep in model.measures):
                    resolve(dep, is_dependency=True)
    except ModelError as exc:
        raise QueryError(str(exc)) from exc
    return plain, window_specs, framed


def _compile(name: str, text: str, schema: Optional[dict], resolved_params: dict,
             *, window_spec: str = "") -> str:
    try:
        return sqlgrammar.compile_expression(
            text, schema, window=bool(window_spec), window_spec=window_spec,
            parameter_values=resolved_params)
    except sqlgrammar.SqlCompileError as exc:
        raise QueryError(f"measure '{name}': {exc}") from exc


def _window_cte(build: _Build, model: Model, agg: Optional[str], dim_entries: list,
                dim_names: list, window_specs: list, resolved_params: dict) -> str:
    """Window measures, applied over the aggregated result.

    The engine supplies the window: partitioned by the query's other
    dimensions, ordered by its time dimension — so adding a breakout dimension
    gives each of its members an independent running total, and the grain the
    query asked for is what "the previous period" means. sqlgrammar resolves
    `OVER w` against that definition at parse time, so what lands here already
    carries the whole window."""
    if agg is None:
        raise QueryError(
            "window measures need at least one plain aggregate measure in the query "
            "to compute over")
    time_dims = [dim.name for dim, _ in dim_entries if dim.type == "time"]
    if not time_dims:
        raise QueryError(
            "window measures require a time dimension in the query's dimensions "
            "to order by")
    if len(time_dims) > 1:
        raise QueryError(
            "window measures support only one time dimension per query — "
            "ambiguous ordering")
    order_dim = time_dims[0]
    partition = [name for name in dim_names if name != order_dim]
    spec = " ".join(filter(None, [
        f"PARTITION BY {', '.join(_q(c) for c in partition)}" if partition else "",
        f"ORDER BY {_q(order_dim)}",
    ]))
    exprs = [f"{_compile(name, text, None, resolved_params, window_spec=spec)} AS {_q(name)}"
             for name, text in window_specs]
    return build.cte("__win", f"SELECT *, {', '.join(exprs)} FROM {agg}")


def _framed_ctes(build: _Build, model: Model, model_cte: str, result: Optional[str],
                 dim_entries: list, dim_names: list, framed: list,
                 schema: dict, resolved_params: dict) -> str:
    """One CTE per `from:` measure, joined back onto the other measures.

    Each block runs against the filtered scan with the query's dimension
    columns already materialized, and its expression then aggregates the
    derived rows per dimension group. Dimensions in the measure's `emits:` are
    the block's own output columns (a per-entity milestone date, say): they're
    withheld from `{dims}` during the step and bucketed on the derived rows
    afterwards, so a timeline groups what the block produced, not the raw
    events feeding it.
    """
    grain_of = {dim.name: grain for dim, grain in dim_entries}
    dim_by_name = {dim.name: dim for dim, _ in dim_entries}
    time_dims = {dim.name for dim, _ in dim_entries if dim.type == "time"}
    for index, (name, from_source, emits, expr_text) in enumerate(framed):
        emitted = [d for d in dim_names if d in emits]
        carried = [d for d in dim_names if d not in emits]
        try:
            rendered = sqlgrammar.compile_relation(
                render_from_block(from_source, [*carried, GROUP_CONST]),
                allowed_tables={MODEL_RELATION})
        except sqlgrammar.SqlCompileError as exc:
            raise QueryError(f"measure '{name}': {exc}") from exc
        derived_schema = _probe_schema(build, rendered)
        missing = [d for d in carried if d not in derived_schema]
        if missing:
            raise QueryError(
                f"measure '{name}': the from: relation lost dimension column(s) {missing} — "
                f"carry the query's dimensions through with {{dims}} (e.g. "
                f"SELECT {{dims}}, … and GROUP BY {{dims}}, …), or list a dimension in the "
                f"measure's emits: and output it from the block")
        missing_emitted = [d for d in emitted if d not in derived_schema]
        if missing_emitted:
            raise QueryError(
                f"measure '{name}': emits: names {missing_emitted}, which the from: "
                f"relation does not output")
        select = [_q(d) for d in carried]
        for d in emitted:
            if d in time_dims and grain_of.get(d):
                # the block chose the emitted column's type; the dimension's
                # own source column decides what the query reports, so a day
                # column stays a date rather than becoming midnight
                trunc = f"date_trunc('{GRAIN_PART[grain_of[d]]}', {_q(d)})"
                if _is_date(schema.get(dim_by_name[d].column)):
                    trunc = f"CAST({trunc} AS DATE)"
                select.append(f"{trunc} AS {_q(d)}")
            else:
                select.append(_q(d))
        select.append(f"{_compile(name, expr_text, derived_schema, resolved_params)} AS {_q(name)}")
        part = build.cte(f"__m{index}", f"SELECT {', '.join(select)} FROM ({rendered}) GROUP BY ALL")
        if result is None:
            result = part
        elif dim_names:
            # a group present on either side keeps its row: carried dims make
            # the framed side a subset, but an emitted dimension can surface
            # groups the raw rows never form
            on = " AND ".join(
                f"{result}.{_q(d)} IS NOT DISTINCT FROM {part}.{_q(d)}" for d in dim_names)
            merged = ", ".join([
                *(f"COALESCE({result}.{_q(d)}, {part}.{_q(d)}) AS {_q(d)}" for d in dim_names),
                f"{result}.* EXCLUDE ({', '.join(_q(d) for d in dim_names)})",
                f"{part}.{_q(name)}",
            ])
            result = build.cte(
                f"__j{index}",
                f"SELECT {merged} FROM {result} FULL OUTER JOIN {part} ON {on}")
        else:
            result = build.cte(f"__j{index}", f"SELECT * FROM {result}, {part}")
    return result


def _probe_schema(build: _Build, rendered: str) -> dict:
    """The columns a `from:` relation produces, read from its metadata alone.

    Costs one LIMIT 0 against the CTEs built so far — the same thing the polars
    engine's collect_schema() did, and what turns "column not found" three
    layers down into a message naming the dimension the block dropped."""
    sql = build.wrap(f"SELECT * FROM ({rendered}) LIMIT 0")
    try:
        rel = duck.cursor().execute(sql, list(build.params))
        return {name: str(dtype) for name, dtype in
                zip([d[0] for d in rel.description], [d[1] for d in rel.description])}
    except duckdb.Error as exc:
        raise QueryError(f"invalid from: relation: {_duck_message(exc)}") from exc


def _order_by(query: dict, dim_entries: list, measure_names: list) -> str:
    """The ORDER BY clause. An explicit sort if it names a valid key, else the
    deterministic default: time ascending if a time dimension is present, else
    the first measure descending."""
    sort = query.get("sort") or {}
    dim_names = [dim.name for dim, _ in dim_entries]
    by = sort.get("by")
    if by and by in set(dim_names) | set(measure_names):
        direction = "DESC" if sort.get("desc", True) else "ASC"
        return f" ORDER BY {_q(by)} {direction} NULLS LAST"
    if not dim_entries:
        return ""
    time_dims = [dim.name for dim, _ in dim_entries if dim.type == "time"]
    if time_dims:
        return f" ORDER BY {_q(time_dims[0])} NULLS LAST"
    return f" ORDER BY {_q(measure_names[0])} DESC NULLS LAST"


def _columns(model: Model, dim_entries: list, measure_names: list, query: dict) -> list[dict]:
    inline = {m["name"]: m for m in (query.get("inline_measures") or []) if m.get("name")}

    def measure_meta(name: str) -> dict:
        if name in inline:
            return {"name": name, "label": inline[name].get("label") or name,
                    "kind": "measure", "format": inline[name].get("format") or "number",
                    "inline": True}
        meas = model.measure(name)
        return {"name": name, "label": meas.label, "kind": "measure", "format": meas.format}

    return [
        {"name": dim.name, "label": dim.label, "kind": "dimension", "type": dim.type}
        for dim, _ in dim_entries
    ] + [measure_meta(name) for name in measure_names]


# ─────────────────────────────────────────────────────────────────────────
# Several fact tables in one model: "drill across" the dimensions they share.
#
# The parts are never joined to each other. Each is aggregated on its own at
# the same grain, over the dimensions they all share, and the *results* are
# merged on those dimension columns afterwards. Joining the fact tables
# directly — even via a shared date — would give every row of one fact a copy
# of every matching row of the others and multiply both sides' measures;
# aggregating separately and merging the aggregates is the only shape that
# keeps each measure's grain intact.
#
# What changed with DuckDB is that this is now one statement rather than one
# query per part plus a merge in Python: each part is a CTE, and the merge is a
# FULL OUTER JOIN the planner sees along with everything feeding it.
#
# A bucket only one part has rows for keeps its row and leaves the other parts'
# measures null. That is deliberate: null reads as "this fact table has nothing
# here", which is what happened, whereas zero would be a number nobody
# measured. Charts draw it as a gap.
# ─────────────────────────────────────────────────────────────────────────

def _not_shared(parts: list, shared: dict, name: str, what: str) -> QueryError:
    """Why `name` can't be used across the fact tables this query reads, naming
    the one that lacks it — the actionable half is which measure to drop."""
    missing = [p.name for p in parts if name not in p.model.dimensions]
    culprit = (
        f"'{missing[0]}' doesn't offer it" if len(missing) == 1
        else f"{', '.join(repr(a) for a in missing)} don't offer it"
    )
    return QueryError(
        f"'{name}' is not shared by the fact tables this query reads "
        f"({', '.join(p.name for p in parts)}) — {culprit}, so there is no honest value to put in "
        f"its column. {what} across these fact tables: {', '.join(shared) or 'none'}"
    )


def _merged_dimensions(query: dict, parts: list, shared: dict) -> list[tuple[str, Optional[str]]]:
    """The query's requested dimensions as (shared name, grain), validated
    against `shared` — the dimensions common to the fact tables this query
    actually reads, which is a superset of the model's all-parts catalog."""
    entries: list[tuple[str, Optional[str]]] = []
    for entry in query.get("dimensions") or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        name = entry.get("name")
        if name not in shared:
            raise _not_shared(parts, shared, name, "Groupable")
        grain = entry.get("grain")
        if grain and grain not in TIME_GRAINS:
            raise QueryError(f"unsupported grain '{grain}'")
        entries.append((name, grain))
    return entries


def _build_parts(model: Model, query: dict, row_cap: Optional[int] = None) -> tuple[str, list, list[dict]]:
    """A model holding several unrelated fact tables, as one statement: a CTE
    per part that contributes a measure, merged on the shared dimensions."""
    if query.get("inline_measures"):
        raise QueryError(
            f"'{model.name}' holds several unrelated fact tables and doesn't take inline "
            f"measures — an expression has to be scoped to one of them, so declare it on the "
            f"dataset it belongs to")
    measure_names = list(query.get("measures") or [])
    if not measure_names:
        raise QueryError("query needs at least one measure")

    wanted: dict[str, list[str]] = {}   # part name -> the measures asked of it
    for name in measure_names:
        try:
            owner = semantic.part_for_measure(model, name)
        except ModelError as exc:
            raise QueryError(str(exc)) from exc
        wanted.setdefault(owner.name, []).append(name)

    # only the fact tables this query names a measure from are read, so only
    # those have to conform: a dimension the others lack never reaches a row
    parts = [p for p in model.parts if wanted.get(p.name)]
    shared = semantic.shared_dimensions(parts)

    dim_entries = _merged_dimensions(query, parts, shared)
    dim_names = [name for name, _ in dim_entries]

    filters = list(query.get("filters") or [])
    for spec in filters:
        if spec.get("field") not in shared:
            raise _not_shared(parts, shared, spec.get("field"), "Filterable")

    build = _Build()
    merged: Optional[str] = None
    for index, part in enumerate(parts):
        own = wanted[part.name]
        sql, params, _ = _build_single(part.model, {
            "dimensions": [{"name": name, "grain": grain} for name, grain in dim_entries],
            "measures": own,
            "filters": filters,
            # merge first, then sort and cut: a per-part limit would drop
            # buckets another fact table still has rows for
            "limit": row_cap or config.MAX_ROWS,
            "parameters": query.get("parameters"),
            "parameter_values": query.get("parameter_values"),
        }, row_cap)
        # two fact tables can store the same shared dimension differently — a
        # timestamp meets a date — so the merge keys are normalized before they
        # are compared, exactly as the polars merge cast them
        keys = [f"CAST({_q(name)} AS DATE) AS {_q(name)}"
                if shared[name].type == "time" else _q(name)
                for name in dim_names]
        projection = ", ".join([*keys, *(_q(m) for m in own)])
        alias = build.cte(f"__p{index}", f"SELECT {projection} FROM ({sql})", params)
        if merged is None:
            merged = alias
            continue
        if dim_names:
            on = " AND ".join(
                f"{merged}.{_q(d)} IS NOT DISTINCT FROM {alias}.{_q(d)}" for d in dim_names)
            select = ", ".join([
                *(f"COALESCE({merged}.{_q(d)}, {alias}.{_q(d)}) AS {_q(d)}" for d in dim_names),
                f"{merged}.* EXCLUDE ({', '.join(_q(d) for d in dim_names)})",
                *(f"{alias}.{_q(m)}" for m in own),
            ])
            merged = build.cte(f"__mg{index}",
                               f"SELECT {select} FROM {merged} FULL OUTER JOIN {alias} ON {on}")
        else:   # grand totals: one row each side
            merged = build.cte(f"__mg{index}", f"SELECT * FROM {merged}, {alias}")

    order = _order_by(query, [(shared[name], grain) for name, grain in dim_entries],
                      measure_names)
    limit = min(int(query.get("limit") or 1000), row_cap or config.MAX_ROWS)
    projection = ", ".join(_q(c) for c in [*dim_names, *measure_names])
    body = f"SELECT {projection} FROM {merged}{order} LIMIT {int(limit)}"

    owner_of = {m: p.name for p in parts for m in wanted[p.name]}
    columns = [
        {"name": name, "label": shared[name].label, "kind": "dimension",
         "type": shared[name].type}
        for name in dim_names
    ] + [
        {"name": m, "label": model.measure(m).label, "kind": "measure",
         "format": model.measure(m).format, "fact": owner_of[m]}
        for m in measure_names
    ]
    return build.wrap(body), build.params, columns


# ── running a query ──────────────────────────────────────────────────────

def build_sql(model: Model, query: dict, row_cap: Optional[int] = None) -> tuple[str, list, list[dict]]:
    """The statement one semantic query compiles to, as (sql, bound values,
    column metadata) — without running it.

    Public because the statement is the engine's real output and worth being
    able to look at: tests assert its shape, and it is what a "show me the SQL"
    affordance would print."""
    builder = _build_parts if model.is_composite else _build_single
    return builder(model, query, row_cap)


def run_query_arrow(model: Model, query: dict, row_cap: Optional[int] = None):
    """run_query's body, stopping one step short of JSON: the Arrow table, its
    column metadata, and how long the whole thing took.

    Split out for the instant-extract path (specs/016-instant-cross-filter/),
    which serializes the same table as Arrow IPC. `row_cap` raises the default
    MAX_ROWS ceiling for that one caller — a keyword argument rather than a
    query field precisely so no HTTP request can lift its own limit."""
    started = time.perf_counter()
    sql, params, columns = build_sql(model, query, row_cap)
    try:
        table = duck.cursor().execute(sql, params).to_arrow_table()
    except duckdb.Error as exc:
        raise QueryError(_duck_message(exc)) from exc
    return table, columns, round((time.perf_counter() - started) * 1000, 1)


def run_query(model: Model, query: dict) -> dict:
    """Execute a semantic query.

    query = {
      dimensions: ["region", {"name": "order_date", "grain": "1mo"}],
      measures: ["revenue"],
      filters: [{"field": "region", "op": "in", "values": [...]}],
      sort: {"by": "revenue", "desc": true} | null,
      limit: 500
    }

    Spine dimensions (dimension.spine = {start, end}) group point-in-time: a
    generated timeline at the requested grain is interval-joined against the
    start/end columns, so each row counts in every bucket it was active for. A
    `how: between` dimension import answers the same kind of question from a
    real date table instead — see scan() and _interval_join.

    A model holding several unrelated fact tables has no source of its own: the
    same query is answered by aggregating each part in its own CTE and merging
    the results on the dimensions they share — see _build_parts.
    """
    table, columns, elapsed_ms = run_query_arrow(model, query)
    rows = [{k: _json_safe(v) for k, v in row.items()} for row in table.to_pylist()]
    return {"columns": columns, "rows": rows, "row_count": table.num_rows,
            "elapsed_ms": elapsed_ms}


def dimension_values(model: Model, dimension: str, limit: int = 100) -> list:
    dim = model.dimension(dimension)
    if model.is_composite:
        # a shared dimension means the same thing to every fact table, so any of
        # them can supply its members; take the first that isn't a generated
        # timeline
        for part in model.parts:
            if part.model.dimension(dimension).spine:
                continue
            return dimension_values(part.model, dimension, limit)
        raise QueryError(
            f"'{dimension}' is a generated timeline; filter it with date ranges instead")
    if dim.spine:
        raise QueryError(
            f"'{dimension}' is a generated timeline; filter it with date ranges instead")
    # a dimension from an interval import has its own values independent of the
    # model's rows — read them off the imported side; anything else needs no
    # interval join at all, hence the empty dimension set
    interval = _interval_binding_for(model, dimension)
    if interval:
        relation, columns = _scan_bundle(interval)
        available = set(columns)
    else:
        relation = scan(model, {})
        available = set(scan_schema(model, {}))
    if dim.column not in available:
        raise QueryError(f"column '{dim.column}' not found in source")
    sql = (f"SELECT DISTINCT {_q(dim.column)} AS {_q(dim.name)} FROM {relation} "
           f"ORDER BY 1 LIMIT {int(limit)}")
    try:
        rows = duck.cursor().execute(sql).fetchall()
    except duckdb.Error as exc:
        raise QueryError(_duck_message(exc)) from exc
    return [_json_safe(row[0]) for row in rows]
