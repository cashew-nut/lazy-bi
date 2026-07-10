"""Query engine: turns a semantic-layer query into a lazy polars scan over S3.

Nothing is downloaded eagerly — scan_parquet/scan_csv against the object store
lets polars push projections and predicates down, so only the columns and row
groups a query needs leave the (emulated) bucket.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any, Optional

import polars as pl

from . import config
from .semantic import ImportBinding, Model, ModelError, Source, TIME_GRAINS, compile_expr

FILTER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains"}


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


def _coerce(value: Any, dtype: pl.DataType) -> Any:
    """Coerce a JSON filter value to the column's dtype so comparisons work."""
    if value is None:
        return None
    if dtype == pl.Date:
        return date.fromisoformat(str(value))
    if isinstance(dtype, pl.Datetime) or dtype == pl.Datetime:
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
    return {
        "eq": col == value, "ne": col != value,
        "gt": col > value, "gte": col >= value,
        "lt": col < value, "lte": col <= value,
    }[op]


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
            v = date.fromisoformat(str(spec.get("value")))
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
        inline[m["name"]] = m
    try:
        measure_exprs = [
            compile_expr(inline[m]["expr"], f"measure '{m}'").alias(m) if m in inline
            else model.measure(m).expr()
            for m in measure_names
        ]
    except ModelError as exc:
        raise QueryError(str(exc)) from exc

    # geo dimensions carry their members' coordinates along as hidden columns
    for dim, _ in dim_specs:
        if dim.geo:
            measure_exprs.append(pl.col(dim.geo.lat).mean().alias(f"__lat_{dim.name}"))
            measure_exprs.append(pl.col(dim.geo.lon).mean().alias(f"__lon_{dim.name}"))

    if dim_specs:
        lf = lf.group_by([e for _, e in dim_specs]).agg(measure_exprs)
    else:
        lf = lf.select(measure_exprs)

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
