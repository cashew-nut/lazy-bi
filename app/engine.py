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

from . import config, measure_dsl
from .semantic import ImportBinding, Model, ModelError, Source, TIME_GRAINS, compile_frame

FILTER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains"}

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
    return pl.scan_parquet(source.path, storage_options=opts)


def scan_source(source: Source) -> pl.LazyFrame:
    """Public: lazily scan a single source (no joins). Used by the dimension
    bundle editor to introspect one dataset's own columns."""
    return _scan_source(source)


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
            lf = lf.join(
                _scan_source(bundle.datasets[ds_name].source),
                left_on=left_on, right_on=right_on, how=edge.how,
            )
            joined.add(ds_name)
            remaining.discard(ds_name)
            progressed = True
        if not progressed:
            # resolve_imports() computes `included` via the same reachability
            # rules, so everything in it must connect back to the anchor
            raise ModelError(
                f"dimension bundle '{bundle.name}': internal error resolving join "
                f"order for datasets {sorted(remaining)}"
            )
    return lf


def scan(model: Model) -> pl.LazyFrame:
    """Base source plus any semantic-layer joins and imported dimension
    bundles, all lazy — polars pushes the needed columns down into each
    side of every join."""
    lf = _scan_source(model.source)
    for join in model.joins:
        lf = lf.join(
            _scan_source(join.source),
            left_on=join.left_on, right_on=join.right_on, how=join.how,
        )
    for binding in model.import_bindings:
        lf = lf.join(
            _scan_bundle(binding),
            left_on=binding.import_spec.left_on, right_on=binding.import_spec.right_on,
            how=binding.import_spec.how,
        )
    return lf


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


FAR_FUTURE = date(9999, 1, 1)


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
    """
    started = time.perf_counter()
    resolved_params = resolve_parameter_values(query.get("parameters") or [], query.get("parameter_values") or {})
    lf = scan(model)
    schema = lf.collect_schema()

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
            bounds = lf.select(
                pl.col(sdim.spine.start).min().alias("lo"),
                pl.col(sdim.spine.end).max().alias("hi"),
            ).collect()
            lo = lo or bounds["lo"][0]
            data_hi = bounds["hi"][0]
            hi = hi or min(data_hi or date.today(), date.today())
        if lo is None or hi < lo:
            raise QueryError("no rows in the timeline window")
        lo = pl.Series([lo]).dt.truncate(grain)[0]
        spine_lf = pl.LazyFrame({sdim.name: pl.date_range(lo, hi, interval=grain, eager=True)})
        lf = spine_lf.join_where(
            lf,
            pl.col(sdim.spine.start) <= pl.col(sdim.name),
            pl.col(sdim.spine.end) >= pl.col(sdim.name),
        )

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

    limit = min(int(query.get("limit") or 1000), config.MAX_ROWS)
    df = lf.limit(limit).collect()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

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
    # write_json serializes dates/decimals to JSON-safe values for us
    rows = json.loads(df.write_json())
    return {"columns": columns, "rows": rows, "row_count": df.height, "elapsed_ms": elapsed_ms}


def dimension_values(model: Model, dimension: str, limit: int = 100) -> list:
    dim = model.dimension(dimension)
    if dim.spine:
        raise QueryError(f"'{dimension}' is a generated timeline; filter it with date ranges instead")
    df = (
        scan(model)
        .select(pl.col(dim.column).alias(dim.name))
        .unique()
        .sort(dim.name)
        .limit(limit)
        .collect()
    )
    return [row[dim.name] for row in json.loads(df.write_json())]
