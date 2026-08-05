"""Query engine: turns a semantic-layer query into a lazy polars scan over S3.

Nothing is downloaded eagerly — scan_parquet/scan_csv against the object store
lets polars push projections and predicates down, so only the columns and row
groups a query needs leave the (emulated) bucket.
"""
from __future__ import annotations

import calendar
import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

import polars as pl

from . import cache, config, iceberg_util, measure_dsl, semantic
from .semantic import Dimension, ImportBinding, Model, ModelError, Source, TIME_GRAINS, compile_frame

FILTER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains"}

# stand-in for a null interval end ("still open"), shared by spine dimensions
# and `how: between` dimension imports
FAR_FUTURE = date(9999, 1, 1)

_COMPARE_OPS = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
}

# ── dynamic ("relative") date filter values ──────────────────────
# A time filter's value may be a keyword like "today" or "start_of_month"
# instead of a fixed ISO date. It's resolved against the current date on
# every query, so a saved "today" keeps meaning today on every future run.

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

_RELATIVE_OFFSET_RE = re.compile(r"^today([+-])(\d+)(d|w|mo|y)$")


def _add_months(d: date, n: int) -> date:
    month0 = d.month - 1 + n
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def resolve_relative_date(value: Any, today: Optional[date] = None) -> Optional[date]:
    """Resolve a relative-date keyword to a concrete date, or return None if
    `value` isn't one (the caller then falls back to parsing a fixed date)."""
    key = str(value).strip().lower()
    keyword = RELATIVE_DATE_KEYWORDS.get(key)
    if keyword:
        return keyword(today or date.today())
    m = _RELATIVE_OFFSET_RE.match(key)
    if not m:
        return None
    sign, n, unit = m.group(1), int(m.group(2)), m.group(3)
    n = n if sign == "+" else -n
    base = today or date.today()
    if unit == "d":
        return base + timedelta(days=n)
    if unit == "w":
        return base + timedelta(weeks=n)
    if unit == "mo":
        return _add_months(base, n)
    return _add_months(base, n * 12)  # "y"


class QueryError(Exception):
    pass


def _scan_source(source: Source) -> pl.LazyFrame:
    opts = config.storage_options()
    if source.format == "csv":
        return pl.scan_csv(source.path, storage_options=opts)
    if source.format == "delta":
        return pl.scan_delta(source.path, storage_options=opts)
    if source.format == "iceberg":
        return iceberg_util.scan(source.path)
    return pl.scan_parquet(source.path, storage_options=opts)


def scan_source(source: Source) -> pl.LazyFrame:
    """Public: lazily scan a single source (no joins). Used by the dimension
    bundle editor to introspect one dataset's own columns."""
    return _scan_source(source)


def source_schema(source: Source) -> pl.Schema:
    """Cached column schema for one source (its own, unjoined) — the
    footer-only read source introspection needs (dataset picker, dimension
    bundle editor), without re-hitting S3 for it on every keystroke. Keyed
    on the source's own path+format, exactly what determines the answer, so
    it's safe to share across every caller, mid-edit or not — unlike a
    model or bundle name, a source path never means two different things at
    once."""
    key = ("source_schema", source.path, source.format)
    return cache.get_or_set(key, config.SCHEMA_CACHE_TTL, lambda: _scan_source(source).collect_schema())


def _scan_bundle(binding: ImportBinding) -> pl.LazyFrame:
    """Build one imported dimension bundle's combined lazy frame: scan the
    anchor dataset, then join in every other dataset the import resolved as
    reachable, via the bundle's own declared DatasetJoins. Each join is
    applied with the already-accumulated side as polars' left operand and
    `how` taken from the edge as declared — so an import always preserves
    the anchor (and anything already pulled in) in full, gaining nullable
    columns for anything only reachable in the reverse of how the bundle's
    author happened to declare that particular edge."""
    bundle = binding.bundle
    included = set(binding.included_datasets)

    edge_by_pair: dict[tuple[str, str], object] = {}
    for ds in bundle.datasets.values():
        for j in ds.joins:
            edge_by_pair[(ds.name, j.to)] = j

    anchor = binding.import_spec.anchor_dataset
    lf = _scan_source(bundle.datasets[anchor].source)
    joined = {anchor}
    remaining = included - joined
    while remaining:
        progressed = False
        for ds_name in list(remaining):
            edge, reversed_edge = None, False
            for joined_name in joined:
                if (joined_name, ds_name) in edge_by_pair:
                    edge = edge_by_pair[(joined_name, ds_name)]
                    break
                if (ds_name, joined_name) in edge_by_pair:
                    edge, reversed_edge = edge_by_pair[(ds_name, joined_name)], True
                    break
            if edge is None:
                continue
            left_on, right_on = (edge.right_on, edge.left_on) if reversed_edge else (edge.left_on, edge.right_on)
            # coalesce=False: a differently-named right_on key (e.g. a bridge
            # table's own id column) can itself be a declared Dimension of
            # ds_name — polars' default coalescing silently drops it otherwise
            lf = lf.join(
                _scan_source(bundle.datasets[ds_name].source),
                left_on=left_on, right_on=right_on, how=edge.how, coalesce=False,
            )
            joined.add(ds_name)
            remaining.discard(ds_name)
            progressed = True
        if not progressed:
            # _resolve_part_imports() computes `included` via the same reachability
            # rules, so everything in it must connect back to the anchor
            raise ModelError(
                f"dimension bundle '{bundle.name}': internal error resolving join "
                f"order for datasets {sorted(remaining)}"
            )

    # Project down before handing the frame to the importer: every included
    # dimension under its *dimension* name, plus the raw columns the engine
    # still addresses positionally — geo coordinate pairs, and the import's own
    # join key. A bundle's raw column names never reach the fact model's
    # namespace, so a model whose source has a `month` of its own can import a
    # calendar that also has one; before this, polars suffixed the collision to
    # `month_right` and the calendar's dimension quietly read the fact's column.
    exprs: list[pl.Expr] = []
    taken: set[str] = set()

    def keep(column: str, alias: str) -> None:
        if alias not in taken:
            taken.add(alias)
            exprs.append(pl.col(column).alias(alias))

    for ds_name in binding.included_datasets:   # the list, for a stable column order
        for dim in bundle.datasets[ds_name].dimensions.values():
            keep(dim.column, dim.name)
            if dim.geo:
                keep(dim.geo.lat, dim.geo.lat)
                keep(dim.geo.lon, dim.geo.lon)
    for key in binding.import_spec.right_on:
        keep(key, key)
    return lf.select(exprs)


def _as_date(lf: pl.LazyFrame, column: str, schema: pl.Schema, what: str) -> pl.LazyFrame:
    """Normalize one join key to pl.Date so interval comparisons line up
    regardless of whether the source stored dates or timestamps."""
    if column not in schema:
        raise QueryError(f"{what} column '{column}' not found in source")
    if isinstance(schema.get(column), pl.Datetime):
        return lf.with_columns(pl.col(column).cast(pl.Date))
    return lf


# hidden columns carrying a reporting period's span alongside the row that
# represents it; dropped again as soon as the join that needs them is built
PERIOD_FROM = "__period_from"
PERIOD_TO = "__period_to"


def _period_conditions(start: str, end: str, match: str) -> list[pl.Expr]:
    """Join predicates matching a row's [start, end] interval against a
    reporting period spanning [PERIOD_FROM, PERIOD_TO].

    Three readings of "was this row active in this period", shared by both
    point-in-time mechanisms (Dimension.spine and a `how: between` import) so
    they always answer the same question:

      overlap       the interval touches the period at all — a row opened on
                    Feb 2nd and closed Feb 15th counts in February, in Q1 and
                    in the year, which is what "active during" normally means
      period_start  it was already open on the period's first day
      period_end    it was still open on the period's last day

    The last two are snapshots, so a row that opens and closes inside a period
    without spanning its boundary is not counted; `overlap` is the default for
    that reason. At day grain the period is one day wide and all three agree.
    """
    lo, hi = pl.col(PERIOD_FROM), pl.col(PERIOD_TO)
    s, e = pl.col(start), pl.col(end)
    if match == "period_start":
        return [s <= lo, e >= lo]
    if match == "period_end":
        return [s <= hi, e >= hi]
    return [s <= hi, e >= lo]


def _period_rows(lf: pl.LazyFrame, point: str, grain: str) -> pl.LazyFrame:
    """Collapse a date table to one row per `grain` bucket, carrying that
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
    bucket = pl.col(point).dt.truncate(grain)
    lf = lf.with_columns(
        pl.col(point).min().over(bucket).alias(PERIOD_FROM),
        pl.col(point).max().over(bucket).alias(PERIOD_TO),
    )
    return lf.filter(pl.col(point) == pl.col(PERIOD_FROM))


def _join_interval(lf: pl.LazyFrame, binding: ImportBinding, grain: str) -> pl.LazyFrame:
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
    `grain` comes from the dimensions actually in play — see _interval_grain,
    and _period_conditions for what "active in this bucket" means.

    A null end column means "still open" (same convention as Dimension.spine),
    and the join is inner: buckets with nothing active drop out rather than
    appearing as zero rows.
    """
    imp = binding.import_spec
    start, end = imp.left_on
    point = imp.right_on[0]

    schema = lf.collect_schema()
    lf = _as_date(lf, start, schema, "interval start")
    lf = _as_date(lf, end, schema, "interval end")
    lf = lf.with_columns(pl.col(end).fill_null(FAR_FUTURE))

    right = _scan_bundle(binding)
    right = _as_date(right, point, right.collect_schema(), f"'{imp.bundle}' date")
    right = _period_rows(right, point, grain)
    joined = lf.join_where(right, *_period_conditions(start, end, imp.match))
    return joined.drop(PERIOD_FROM, PERIOD_TO)


GRAIN_ORDER = list(TIME_GRAINS)  # finest to coarsest: 1d, 1w, 1mo, 1q, 1y


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


def scan(model: Model, dimensions: Optional[dict] = None) -> pl.LazyFrame:
    """Base source plus any semantic-layer joins and imported dimension
    bundles, all lazy — polars pushes the needed columns down into each
    side of every join.

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
            f"({', '.join(p.name for p in model.parts)}) — there is no single frame to scan; "
            f"each part is scanned on its own (see _run_parts)"
        )
    lf = _scan_source(model.source)
    for join in model.joins:
        # coalesce=False: see the matching note in _scan_bundle — a right_on
        # key named differently from left_on is still a column a model
        # dimension can address by its own name, not just the join's key
        lf = lf.join(
            _scan_source(join.source),
            left_on=join.left_on, right_on=join.right_on, how=join.how, coalesce=False,
        )
    for binding in model.import_bindings:
        if binding.import_spec.is_interval:
            if dimensions is None:
                lf = _join_interval(lf, binding, GRAIN_ORDER[0])
            elif set(dimensions) & set(binding.dimension_owners):
                lf = _join_interval(lf, binding, _interval_grain(model, binding, dimensions))
            continue
        # A `how: left` import only ever adds columns, so a query using none of
        # them gets the same answer without it — and paying for a join it can't
        # read from is how a model that imports a calendar purely to conform
        # with its neighbours (models/sales.yaml) would slow down every query
        # that has nothing to do with dates. `inner` also *filters* the model's
        # rows, so it has to be applied whether or not its dimensions are read.
        if (dimensions is not None and binding.import_spec.how == "left"
                and not set(dimensions) & set(binding.dimension_owners)):
            continue
        # same coalesce=False reasoning: a "matching columns" import's
        # right_on (e.g. a calendar's own `date`) is routinely a declared
        # Dimension of the anchor dataset in its own right, not just a key
        lf = lf.join(
            _scan_bundle(binding),
            left_on=binding.import_spec.left_on, right_on=binding.import_spec.right_on,
            how=binding.import_spec.how, coalesce=False,
        )
    return lf


def _model_cache_key(model: Model) -> tuple:
    """Cache-key component identifying this exact Model object, not just its
    name — a model name isn't a stable identity while it's being edited (the
    guided form/YAML editor re-parses a fresh, unregistered Model on every
    keystroke, sometimes reusing an existing model's own name before it's
    ever saved), so anything cached under the name alone could leak a
    not-yet-saved edit's schema to an unrelated caller, or hand the editor
    back a stale one. Folding in id(model) means only repeat calls against
    the very same object — e.g. the registry's live model, kept alive in
    registry.models until the next reload_all() — ever hit the cache; a
    fresh reparse always misses and computes its own true answer."""
    return (model.name, id(model))


def scan_schema(model: Model, dimensions: Optional[dict] = None) -> pl.Schema:
    """Cached column schema for scan(model, dimensions) — same "which joins
    are in play" rules as scan() (see its docstring), just memoized so the
    model/measure editors and every query's own schema lookup (_run_single
    below) don't each re-resolve the whole join plan's schema from S3."""
    key = ("scan_schema", _model_cache_key(model),
           None if dimensions is None else tuple(sorted(dimensions)))
    return cache.get_or_set(key, config.SCHEMA_CACHE_TTL, lambda: scan(model, dimensions).collect_schema())


def _spine_bounds(
    model: Model, dims_in_play: dict, filters: list, sdim: Dimension, lf: pl.LazyFrame,
) -> tuple:
    """Cached (min start, max end) over `lf`'s spine columns — the natural
    window an unbounded spine query (no explicit date filter) falls back
    to. `lf` at this point already reflects every filter and optional join
    that shapes its row set, so those — not just the model and spine
    dimension — are exactly what have to key the cache; leaving one out
    would risk serving one query's bounds to a differently-filtered one.
    The source data itself is the one input this can't see change, hence
    the short TTL rather than trusting the entry forever."""
    key = ("spine_bounds", _model_cache_key(model), sdim.name,
           tuple(sorted(dims_in_play)), json.dumps(filters, sort_keys=True, default=str))
    row = cache.get_or_set(key, config.BOUNDS_CACHE_TTL, lambda: lf.select(
        pl.col(sdim.spine.start).min().alias("lo"),
        pl.col(sdim.spine.end).max().alias("hi"),
    ).collect())
    return row["lo"][0], row["hi"][0]


def _interval_binding_for(model: Model, dimension: str) -> Optional[ImportBinding]:
    """The interval import owning `dimension`, if any — lets callers that only
    want the imported side's own values read it without the fan-out join."""
    for binding in model.import_bindings:
        if binding.import_spec.is_interval and dimension in binding.dimension_owners:
            return binding
    return None


def _resolve_date_value(value: Any) -> date:
    """A filter value as a concrete date: a relative keyword/offset resolved
    against today, or else a fixed ISO date. Raises ValueError if it's
    neither."""
    relative = resolve_relative_date(value)
    return relative if relative is not None else date.fromisoformat(str(value))


def _coerce(value: Any, dtype: pl.DataType) -> Any:
    """Coerce a JSON filter value to the column's dtype so comparisons work."""
    if value is None:
        return None
    if dtype == pl.Date:
        return _resolve_date_value(value)
    if isinstance(dtype, pl.Datetime) or dtype == pl.Datetime:
        relative = resolve_relative_date(value)
        if relative is not None:
            return datetime.combine(relative, datetime.min.time())
        return datetime.fromisoformat(str(value))
    if dtype.is_integer():
        return int(value)
    if dtype.is_float():
        return float(value)
    if dtype == pl.Boolean:
        return value in (True, "true", "True", 1)
    return str(value)


def _filter_expr(model: Model, spec: dict, schema: pl.Schema) -> pl.Expr:
    dim = model.dimension(spec.get("field", ""))
    op = spec.get("op", "eq")
    if op not in FILTER_OPS:
        raise QueryError(f"unsupported filter op '{op}'")
    col = pl.col(dim.column)
    dtype = schema.get(dim.column)
    if dtype is None:
        raise QueryError(f"column '{dim.column}' not found in source")

    if op in ("in", "not_in"):
        values = [_coerce(v, dtype) for v in spec.get("values", [])]
        expr = col.is_in(values)
        return expr.not_() if op == "not_in" else expr
    value = _coerce(spec.get("value"), dtype)
    if op == "contains":
        return col.cast(pl.String).str.contains(f"(?i){str(spec.get('value', ''))}", literal=False)
    return _COMPARE_OPS[op](col, value)


def _spine_prepare(lf: pl.LazyFrame, dims: list, schema: pl.Schema) -> pl.LazyFrame:
    """Normalize spine columns: cast datetimes to dates, treat null end as
    open-ended (still active)."""
    for dim in dims:
        for col in (dim.spine.start, dim.spine.end):
            if col not in schema:
                raise QueryError(f"spine column '{col}' not found in source")
            if isinstance(schema.get(col), pl.Datetime):
                lf = lf.with_columns(pl.col(col).cast(pl.Date))
        lf = lf.with_columns(pl.col(dim.spine.end).fill_null(FAR_FUTURE))
    return lf


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
    value handed to measure_dsl.compile_measure's parameter_values, and
    every value compared for dashboard definition-equality, passes through
    this first, so a lag() periods check (which requires a real int) is
    never fooled by an int-shaped JSON float."""
    return float(value) if type_name == "float" else value


def resolve_parameter_values(parameters: list, parameter_values: dict) -> dict:
    """Validate a query's declared parameters and the caller's selected
    values, returning {name: value} with every declared parameter present
    — the caller's pick where given and in-list, else that parameter's
    declared default, each coerced to its declared (or implicit int) type.
    This is the only allowlist-membership check a parameter value ever
    passes through; the result is the only thing measure_dsl.compile_
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


def run_query_frame(model: Model, query: dict, row_cap: Optional[int] = None) -> tuple[pl.DataFrame, list[dict], float]:
    """run_query's body, stopping one step short of JSON: the collected frame,
    its column metadata, and how long the whole thing took.

    Split out for the instant-extract path (specs/016-instant-cross-filter/),
    which serializes the same frame as Arrow IPC instead. `row_cap` raises the
    default MAX_ROWS ceiling for that one caller — it is a keyword argument
    rather than a query field precisely so no HTTP request can lift its own
    limit; see app/extract.py.
    """
    started = time.perf_counter()
    runner = _run_parts if model.is_composite else _run_single
    df, columns = runner(model, query, row_cap) if row_cap else runner(model, query)
    return df, columns, round((time.perf_counter() - started) * 1000, 1)


def run_query(model: Model, query: dict) -> dict:
    """Execute a semantic query.

    query = {
      dimensions: ["region", {"name": "order_date", "grain": "1mo"}],
      measures: ["revenue"],
      filters: [{"field": "region", "op": "in", "values": [...]}],
      sort: {"by": "revenue", "desc": true} | null,
      limit: 500
    }

    Spine dimensions (dimension.spine = {start, end}) group point-in-time:
    a generated timeline at the requested grain is interval-joined against the
    start/end columns, so each row counts in every bucket it was active for.
    A `how: between` dimension import answers the same kind of question from a
    real date table instead — see scan() and _join_interval.

    A model holding several unrelated fact tables has no source of its own:
    the same query is answered by running one of these per part and merging the
    results on the dimensions they share — see _run_parts.
    """
    df, columns, elapsed_ms = run_query_frame(model, query)
    # write_json serializes dates/decimals to JSON-safe values for us
    rows = json.loads(df.write_json())
    return {"columns": columns, "rows": rows, "row_count": df.height, "elapsed_ms": elapsed_ms}


def _run_single(model: Model, query: dict, row_cap: Optional[int] = None) -> tuple[pl.DataFrame, list[dict]]:
    """Answer a query against one fact table — the whole of the engine's
    aggregation path. Returns the collected frame and its column metadata;
    run_query wraps it, and _run_composite calls it once per fact."""
    resolved_params = resolve_parameter_values(query.get("parameters") or [], query.get("parameter_values") or {})
    dims_in_play = _referenced_dimensions(query)
    lf = scan(model, dims_in_play)
    schema = scan_schema(model, dims_in_play)

    # split filters into spine-dimension filters and plain column filters
    spine_filters, plain_filters = [], []
    for spec in query.get("filters", []):
        dim = model.dimension(spec.get("field", ""))
        (spine_filters if dim.spine else plain_filters).append((dim, spec))

    # normalize requested dimensions; pull out the (single) spine dimension
    dim_entries = []          # [(dim, grain, is_spine)] in query order
    spine_entry = None        # (dim, grain)
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
            dim_entries.append((dim, grain, True))
        else:
            dim_entries.append((dim, grain, False))

    involved_spines = {dim.name: dim for dim, _ in spine_filters}
    if spine_entry:
        involved_spines[spine_entry[0].name] = spine_entry[0]
    if involved_spines:
        lf = _spine_prepare(lf, list(involved_spines.values()), schema)

    for _, spec in plain_filters:
        lf = lf.filter(_filter_expr(model, spec, schema))

    # spine filters restrict rows to those active in the window, and also
    # bound the generated timeline
    spine_lo = spine_hi = None
    for dim, spec in spine_filters:
        op = spec.get("op", "eq")
        try:
            v = _resolve_date_value(spec.get("value"))
        except ValueError:
            raise QueryError(f"spine filter on '{dim.name}' needs an ISO date value")
        s, e = pl.col(dim.spine.start), pl.col(dim.spine.end)
        if op in ("gte", "gt"):
            lf = lf.filter(e >= v)
            spine_lo = max(spine_lo, v) if spine_lo else v
        elif op in ("lte", "lt"):
            lf = lf.filter(s <= v)
            spine_hi = min(spine_hi, v) if spine_hi else v
        elif op == "eq":
            lf = lf.filter((s <= v) & (e >= v))
            spine_lo = spine_hi = v
        else:
            raise QueryError(f"filter op '{op}' not supported on spine dimension '{dim.name}'")

    # timeline join: spine buckets x rows active at each bucket
    if spine_entry:
        sdim, grain = spine_entry
        lo, hi = spine_lo, spine_hi
        if lo is None or hi is None:
            data_lo, data_hi = _spine_bounds(model, dims_in_play, query.get("filters") or [], sdim, lf)
            lo = lo or data_lo
            hi = hi or min(data_hi or date.today(), date.today())
        if lo is None or hi < lo:
            raise QueryError("no rows in the timeline window")
        lo = pl.Series([lo]).dt.truncate(grain)[0]
        # one row per bucket, carrying the bucket's span so the same three
        # readings of "active in this period" apply here as to a real date
        # table (see _period_conditions)
        buckets = pl.date_range(lo, hi, interval=grain, eager=True)
        spine_lf = pl.LazyFrame({sdim.name: buckets}).with_columns(
            pl.col(sdim.name).alias(PERIOD_FROM),
            pl.col(sdim.name).dt.offset_by(grain).dt.offset_by("-1d").alias(PERIOD_TO),
        )
        lf = spine_lf.join_where(
            lf, *_period_conditions(sdim.spine.start, sdim.spine.end, sdim.spine.match),
        ).drop(PERIOD_FROM, PERIOD_TO)

    dim_specs = []
    for dim, grain, is_spine in dim_entries:
        if is_spine:
            dim_specs.append((dim, pl.col(dim.name)))  # spine column already at grain
            continue
        expr = pl.col(dim.column)
        if dim.type == "time" and grain:
            expr = expr.dt.truncate(grain)
        dim_specs.append((dim, expr.alias(dim.name)))

    measure_names = query.get("measures", [])
    if not measure_names:
        raise QueryError("query needs at least one measure")
    # inline measures: ad-hoc expressions scoped to this query (the measure
    # lab / visual-scoped measures); they shadow model measures by name
    inline = {}
    for m in query.get("inline_measures") or []:
        if not m.get("name") or not m.get("expr"):
            raise QueryError("inline measures need a name and an expr")
        if m.get("frame") or m.get("frame_emits"):
            raise QueryError(
                f"measure '{m['name']}': frame-based measures require an authenticated "
                "model-measure save; they are never available as inline/query-time measures"
            )
        inline[m["name"]] = m
    # split measures into three kinds:
    #  - plain aggregations, applied in one group_by over the scan
    #  - framed measures, whose expr aggregates over a derived intermediary
    #    frame instead (Measure.frame_source / inline "frame")
    #  - window measures (running_total()/lag() — see measure_dsl.is_window_
    #    expr), computed *after* the group_by via .over(), since they read
    #    sibling measures' already-aggregated values rather than raw columns
    plain_exprs: list[pl.Expr] = []
    plain_names: set = set()  # names already added to plain_exprs (dedups deps)
    framed: list[tuple[str, str, set, pl.Expr]] = []  # (name, frame_source, frame_emits, agg expr)
    window_specs: list[tuple[str, str]] = []  # (name, dsl text)

    def add_plain(nm: str, expr: pl.Expr) -> None:
        if nm not in plain_names:
            plain_exprs.append(expr)
            plain_names.add(nm)

    def resolve_measure(nm: str, *, is_dependency: bool) -> None:
        if nm in inline:
            # inline measures are never framed (T004/T005 above already
            # reject frame/frame_emits on the way in) and always compile
            # through the safe DSL — never eval, regardless of caller.
            text = inline[nm]["expr"]
            if measure_dsl.is_window_expr(text):
                if is_dependency:
                    raise QueryError(
                        f"measure '{nm}' is itself a window measure and can't be used as "
                        "another window measure's dependency"
                    )
                window_specs.append((nm, text))
                return
            try:
                add_plain(nm, measure_dsl.compile_measure(text, schema, alias=nm, parameter_values=resolved_params))
            except measure_dsl.MeasureCompileError as exc:
                raise QueryError(f"measure '{nm}': {exc}") from exc
            return
        meas = model.measure(nm)
        if meas.frame_source:
            if is_dependency:
                raise QueryError(
                    f"measure '{nm}' uses an intermediary frame and can't be used as another "
                    "window measure's dependency"
                )
            framed.append((nm, meas.frame_source, set(meas.frame_emits), meas.expr(schema)))
            return
        if measure_dsl.is_window_expr(meas.expr_source):
            if is_dependency:
                raise QueryError(
                    f"measure '{nm}' is itself a window measure and can't be used as another "
                    "window measure's dependency"
                )
            window_specs.append((nm, meas.expr_source))
            return
        add_plain(nm, meas.expr(schema))

    try:
        for m in measure_names:
            resolve_measure(m, is_dependency=False)
        # a window measure's sibling references (e.g. running_total(revenue))
        # must be computed even if the caller didn't request them directly —
        # they're trimmed from the final result below if so
        for _, text in window_specs:
            for dep in measure_dsl.referenced_names(text):
                if dep not in plain_names:
                    resolve_measure(dep, is_dependency=True)
    except (ModelError, measure_dsl.MeasureCompileError) as exc:
        raise QueryError(str(exc)) from exc

    # geo dimensions carry their members' coordinates along as hidden columns
    for dim, _ in dim_specs:
        if dim.geo:
            plain_exprs.append(pl.col(dim.geo.lat).mean().alias(f"__lat_{dim.name}"))
            plain_exprs.append(pl.col(dim.geo.lon).mean().alias(f"__lon_{dim.name}"))

    dim_names = [d.name for d, _ in dim_specs]
    if plain_exprs:
        if dim_specs:
            out = lf.group_by([e for _, e in dim_specs]).agg(plain_exprs)
        else:
            out = lf.select(plain_exprs)
    else:
        # all measures framed: the derived frames alone define which dimension
        # groups exist (an emitted timeline shouldn't inherit raw-row buckets)
        out = None

    # window measures (running_total()/lag()) read sibling measures' already-
    # aggregated values, partitioned by the query's other dimensions and
    # ordered by its time dimension — "previous quarter" only means something
    # once the data has been grouped down to one row per quarter. Applied via
    # .over() right after the group_by, before the framed-measure joins below
    # (window measures can only depend on plain measures, never framed ones).
    if window_specs:
        if out is None:
            raise QueryError(
                "window measures (running_total/lag) need at least one plain aggregate "
                "measure in the query to compute over"
            )
        time_dims = [d.name for d, _ in dim_specs if d.type == "time"]
        if not time_dims:
            raise QueryError(
                "window measures (running_total/lag) require a time dimension in the "
                "query's dimensions to order by"
            )
        if len(time_dims) > 1:
            raise QueryError(
                "window measures (running_total/lag) support only one time dimension "
                "per query — ambiguous ordering"
            )
        order_dim = time_dims[0]
        partition_cols = [d for d in dim_names if d != order_dim]
        out_schema = out.collect_schema()
        win_exprs = []
        for name, text in window_specs:
            try:
                win_exprs.append(measure_dsl.compile_measure(
                    text, out_schema, alias=name, partition_by=partition_cols, order_by=order_dim,
                    parameter_values=resolved_params,
                ))
            except measure_dsl.MeasureCompileError as exc:
                raise QueryError(f"measure '{name}': {exc}") from exc
        out = out.with_columns(win_exprs)

    # each framed measure runs its snippet against the filtered scan (with the
    # query's dimension columns materialized), then its expr aggregates the
    # derived frame per dimension group; results join back on the dimensions.
    # dimensions in the measure's frame_emits are the frame's own output
    # columns (e.g. a per-entity milestone date): they're withheld from `dims`
    # during the step and bucketed on the derived frame afterwards, so a
    # timeline groups the derived rows, not the raw events feeding them
    if framed:
        dim_expr = {dim.name: e for dim, e in dim_specs}
        grain_of = {dim.name: grain for dim, grain, _ in dim_entries}
        time_dim = {dim.name for dim, _ in dim_specs if dim.type == "time"}
    for name, frame_source, emits, expr in framed:
        emitted = [d for d in dim_names if d in emits]
        carried = [d for d in dim_names if d not in emits]
        base = lf.with_columns([dim_expr[d] for d in carried]) if carried else lf
        try:
            derived = compile_frame(frame_source, base, carried, f"measure '{name}'")
        except ModelError as exc:
            raise QueryError(str(exc)) from exc
        try:
            derived_schema = derived.collect_schema()
        except Exception as exc:
            raise QueryError(f"measure '{name}': invalid intermediary frame: {exc}") from exc
        missing = [d for d in dim_names if d not in derived_schema]
        if missing:
            raise QueryError(
                f"measure '{name}': the intermediary frame lost dimension column(s) {missing} — "
                "carry the query's dimensions through with `dims` (e.g. group_by([*keys, *dims])), "
                "or list a dimension in the measure's frame_emits and output it from the frame"
            )
        trunc = [pl.col(d).dt.truncate(grain_of[d]).alias(d)
                 for d in emitted if d in time_dim and grain_of.get(d)]
        if trunc:
            derived = derived.with_columns(trunc)
        part = derived.group_by(dim_names).agg(expr) if dim_names else derived.select(expr)
        if out is None:
            out = part
        elif dim_names:
            # full join: a group present on either side keeps its row — carried
            # dims make the framed side a subset (same as a left join), but an
            # emitted dimension can surface groups the raw rows never form
            out = out.join(part, on=dim_names, how="full", coalesce=True, nulls_equal=True)
        else:
            out = out.join(part, how="cross")

    # drop any sibling measure only pulled in as a window measure's dependency
    # (e.g. running_total(revenue) requested alone still needs revenue
    # computed) — keep exactly what was asked for, plus the geo hidden columns
    extra = plain_names - set(measure_names)
    if extra:
        geo_cols = [c for dim, _ in dim_specs if dim.geo for c in (f"__lat_{dim.name}", f"__lon_{dim.name}")]
        out = out.select([*dim_names, *geo_cols, *measure_names])
    lf = out

    sort = query.get("sort") or {}
    valid_sort_keys = {d.name for d, _ in dim_specs} | set(measure_names)
    by = sort.get("by")
    if by and by in valid_sort_keys:
        lf = lf.sort(by, descending=bool(sort.get("desc", True)))
    elif dim_specs:
        # deterministic default: time ascending if present, else first measure desc
        time_dims = [d.name for d, _ in dim_specs if d.type == "time"]
        if time_dims:
            lf = lf.sort(time_dims[0])
        else:
            lf = lf.sort(measure_names[0], descending=True)

    limit = min(int(query.get("limit") or 1000), row_cap or config.MAX_ROWS)
    df = lf.limit(limit).collect()

    def _measure_meta(m: str) -> dict:
        if m in inline:
            return {"name": m, "label": inline[m].get("label") or m, "kind": "measure",
                    "format": inline[m].get("format") or "number", "inline": True}
        meas = model.measure(m)
        return {"name": m, "label": meas.label, "kind": "measure", "format": meas.format}

    columns = [
        {"name": d.name, "label": d.label, "kind": "dimension", "type": d.type}
        for d, _ in dim_specs
    ] + [_measure_meta(m) for m in measure_names]
    return df, columns


# ---------------------------------------------------------------------------
# Several fact tables in one model: "drill across" the dimensions they share.
#
# The parts are never joined to each other. Each is queried on its own at the
# same grain, over the dimensions they all share, and the results are merged on
# those dimension columns afterwards. Joining the fact tables directly — even
# via a shared date — would give every row of one fact a copy of every matching
# row of the others and multiply both sides' measures; running them separately
# and merging the *aggregates* is the only shape that keeps each measure's
# grain intact.
#
# A bucket that only one part has rows for keeps the row and leaves the other
# parts' measures null. That is deliberate: null reads as "this fact table has
# nothing here", which is what happened, whereas zero would be a number nobody
# measured. Charts draw it as a gap.
# ---------------------------------------------------------------------------

def _align_join_key(df: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """Normalize the dtypes the per-part results are merged on, so two fact
    tables that store the same shared dimension differently still line up: a
    timestamp column meets a date one at date, a categorical meets a string at
    string."""
    casts = []
    for name in columns:
        dtype = df.schema[name]
        if isinstance(dtype, pl.Datetime):
            casts.append(pl.col(name).cast(pl.Date))
        elif dtype in (pl.Categorical, pl.Enum):
            casts.append(pl.col(name).cast(pl.String))
    return df.with_columns(casts) if casts else df


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


def _merged_dimensions(
    query: dict, parts: list, shared: dict,
) -> list[tuple[str, Optional[str]]]:
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


def _run_parts(model: Model, query: dict, row_cap: Optional[int] = None) -> tuple[pl.DataFrame, list[dict]]:
    """Answer a query against a model holding several unrelated fact tables:
    one _run_single per part that contributes a measure, merged on the shared
    dimensions."""
    if query.get("inline_measures"):
        raise QueryError(
            f"'{model.name}' holds several unrelated fact tables and doesn't take inline "
            f"measures — an expression has to be scoped to one of them, so declare it on the "
            f"dataset it belongs to"
        )
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

    out: Optional[pl.DataFrame] = None
    for part in parts:
        own_measures = wanted[part.name]
        frame, _ = _run_single(part.model, {
            "dimensions": [{"name": name, "grain": grain} for name, grain in dim_entries],
            "measures": own_measures,
            "filters": filters,
            # merge first, then sort and cut: a per-part limit would drop
            # buckets another fact table still has rows for
            "limit": row_cap or config.MAX_ROWS,
            "parameters": query.get("parameters"),
            "parameter_values": query.get("parameter_values"),
        }, row_cap)
        frame = _align_join_key(frame.select([*dim_names, *own_measures]), dim_names)
        if out is None:
            out = frame
        elif dim_names:
            out = out.join(frame, on=dim_names, how="full", coalesce=True, nulls_equal=True)
        else:
            out = out.join(frame, how="cross")   # grand totals: one row each side

    out = out.select([*dim_names, *measure_names])

    sort = query.get("sort") or {}
    by = sort.get("by")
    if by and by in set(dim_names) | set(measure_names):
        out = out.sort(by, descending=bool(sort.get("desc", True)), nulls_last=True)
    elif dim_names:
        # same deterministic default as _run_single: time ascending if present,
        # else the first measure descending
        time_dims = [name for name in dim_names if shared[name].type == "time"]
        if time_dims:
            out = out.sort(time_dims[0], nulls_last=True)
        else:
            out = out.sort(measure_names[0], descending=True, nulls_last=True)

    out = out.head(min(int(query.get("limit") or 1000), row_cap or config.MAX_ROWS))
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
    return out, columns


def dimension_values(model: Model, dimension: str, limit: int = 100) -> list:
    dim = model.dimension(dimension)
    if model.is_composite:
        # a shared dimension means the same thing to every fact table, so any
        # of them can supply its members; take the first that isn't a
        # generated timeline
        for part in model.parts:
            if part.model.dimension(dimension).spine:
                continue
            return dimension_values(part.model, dimension, limit)
        raise QueryError(f"'{dimension}' is a generated timeline; filter it with date ranges instead")
    if dim.spine:
        raise QueryError(f"'{dimension}' is a generated timeline; filter it with date ranges instead")
    # a dimension from an interval import has its own values independent of the
    # model's rows — read them off the imported side; anything else needs no
    # interval join at all, hence the empty dimension set
    interval = _interval_binding_for(model, dimension)
    base = _scan_bundle(interval) if interval else scan(model, {})
    if dim.column not in base.collect_schema():
        raise QueryError(f"column '{dim.column}' not found in source")
    df = (
        base
        .select(pl.col(dim.column).alias(dim.name))
        .unique()
        .sort(dim.name)
        .limit(limit)
        .collect()
    )
    return [row[dim.name] for row in json.loads(df.write_json())]
