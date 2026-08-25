"""Instant cross-filter extracts (specs/016-instant-cross-filter/).

A dashboard in *instant* mode fetches each tile once, as an Arrow IPC
extract, and then answers every subsequent cross-filter, grain toggle, or
focus change by re-aggregating that extract in the browser — no round trip.
This module decides what such an extract has to contain, builds it, and
enforces the size cap that sends a tile back to the ordinary live query path.

Three things make an extract different from the tile's ordinary `/query`
result, and all three live here rather than in the engine:

  1. **It is wider.** Alongside the tile's own dimensions it carries every
     *other* dashboard tile's dimensions that this tile's model also has
     (FR-006), so a cross-filter originating elsewhere can be applied
     locally. That is the one place this feature trades against Principle II:
     a wider result set, still fully pushed down, still capped.

  2. **It is decomposed.** Re-aggregating an already-aggregated extract is
     only sound for measures that decompose — see sqlgrammar.rollup_plan.
     Each measure is replaced by its additive components (requested through
     the ordinary inline-measure path, so the engine runs unchanged), and
     the browser recomputes the measure from the rolled-up components.
     A measure with no decomposition makes the whole tile ineligible, and it
     silently keeps today's behavior (FR-009/FR-010) rather than rendering a
     plausible-looking wrong number.

  3. **It carries its own coarser buckets.** A session grain override that
     asks for a *coarser* bucket than the extract holds is answered locally
     (R3), from a column the engine truncated with `date_trunc` — so the
     browser never does date arithmetic that could disagree with it.

Everything else — model resolution, authorization, filter pushdown, the
composite merge — is engine.run_query's existing path, untouched (FR-001/
FR-002).
"""
from __future__ import annotations

from typing import Optional

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc as pa_ipc

from . import config, engine, semantic, sqlgrammar
from .semantic import Model, ModelError

# component measure columns (one per additive part of a measure) and the
# precomputed coarser-grain dimension columns. Both are engine-internal
# names the browser addresses by the metadata below, never by guessing.
COMPONENT_PREFIX = "__x"
GRAIN_PREFIX = "__g"

# Which coarser buckets a grain can be rolled up into *locally*. Deliberately
# not simply "everything above me in GRAIN_ORDER": weeks do not nest inside
# months (a week can straddle two of them), so a week-grained extract can
# answer nothing coarser and has to re-fetch. Days nest in everything;
# months nest in quarters and years; quarters in years.
COARSER_GRAINS = {
    None: ["1d", "1w", "1mo", "1q", "1y"],   # ungrained time column: any bucket
    "1d": ["1w", "1mo", "1q", "1y"],
    "1w": [],
    "1mo": ["1q", "1y"],
    "1q": ["1y"],
    "1y": [],
}


class NotInstantable(Exception):
    """This tile cannot be served as an extract — it runs live instead. The
    message is shown to the author on the tile's mode badge (FR-013)."""


class CapExceeded(Exception):
    """The extract was built but is over the configured per-tile cap, so the
    tile runs live. Carries which cap tripped, for the same badge.

    The row cap is checked before serializing — there's no reason to spend
    the CPU encoding something already destined for the bin — so byte_size is
    0 in that case, and the message says nothing about a size nobody
    measured."""

    def __init__(self, cap: str, rows: int, byte_size: int):
        self.cap, self.rows, self.byte_size = cap, rows, byte_size
        measured = (f"{rows:,} rows, over the {config.EXTRACT_MAX_ROWS:,} row limit"
                    if cap == "rows" else
                    f"{byte_size / 1e6:.1f} MB over {rows:,} rows, over the "
                    f"{config.EXTRACT_MAX_BYTES / 1e6:.0f} MB limit")
        super().__init__(f"extract too large for instant mode: {measured}")


# ── what may go into an extract ─────────────────────────────────────────

def _fans_out(model: Model, name: str) -> bool:
    """True if grouping by this dimension counts a source row in more than one
    bucket — a spine dimension, or one read through a `how: between` interval
    import. Both are the point-in-time mechanisms described in engine.scan;
    both make a local roll-up double-count, so an extract never carries one.
    """
    if model.is_composite:
        for part in model.parts:
            if name in part.model.dimensions and _fans_out(part.model, name):
                return True
        return False
    dim = model.dimensions.get(name)
    if dim is None:
        return False
    return bool(dim.spine) or engine._interval_binding_for(model, name) is not None


def _dimension(model: Model, name: str):
    """The Dimension object behind a name, whether the model has one fact
    table or several (whose shared dimensions live on the parts)."""
    if not model.is_composite:
        return model.dimensions.get(name)
    for part in model.parts:
        if name in part.model.dimensions:
            return part.model.dimensions[name]
    return None


def _measure_meta(model: Model, name: str, inline: dict) -> dict:
    """Display metadata for one requested measure, in the same shape
    engine._run_single/_run_parts put on a /query response — the chart
    renderers read `columns` identically either way (FR-005)."""
    if name in inline:
        spec = inline[name]
        return {"name": name, "label": spec.get("label") or name, "kind": "measure",
                "format": spec.get("format") or "number", "inline": True}
    meas = model.measure(name)
    meta = {"name": name, "label": meas.label, "kind": "measure", "format": meas.format}
    if model.is_composite:
        meta["fact"] = semantic.part_for_measure(model, name).name
    return meta


def _measure_text(model: Model, name: str, inline: dict) -> str:
    """The SQL text behind a measure, whichever kind it is. Raises
    NotInstantable for the one construct whose expression means nothing on its
    own — a measure that aggregates a `from:` relation."""
    if name in inline:
        return inline[name]["expr"]
    try:
        meas = model.measure(name)
    except ModelError as exc:
        raise NotInstantable(str(exc)) from exc
    if meas.from_source:
        raise NotInstantable(
            f"measure '{name}' aggregates a from: relation, which can't be re-aggregated "
            "in the browser"
        )
    return meas.expr_source


# ── planning ────────────────────────────────────────────────────────────

def plan(model: Model, query: dict, cross_dimensions: list,
         interactive_filters: Optional[list] = None, hoist: bool = True) -> dict:
    """Decide what this tile's extract contains, or raise NotInstantable.

    Returns {"query": <the rewritten engine query>, "measures": [...],
    "dimensions": [...], "passthrough": [...], "local_filters": [...]} — the
    rewritten query is what goes to the engine, the rest is what the browser
    needs to re-aggregate the result it comes back with.

    `hoist=False` re-plans with every filter pushed down again, which is what
    build() falls back to when hoisting blows the size cap.
    """
    inline = {m["name"]: m for m in (query.get("inline_measures") or []) if m.get("name")}
    measure_names = list(query.get("measures") or [])
    if not measure_names:
        raise NotInstantable("query needs at least one measure")

    # ── dimensions: the tile's own, then the cross-filterable union ──
    own: list[tuple[str, Optional[str]]] = []
    for entry in query.get("dimensions") or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        name = entry.get("name")
        if not name:
            continue
        if _fans_out(model, name):
            raise NotInstantable(
                f"dimension '{name}' counts a row in every period it spans, so a local "
                "roll-up would double-count it"
            )
        own.append((name, entry.get("grain")))

    own_names = {name for name, _ in own}
    # every dimension this tile's model shares with another tile on the
    # dashboard, so a cross-filter from there lands locally (FR-006). A
    # dimension the model doesn't have is skipped — exactly today's silent
    # no-op for a mismatched model.
    #
    # Time dimensions are never unioned in. Every chart renderer refuses to
    # emit a cross-filter from a time mark (the `type !== "time"` guards in
    # charts/bar.js, ribbon.js, scatter.js), so another tile's dates can never
    # be the value that arrives here — carrying them buys nothing, and at
    # ungrained resolution they are by far the most expensive thing an extract
    # could hold. A tile's *own* time dimensions are unaffected: it groups by
    # those, and they keep their coarser buckets for the grain override.
    extra: list[tuple[str, Optional[str]]] = []
    for entry in cross_dimensions:
        if isinstance(entry, str):
            entry = {"name": entry}
        name = entry.get("name")
        dim = _dimension(model, name) if name else None
        if not name or name in own_names or dim is None or dim.type == "time" or _fans_out(model, name):
            continue
        own_names.add(name)
        extra.append((name, None))

    # ── static filters: pushed down, or carried as a column ──
    # A dashboard-view filter the viewer can change is normally baked into the
    # extract, which means changing it costs a re-fetch — the slowest thing on
    # an instant dashboard. Hoisting it instead (leave it out of the pushdown,
    # carry its dimension as a column) lets the browser answer a value change
    # locally, at the price of an extract holding every value rather than one.
    # A dimension another tile already contributed is free; a new one costs its
    # cardinality, which is what the cap below is for.
    #
    # Only the fields the caller names are candidates: a visual's *own* saved
    # filters are part of what the tile is, never edited from the dashboard, so
    # hoisting them would buy nothing and cost the same cardinality.
    filters = list(query.get("filters") or [])
    hoisted: list[str] = []
    if hoist:
        for field in dict.fromkeys(interactive_filters or []):
            on_field = [f for f in filters if f.get("field") == field]
            # a field with no value set yet is hoisted anyway — that is the
            # state a dashboard sits in before anyone touches its filters, and
            # carrying the column now is what makes the *first* change local
            # rather than the second
            if _field_hoistable(model, field) and all(_op_hoistable(f) for f in on_field):
                hoisted.append(field)
        for field in hoisted:
            if field not in own_names:
                own_names.add(field)
                extra.append((field, None))
        filters = [f for f in filters if f.get("field") not in hoisted]

    # ── measures: decomposed into additive components ──
    # A measure's formula refs index its *own* components list, so each entry
    # in `measures` is self-contained; `components` below is just the deduped
    # set of columns the extract has to carry to satisfy all of them.
    components: list[dict] = []      # [{"col", "agg", "expr"|None}]
    by_expr: dict[tuple, dict] = {}
    measures: list[dict] = []
    for name in measure_names:
        plan_ = sqlgrammar.rollup_plan(_measure_text(model, name, inline))
        if plan_ is None:
            raise NotInstantable(
                f"measure '{name}' can't be re-aggregated from an already-aggregated "
                "extract without changing its value"
            )
        if model.is_composite and (len(plan_["components"]) != 1 or plan_["formula"] != {"ref": 0}):
            # a model with several fact tables answers each separately and
            # takes no inline measures (engine._run_parts) — so its measures go
            # in as themselves, and only a measure that *is* one whole additive
            # aggregate can be rolled up
            raise NotInstantable(
                f"measure '{name}' would need to be broken into components, which a model with "
                "several fact tables can't do (each is queried separately)"
            )
        used = []
        for comp in plan_["components"]:
            key = (name, comp["agg"]) if model.is_composite else (comp["expr"], comp["agg"])
            existing = by_expr.get(key)
            if existing is None:
                existing = {
                    "col": name if model.is_composite else f"{COMPONENT_PREFIX}{len(components)}",
                    "agg": comp["agg"],
                    "expr": None if model.is_composite else comp["expr"],
                }
                by_expr[key] = existing
                components.append(existing)
            used.append({"col": existing["col"], "agg": existing["agg"]})
        measures.append({
            **_measure_meta(model, name, inline),
            "components": used,
            "formula": plan_["formula"],
        })

    # ── the query the engine actually runs ──
    dims = [{"name": name, "grain": g} if g else name for name, g in [*own, *extra]]
    engine_query = {
        **query,
        "filters": filters,
        "dimensions": dims,
        "measures": [c["col"] for c in components],
        "inline_measures": ([] if model.is_composite else
                            [{"name": c["col"], "expr": c["expr"]} for c in components]),
        "sort": None,        # the browser sorts the tile's own result, post-roll-up
        "limit": config.EXTRACT_MAX_ROWS + 1,   # +1 so "at the cap" is detectable
    }
    if model.is_composite:
        engine_query["measures"] = measure_names

    displayed = {name for name, _ in own}
    dimensions = []
    for name, g in [*own, *extra]:
        dim = _dimension(model, name)
        entry = {"name": name, "label": dim.label, "type": dim.type,
                 "grain": g, "display": name in displayed}
        if dim.type == "time":
            entry["coarser"] = {c: f"{GRAIN_PREFIX}{c}__{name}" for c in COARSER_GRAINS.get(g, [])}
        dimensions.append(entry)

    return {"query": engine_query, "measures": measures, "dimensions": dimensions,
            "passthrough": _passthrough(model, [*own, *extra]), "local_filters": hoisted}


def _passthrough(model: Model, dims: list) -> list:
    return [
        {"col": f"__{axis}_{name}", "agg": "mean"}
        for name, _ in dims
        for axis in ("lat", "lon")
        if getattr(_dimension(model, name), "geo", None)
    ]


# filter ops the browser can reproduce exactly. `contains` is deliberately
# absent: the engine implements it as a case-insensitive *regex* over the
# column cast to string (engine._filter_expr), which is not what any
# client-side substring match does — so it stays pushed down.
HOISTABLE_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"}


def _field_hoistable(model: Model, field: str) -> bool:
    """Can this dimension be carried as a column for the browser to filter on?

    The one genuinely dangerous case is a time dimension: the engine filters
    the *raw* column, while an extract holds it truncated to the tile's grain,
    so `order_date >= 2024-06-15` would keep all of June rather than half of
    it. Rather than reason about bucket alignment for a value that changes
    after the extract was built, time filters always stay pushed down.
    """
    dim = _dimension(model, field)
    return dim is not None and dim.type != "time" and not _fans_out(model, field)


def _op_hoistable(spec: dict) -> bool:
    return spec.get("op", "eq") in HOISTABLE_OPS


# ── building ────────────────────────────────────────────────────────────

def build(model: Model, query: dict, cross_dimensions: list,
          interactive_filters: Optional[list] = None) -> tuple[bytes, dict]:
    """Run the planned extract through the ordinary engine path and serialize
    it as an Arrow IPC stream. Returns (payload, metadata).

    Hoisting the dashboard's static filters (so changing one costs no round
    trip) makes an extract as many times larger as those filters were
    selective, which can push a tile over the cap that fitted comfortably
    without it. That is a reason to give up the hoisting, not the whole
    feature — so a cap miss is retried once with every filter pushed back
    down, and only a tile that fails *that* goes live.
    """
    try:
        return _build(model, query, cross_dimensions, interactive_filters, hoist=True)
    except CapExceeded:
        if not plan(model, query, cross_dimensions, interactive_filters)["local_filters"]:
            raise      # nothing was hoisted, so there is nothing to give up
    return _build(model, query, cross_dimensions, interactive_filters, hoist=False)


def _build(model: Model, query: dict, cross_dimensions: list,
           interactive_filters: Optional[list], hoist: bool) -> tuple[bytes, dict]:
    planned = plan(model, query, cross_dimensions, interactive_filters, hoist=hoist)
    table, columns, elapsed_ms = engine.run_query_arrow(
        model, planned["query"], row_cap=config.EXTRACT_MAX_ROWS + 1)
    if table.num_rows > config.EXTRACT_MAX_ROWS:
        raise CapExceeded("rows", table.num_rows, 0)

    table = _add_coarser_grains(table, planned["dimensions"])
    for dim in planned["dimensions"]:
        # a "time" dimension whose source column isn't actually temporal has no
        # precomputed buckets — drop the promise rather than let the browser
        # reach for a column that was never written
        if dim.get("coarser"):
            dim["coarser"] = {g: c for g, c in dim["coarser"].items()
                              if c in table.column_names}
    table = _normalize(table)
    # the browser has to coerce a filter value the same way engine._coerce
    # does, and after normalization every dimension column is one of three
    # things — so say which, rather than make it guess from the value
    for dim in planned["dimensions"]:
        index = table.column_names.index(dim["name"]) if dim["name"] in table.column_names else None
        dtype = table.schema.field(index).type if index is not None else None
        dim["value_type"] = (
            "number" if dtype is not None and (pa.types.is_integer(dtype)
                                               or pa.types.is_floating(dtype))
            else "boolean" if dtype is not None and pa.types.is_boolean(dtype)
            else "string")
    payload = _ipc_stream(table)
    if len(payload) > config.EXTRACT_MAX_BYTES:
        raise CapExceeded("bytes", table.num_rows, len(payload))

    by_name = {c["name"]: c for c in columns}
    display = [by_name[d["name"]] for d in planned["dimensions"]
               if d["display"] and d["name"] in by_name] + [
        {k: v for k, v in m.items() if k not in ("components", "formula")}
        for m in planned["measures"]
    ]
    meta = {
        "row_count": table.num_rows,
        "byte_size": len(payload),
        "elapsed_ms": elapsed_ms,
        "columns": display,                      # what the chart renderers read
        "dimensions": planned["dimensions"],
        "measures": planned["measures"],
        "passthrough": [p for p in planned["passthrough"] if p["col"] in table.column_names],
        # the dashboard filters this extract can answer a *value change* on
        # without coming back; anything else stayed baked in, and changing it
        # still costs a re-fetch
        "local_filters": planned["local_filters"],
    }
    return payload, meta


def _ipc_stream(table: pa.Table) -> bytes:
    """The extract as an Arrow IPC stream.

    Written through pyarrow rather than by the query engine: DuckDB has no
    IPC writer of its own, and the handoff is zero-copy either way since the
    result already *is* an Arrow table."""
    sink = pa.BufferOutputStream()
    with pa_ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _add_coarser_grains(table: pa.Table, dimensions: list) -> pa.Table:
    """Precompute, for each time dimension, the coarser buckets a session
    grain override could ask for. Truncating an already-truncated date to a
    coarser (nesting) grain gives the same bucket as truncating the raw date,
    so these are exactly the values a re-fetch would have returned — computed
    here rather than by date arithmetic in the browser."""
    for dim in dimensions:
        if not dim.get("coarser") or dim["name"] not in table.column_names:
            continue
        column = table.column(dim["name"])
        if not (pa.types.is_temporal(column.type)):
            continue
        for grain, name in dim["coarser"].items():
            unit = _ARROW_GRAIN_UNIT[grain]
            multiple = 3 if grain == "1q" else 1
            truncated = pc.floor_temporal(column, multiple=multiple, unit=unit,
                                          week_starts_monday=True)
            table = table.append_column(name, truncated)
    return table


# the five grains as pyarrow floor_temporal units; a quarter is three months,
# which is how DuckDB's own date_trunc('quarter', …) lands too
_ARROW_GRAIN_UNIT = {"1d": "day", "1w": "week", "1mo": "month",
                     "1q": "month", "1y": "year"}


def _normalize(table: pa.Table) -> pa.Table:
    """Coerce every column to a type Arrow IPC round-trips into Perspective,
    keeping the *values* identical to what the JSON path produces.

    Temporal columns become the same ISO strings engine.run_query emits, so a
    cross-filter value clicked on a live tile compares equal to one in an
    extract, and grouping by a date is grouping by the same token either way.
    Strings are written as `large_string`: Arrow's newer `string_view` layout
    is rejected outright by Perspective's reader, and the long-established
    layout carries the same values.
    """
    fields, columns = [], []
    for index, field in enumerate(table.schema):
        column = table.column(index)
        dtype = field.type
        if pa.types.is_timestamp(dtype) or pa.types.is_date(dtype) or pa.types.is_time(dtype):
            column = _iso_strings(column)
        elif pa.types.is_decimal(dtype):
            column = column.cast(pa.float64())
        elif pa.types.is_dictionary(dtype) or pa.types.is_null(dtype):
            column = column.cast(pa.large_string())
        elif pa.types.is_unsigned_integer(dtype):
            column = column.cast(pa.int64())
        elif pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
            column = column.cast(pa.large_string())
        fields.append(pa.field(field.name, column.type))
        columns.append(column)
    return pa.Table.from_arrays(
        [c.combine_chunks() if isinstance(c, pa.ChunkedArray) else c for c in columns],
        schema=pa.schema(fields))


def _iso_strings(column):
    """A temporal column as the ISO strings the JSON path produces: a date as
    `2024-01-01`, a timestamp as `2024-01-01T00:00:00`. Written out rather than
    left to a plain cast, which would give a date a time component it never
    had and break equality against a live tile's value.

    The trailing-zeros trim is what matches `datetime.isoformat()`, which omits
    the fractional part only when it is zero — a truncated bucket and a raw
    timestamp both have to render the same way here as they do in JSON, or a
    cross-filter value clicked on a live tile won't match one in an extract."""
    dtype = column.type
    if pa.types.is_date(dtype):
        return pc.strftime(column, format="%Y-%m-%d").cast(pa.large_string())
    # %S already carries the fractional part at sub-second precision, so the
    # only work left is dropping it when it is all zeros
    fmt = "%H:%M:%S" if pa.types.is_time(dtype) else "%Y-%m-%dT%H:%M:%S"
    formatted = pc.strftime(column, format=fmt)
    trimmed = pc.replace_substring_regex(formatted, pattern=r"\.0+$", replacement="")
    return trimmed.cast(pa.large_string())
