"""Semantic layer: YAML model definitions on top of S3 files.

A model maps a file source (parquet/csv/delta/iceberg on S3) to named
dimensions and measures, optionally joining in other sources (lookup/dimension
tables). Measures are SQL — a `expr:` holding an aggregate, and optionally a
`from:` holding the SELECT that aggregate reads. Both go through
app/sqlgrammar.py before they reach a connection, so nothing here has to
assume the YAML came from someone trusted; the structural parts of a model
(sources, joins, imports) still do.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import sqlgrammar

TIME_GRAINS = {"1d": "Day", "1w": "Week", "1mo": "Month", "1q": "Quarter", "1y": "Year"}
SOURCE_FORMATS = ("parquet", "csv", "delta", "iceberg")
JOIN_KINDS = ("left", "inner")
# dimension_imports additionally accept "between": an interval join, matching a
# date column on the imported side against a [start, end] pair on the importing
# model. See Import.is_interval and engine._join_interval.
IMPORT_JOIN_KINDS = ("left", "inner", "between")
# how a row's [start, end] interval is matched against a reporting period —
# see Spine.match / Import.match, and engine._period_conditions
#   overlap       counted in every period its interval touches at all
#   period_start  counted only where it was already open on the period's first day
#   period_end    counted only where it was still open on the period's last day
MATCH_MODES = ("overlap", "period_start", "period_end")
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


class ModelError(Exception):
    pass


MODEL_RELATION = "__model"
"""The name a measure's `from:` block uses for the fact scan.

`{model}` in authored YAML is substituted with this before validation, so the
validator's "which tables may this name" rule has one concrete answer, and the
engine's own CTE carries the same name."""


def render_from_block(source: str, dims: list[str]) -> str:
    """A `from:` block with its placeholders filled in.

    `{model}` becomes the engine's fact CTE. `{dims}` becomes the query's
    grouping columns — always at least one, because the engine carries a
    constant grouping column when the query groups by nothing, which is what
    makes `SELECT {dims}, x` and `GROUP BY {dims}, y` safe to write
    unconditionally."""
    columns = ", ".join(f'"{d}"' for d in dims) if dims else "TRUE"
    return source.replace("{model}", MODEL_RELATION).replace("{dims}", columns)


def validate_from_block(source: str, owner: str, dims: Optional[list[str]] = None) -> str:
    """Load-time and query-time check for a measure's `from:` block.

    At load time `dims` is None and a single placeholder column stands in for
    the query's grouping, which is enough to check everything structural: the
    block is one SELECT, it names no table but the model and its own CTEs, and
    it calls no table function. At query time the real dimensions are
    substituted and the same check runs again over the real text."""
    try:
        return sqlgrammar.compile_relation(
            render_from_block(source, dims if dims is not None else ["__dim"]),
            allowed_tables={MODEL_RELATION},
        )
    except sqlgrammar.SqlCompileError as exc:
        raise ModelError(f"{owner}: {exc}") from exc


@dataclass
class Source:
    path: str            # s3://bucket/prefix/*.parquet | .../table (delta/iceberg root)
    format: str = "parquet"


@dataclass
class Join:
    name: str
    source: Source
    left_on: list[str]
    right_on: list[str]
    how: str = "left"


@dataclass
class DatasetJoin:
    """A relation from one Dataset to a sibling Dataset in the same model or
    dimension bundle (as opposed to Join, which targets a raw Source).

    Declared on one side, read from both: the graph these edges form is
    undirected, and which end a scan starts from depends on where it enters
    (a bundle's anchor dataset, a model part's root fact table)."""
    to: str
    left_on: list[str]
    right_on: list[str]
    how: str = "left"


@dataclass
class Spine:
    """Marks a time dimension as a generated timeline: a row is counted in the
    time buckets its [start, end] interval matches, at whatever grain the query
    asks for. Null end = still active. `match` picks which periods count — by
    default every period the interval overlaps at all."""
    start: str
    end: str
    match: str = "overlap"


@dataclass
class Geo:
    """Coordinates for a dimension's members, enabling map visuals: the engine
    aggregates mean(lat)/mean(lon) alongside the measures when grouping."""
    lat: str
    lon: str


@dataclass
class Dimension:
    name: str
    column: str
    label: str
    type: str = "categorical"  # categorical | time | numeric
    description: str = ""
    spine: Optional[Spine] = None
    geo: Optional[Geo] = None
    # for a column of a date table imported with `how: between`: the size of
    # the bucket this column is constant across ("1mo" for a month label,
    # "1q" for a quarter). It tells the engine how far to thin the date table
    # before the interval join, so one row of it represents one bucket and
    # measures aggregate at that grain without counting a row once per day.
    # Absent means the table's own row grain (a plain day column, a weekday
    # flag) — see engine._join_interval.
    grain: Optional[str] = None
    # alternate business vocabulary a question might use instead of the
    # declared name/label (e.g. "date" for order_date) — advisory only, never
    # a second valid identifier: Model.dimension() still resolves by `name`
    # alone (see app/nlq.py's catalog, the one consumer that reads this)
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Measure:
    name: str
    label: str
    expr_source: str
    format: str = "number"  # number | currency | percent
    description: str = ""
    # optional intermediary step: the SQL SELECT that expr_source aggregates
    # over, instead of the fact scan. See render_from_block for its two
    # placeholders, and contracts/measure-sql.md for the shape.
    from_source: Optional[str] = None
    # dimensions the block computes itself (columns of the derived relation,
    # e.g. a per-entity milestone date): excluded from {dims} during the step,
    # and grouped — time grains included — on its output afterwards
    emits: list[str] = field(default_factory=list)
    # see Dimension.synonyms — same advisory-only contract, never a second
    # valid identifier for Model.measure()
    synonyms: list[str] = field(default_factory=list)

    def sql(self, schema: Optional[dict] = None, *, parameter_values: Optional[dict] = None,
            window_spec: str = "") -> str:
        """The SQL to embed for this measure, rendered from its own validated
        AST rather than from the author's text.

        `schema` is the relation the expression reads — the fact scan, the
        `from:` block's output, or (for a window measure) the aggregated
        result; None skips the column-existence check, which is what model load
        does rather than fetching a schema from S3 just to parse config.
        `window_spec` is the engine's PARTITION BY/ORDER BY, known only once a
        query has resolved its dimensions; without one a window measure still
        validates, against an empty window, which is all a load-time check
        needs."""
        try:
            return sqlgrammar.compile_expression(
                self.expr_source, schema, window=bool(window_spec) or self.is_window,
                window_spec=window_spec, parameter_values=parameter_values,
            )
        except sqlgrammar.SqlCompileError as exc:
            raise ModelError(f"measure '{self.name}': {exc}") from exc

    @property
    def is_window(self) -> bool:
        """Does this measure read across already-aggregated rows? Bare names
        inside one are sibling measures, and it is computed after the
        group-by rather than in it."""
        return sqlgrammar.is_window_expr(self.expr_source)


@dataclass
class Dataset:
    """A single source, the dimensions it exposes, and its relations to
    sibling datasets — the unit both a Model and a DimensionBundle are built
    out of.

    A bundle's datasets never declare measures (a common dimensional model has
    none to declare); a model's may, and a measure is what makes its dataset a
    *fact* table rather than a lookup one."""
    name: str
    source: Source
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    joins: list[DatasetJoin] = field(default_factory=list)
    measures: dict[str, Measure] = field(default_factory=dict)


@dataclass
class DimensionBundle:
    """A named, reusable set of Datasets (plus the joins between them),
    independent of any single fact Model. See Model.imports."""
    name: str
    label: str
    description: str
    datasets: dict[str, Dataset] = field(default_factory=dict)
    origin: Optional[Path] = None
    # every bundle is loaded from dimensions/*.yaml (there's no local-bundle
    # store, unlike Model) so this is always true today — it exists so
    # app/api/dimensions.py can refuse to edit/delete a built-in common model
    # the same way app/api/models.py refuses to for a locked Model.
    locked: bool = True

    def dataset(self, name: str) -> Dataset:
        try:
            return self.datasets[name]
        except KeyError:
            raise ModelError(f"unknown dataset '{name}' in dimension bundle '{self.name}'")


@dataclass
class Import:
    """A Model's reference to a DimensionBundle: which of the model's own
    datasets relates to it (`from_dataset`), which of the bundle's datasets
    that relation lands on (`anchor_dataset`), and an optional subset of the
    bundle's datasets to include (default: all of them).

    `from_dataset` decides which *part* of the model the bundle joins into —
    two fact tables in one model can each import the same bundle, which is
    exactly what conforms them (see resolve_model). Its keys are read from the
    whole part, so `left_on` may name a column of any dataset joined to
    `from_dataset`, not only that dataset's own.

    `how: between` makes it an *interval* import instead of an equality one:
    left_on is a [start, end] column pair on the model and right_on a single
    date column on the anchor dataset, and a model row matches every imported
    row whose date falls inside its interval. That is how a disconnected
    calendar table becomes point-in-time queryable — see engine._join_interval.
    """
    bundle: str
    anchor_dataset: str
    left_on: list[str]
    right_on: list[str]
    from_dataset: str = ""    # "" until resolve_model defaults it to the sole part's root
    how: str = "left"
    datasets: Optional[list[str]] = None  # None = whole bundle
    # interval imports only: which reporting periods a model row counts in —
    # see MATCH_MODES. Same field, same meaning and same default as Spine.match,
    # so the two mechanisms answer the same question.
    match: str = "overlap"

    @property
    def is_interval(self) -> bool:
        return self.how == "between"


@dataclass
class ImportBinding:
    """Resolved, engine-facing form of an Import - computed once at
    load/hot-reload time by _resolve_part_imports(), not part of the YAML shape."""
    import_spec: Import
    bundle: DimensionBundle
    included_datasets: list[str]           # BFS-reachable from anchor_dataset, subset-filtered
    dimension_owners: dict[str, str]        # imported dimension name -> owning dataset name


@dataclass
class ModelPart:
    """One connected component of a model's dataset graph: a fact table plus
    everything related to it, scanned as one joined relation.

    A model's datasets need not all be related to each other — two fact tables
    that share nothing but a common dimension model are the ordinary case (see
    resolve_model). Each component becomes a part, and a part is what the
    engine actually scans: `model` here is a synthetic single-source Model
    carrying that component's root source, its internal joins, its dimensions,
    its measures and the bundle imports declared against it.

    Parts are never joined to each other — that is the whole point. Joining
    them would pair every row of one fact with every matching row of the other
    and inflate both sides' measures; instead each is queried on its own and
    the per-part *results* are merged on the dimensions they share
    (engine._run_parts). A model with a single part is that part: `model` is
    the outer Model itself, and nothing about the query path changes.
    """
    name: str                # the root dataset's name — what names this fact table
    datasets: list[str]      # component members, root first, in join order
    model: "Model"           # single-source Model the engine scans for this part


@dataclass
class LineageFieldEntry:
    """One target field's declared lineage, as rendered by the owning
    pipeline (specs/014-polars-pipeline-module/) — see replace_lineage_yaml."""
    field: str
    sources: list[str] = field(default_factory=list)
    transform: str = ""
    stale: bool = False


@dataclass
class LineageSection:
    """The pipeline-owned `pipeline_lineage:` section of a model — entirely
    regenerated by the owning pipeline after each successful run; never
    hand-authored, never touched by the query engine."""
    pipeline: str
    updated: str = ""
    fields: list[LineageFieldEntry] = field(default_factory=list)
    orphaned: bool = False


@dataclass
class Model:
    name: str
    label: str
    description: str
    # `datasets` is the authored shape: every table this model reads, plus the
    # relations between them. The terser `source:`/`joins:` spelling of a
    # single fact table desugars into it at parse time (_parse_model), so from
    # here on there is only one shape to reason about.
    datasets: dict[str, Dataset] = field(default_factory=dict)
    # ...and `parts` is the resolved one: one per connected component of that
    # graph (resolve_model). The four fields below describe the *whole* model —
    # on a single-part model they are that part's own (and parts[0].model is
    # this object); on a multi-part one, `source`/`joins` are empty and the
    # catalog is merged: measures from every part, dimensions only those every
    # part offers.
    parts: list[ModelPart] = field(default_factory=list)
    source: Optional[Source] = None   # None when the model has several parts
    joins: list[Join] = field(default_factory=list)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
    imports: list[Import] = field(default_factory=list)
    import_bindings: list[ImportBinding] = field(default_factory=list)  # populated by resolve_model
    pipeline_lineage: Optional[LineageSection] = None  # tolerantly parsed; see _parse_lineage_section
    origin: Optional[Path] = None  # yaml file the model was loaded from; None for a local model
    # True for every model loaded from the committed models/ directory — the
    # built-in demo catalog, uneditable through the app regardless of role
    # (see app/api/models.py). False for a model loaded from LocalModelStore
    # (app/localmodelstore.py), which the registry sets after parsing it.
    locked: bool = True

    @property
    def is_composite(self) -> bool:
        """True when this model holds several unrelated fact tables: it has no
        single frame to scan, and engine.run_query answers it by querying each
        part separately and merging (see engine._run_parts)."""
        return len(self.parts) > 1

    def dimension(self, name: str) -> Dimension:
        try:
            return self.dimensions[name]
        except KeyError:
            raise ModelError(f"unknown dimension '{name}' in model '{self.name}'")

    def measure(self, name: str) -> Measure:
        try:
            return self.measures[name]
        except KeyError:
            raise ModelError(f"unknown measure '{name}' in model '{self.name}'")

    def to_public(self) -> dict:
        dim_sources = dimension_sources(self)
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "kind": "composite" if self.is_composite else "fact",
            "locked": self.locked,
            "path": self.source.path if self.source else None,
            "format": self.source.format if self.source else None,
            "file": self.origin.name if self.origin else None,
            "parts": [
                {"name": p.name, "label": p.model.label, "datasets": list(p.datasets),
                 "path": p.model.source.path if p.model.source else None,
                 "measures": list(p.model.measures)}
                for p in self.parts
            ],
            "joins": [{"name": j.name, "path": j.source.path, "format": j.source.format} for j in self.joins],
            "imports": [
                {"bundle": b.import_spec.bundle, "anchor_dataset": b.import_spec.anchor_dataset,
                 "datasets": b.import_spec.datasets}
                for b in self.import_bindings
            ],
            "dimensions": [
                {"name": d.name, "label": d.label, "type": d.type,
                 "description": d.description, "spine": bool(d.spine), "geo": bool(d.geo),
                 "synonyms": d.synonyms, "dataset": dim_sources.get(d.name, self.label)}
                for d in self.dimensions.values()
            ],
            "measures": [
                {"name": m.name, "label": m.label, "format": m.format,
                 "description": m.description, "expr": m.expr_source,
                 "from": m.from_source, "emits": m.emits,
                 "synonyms": m.synonyms}
                for m in self.measures.values()
            ],
            "pipeline_lineage": (
                {"pipeline": self.pipeline_lineage.pipeline,
                 "updated": self.pipeline_lineage.updated,
                 "orphaned": self.pipeline_lineage.orphaned,
                 "fields": [
                     {"field": f.field, "sources": f.sources, "transform": f.transform, "stale": f.stale}
                     for f in self.pipeline_lineage.fields
                 ]}
                if self.pipeline_lineage else None
            ),
        }


def _parse_lineage_section(raw: object) -> Optional[LineageSection]:
    """Tolerant parse of a model's `pipeline_lineage:` block — a malformed or
    hand-corrupted section never blocks the model from loading (it's
    documentation, not part of the query path); it just parses as absent."""
    if not isinstance(raw, dict):
        return None
    try:
        fields_raw = raw.get("fields") or []
        entries = [
            LineageFieldEntry(
                field=f["field"], sources=list(f.get("sources") or []),
                transform=f.get("transform", ""), stale=bool(f.get("stale", False)),
            )
            for f in fields_raw
        ]
        return LineageSection(
            pipeline=raw.get("pipeline", ""), updated=raw.get("updated", ""),
            fields=entries, orphaned=bool(raw.get("orphaned", False)),
        )
    except Exception:
        return None


def _parse_source(raw: dict, origin: Path) -> Source:
    source = Source(path=raw["path"], format=raw.get("format", "parquet"))
    if source.format not in SOURCE_FORMATS:
        raise ModelError(f"{origin.name}: unsupported source format '{source.format}'")
    return source


def _as_list(v) -> list[str]:
    return v if isinstance(v, list) else [v]


def _parse_join_keys(
    j: dict, owner: str, join_desc: str, kinds: tuple[str, ...] = JOIN_KINDS,
) -> tuple[list[str], list[str], str]:
    """Shared on/left_on/right_on/how resolution for both Join (model -> raw
    source) and DatasetJoin (dataset -> sibling dataset in a bundle). YAML 1.1
    parses a bare `on:` key as boolean True — accept both."""
    on = j.get("on", j.get(True))
    left_on = _as_list(j["left_on"] if "left_on" in j else on)
    right_on = _as_list(j["right_on"] if "right_on" in j else on)
    how = j.get("how", "left")
    if not left_on or left_on == [None]:
        raise ModelError(f"{owner}: {join_desc} needs 'on' or 'left_on'/'right_on'")
    if how not in kinds:
        raise ModelError(f"{owner}: {join_desc}: unsupported how '{how}'")
    return left_on, right_on, how


def _parse_dimensions(raw_list: list, owner: str) -> dict[str, Dimension]:
    """Shared `dimensions:` block parsing for both Model and Dataset."""
    dims: dict[str, Dimension] = {}
    for d in raw_list:
        spine_raw = d.get("spine")
        geo_raw = d.get("geo")
        dim = Dimension(
            name=d["name"],
            column=d.get("column", d["name"]),
            label=d.get("label", d["name"].replace("_", " ").title()),
            type=d.get("type", "categorical"),
            description=d.get("description", ""),
            spine=(Spine(start=spine_raw["start"], end=spine_raw["end"],
                         match=spine_raw.get("match", "overlap"))
                   if spine_raw else None),
            geo=Geo(lat=geo_raw["lat"], lon=geo_raw["lon"]) if geo_raw else None,
            grain=d.get("grain"),
            synonyms=_as_list(d["synonyms"]) if d.get("synonyms") else [],
        )
        if dim.spine and dim.type != "time":
            raise ModelError(f"{owner}: spine dimension '{dim.name}' must have type: time")
        if dim.spine and dim.spine.match not in MATCH_MODES:
            raise ModelError(
                f"{owner}: spine dimension '{dim.name}': 'match' must be one of "
                f"{', '.join(MATCH_MODES)}, got '{dim.spine.match}'"
            )
        if dim.grain is not None and dim.grain not in TIME_GRAINS:
            raise ModelError(
                f"{owner}: dimension '{dim.name}': unsupported grain '{dim.grain}' "
                f"(one of {', '.join(TIME_GRAINS)})"
            )
        dims[dim.name] = dim
    return dims


def _parse_import(raw: dict, owner: str) -> Import:
    anchor = raw.get("anchor_dataset")
    if not anchor:
        raise ModelError(f"{owner}: dimension_imports entry needs 'anchor_dataset'")
    desc = f"import of '{raw.get('bundle')}'"
    left_on, right_on, how = _parse_join_keys(raw, owner, desc, kinds=IMPORT_JOIN_KINDS)
    match = raw.get("match", "overlap")
    if how == "between":
        if match not in MATCH_MODES:
            raise ModelError(
                f"{owner}: {desc}: 'match' must be one of {', '.join(MATCH_MODES)}, "
                f"got '{match}'"
            )
        if len(left_on) != 2:
            raise ModelError(
                f"{owner}: {desc}: 'how: between' needs left_on: [start_column, end_column] — "
                f"the interval on this model, got {left_on}"
            )
        if len(right_on) != 1:
            raise ModelError(
                f"{owner}: {desc}: 'how: between' needs a single right_on column — "
                f"the date column on '{anchor}', got {right_on}"
            )
    datasets = raw.get("datasets")
    if datasets is not None and not isinstance(datasets, list):
        raise ModelError(f"{owner}: {desc}: 'datasets' must be a list")
    return Import(
        bundle=raw["bundle"], anchor_dataset=anchor, from_dataset=raw.get("from_dataset") or "",
        left_on=left_on, right_on=right_on, how=how, datasets=datasets, match=match,
    )


def _parse_measures(raw_list: list, owner: str) -> dict[str, Measure]:
    """Shared `measures:` block parsing — a model's datasets declare these; a
    dimension bundle's never do (see _parse_dataset)."""
    measures: dict[str, Measure] = {}
    for m in raw_list:
        if "frame" in m or "frame_emits" in m:
            raise ModelError(
                f"{owner}: measure '{m.get('name')}': 'frame:' was the python "
                "intermediary-frame construct and is gone — write the same step as "
                "SQL under 'from:' (and 'emits:' for a dimension it computes itself)")
        meas = Measure(
            name=m["name"],
            label=m.get("label", m["name"].replace("_", " ").title()),
            expr_source=m["expr"],
            format=m.get("format", "number"),
            description=m.get("description", ""),
            from_source=m.get("from"),
            emits=_as_list(m["emits"]) if m.get("emits") else [],
            synonyms=_as_list(m["synonyms"]) if m.get("synonyms") else [],
        )
        if meas.from_source:
            validate_from_block(meas.from_source, f"{owner}: measure '{meas.name}'")
        elif meas.emits:
            raise ModelError(f"{owner}: measure '{meas.name}': 'emits' needs a 'from'")
        meas.sql()  # validate at load time
        measures[meas.name] = meas
    return measures


# ---------------------------------------------------------------------------
# Datasets and the graph they form. A Model and a DimensionBundle are both a
# named set of Datasets plus the relations between them; the parsing and the
# graph walks below are shared by the two, and differ only in what a component
# of the graph *means*: a bundle is walked from an import's anchor to decide
# what that import pulls in, a model is split into components because every
# one of them is a fact table to be scanned on its own.
# ---------------------------------------------------------------------------

def _parse_dataset_join(j: dict, origin: Path, owner: str) -> DatasetJoin:
    to = j.get("to")
    if not to:
        raise ModelError(f"{origin.name}: {owner}: dataset join needs 'to'")
    left_on, right_on, how = _parse_join_keys(j, origin.name, f"{owner}: join to '{to}'")
    return DatasetJoin(to=to, left_on=left_on, right_on=right_on, how=how)


def _parse_dataset(raw: dict, origin: Path, with_measures: bool = False) -> Dataset:
    try:
        dataset = Dataset(name=raw["name"], source=_parse_source(raw["source"], origin))
    except KeyError as exc:
        raise ModelError(f"{origin.name}: dataset missing required key {exc}") from exc
    owner = f"dataset '{dataset.name}'"
    dataset.dimensions = _parse_dimensions(raw.get("dimensions", []), f"{origin.name}: {owner}")
    if raw.get("measures"):
        if not with_measures:
            raise ModelError(
                f"{origin.name}: {owner}: a common dimensional model's datasets declare no "
                f"measures — they provide shared dimensions; measures belong to the fact models "
                f"that import them"
            )
        dataset.measures = _parse_measures(raw["measures"], f"{origin.name}: {owner}")
    for j in raw.get("joins", []):
        dataset.joins.append(_parse_dataset_join(j, origin, owner))
    return dataset


def _dataset_edges(datasets: dict[str, Dataset]) -> dict[str, set[str]]:
    """Undirected adjacency: a DatasetJoin declared on either side makes both
    datasets reachable from each other, whichever end the walk starts at."""
    edges: dict[str, set[str]] = {name: set() for name in datasets}
    for ds in datasets.values():
        for j in ds.joins:
            edges[ds.name].add(j.to)
            edges[j.to].add(ds.name)
    return edges


def _check_dataset_graph(datasets: dict[str, Dataset], owner: str) -> None:
    """Every relation points at a dataset that exists, and no cycles: a cycle
    would give the join walk two different routes to the same table, and so
    two different answers."""
    for ds in datasets.values():
        for j in ds.joins:
            if j.to not in datasets:
                raise ModelError(f"{owner}: dataset '{ds.name}' relates to unknown dataset '{j.to}'")
    edges = _dataset_edges(datasets)
    visited: set[str] = set()

    def dfs(node: str, parent: Optional[str]) -> None:
        visited.add(node)
        for neighbor in edges[node]:
            if neighbor == parent:
                continue
            if neighbor in visited:
                raise ModelError(
                    f"{owner}: cyclical relation between datasets '{node}' and '{neighbor}'")
            dfs(neighbor, node)

    for start in datasets:
        if start not in visited:
            dfs(start, None)


def _components(datasets: dict[str, Dataset]) -> list[list[str]]:
    """Connected components of the relation graph, each in declaration order,
    the components themselves ordered by their first-declared member.

    For a model this is the whole point: datasets that relate to each other
    are one fact table to scan, and datasets that don't are separate ones that
    must never be joined (see ModelPart)."""
    edges = _dataset_edges(datasets)
    seen: set[str] = set()
    out: list[list[str]] = []
    for start in datasets:
        if start in seen:
            continue
        group: set[str] = {start}
        frontier = [start]
        while frontier:
            node = frontier.pop()
            for neighbor in edges[node]:
                if neighbor not in group:
                    group.add(neighbor)
                    frontier.append(neighbor)
        seen |= group
        out.append([name for name in datasets if name in group])
    return out


_FACTS_REMOVED = (
    "'facts:' is no longer supported — a model no longer borrows other models' fact tables. "
    "Declare the fact tables here instead, one entry per table under 'datasets:', each with its "
    "own source, dimensions and measures. Datasets that relate to nothing else in the model stay "
    "unjoined and are read exactly the way facts: used to be: separately, merged on the dimensions "
    "they share (import the same common model into each to conform them)"
)


def _model_datasets(raw: dict, origin: Path) -> dict[str, Dataset]:
    """The model's datasets, from either spelling.

    `datasets:` is the general shape — every table the model reads, plus the
    relations between them. `source:` (+ optional `joins:`) is the terser
    spelling of the single-fact case, and desugars into exactly the same
    thing: the model's own dataset, named after the model, related to one
    dataset per join entry. Everything downstream sees only `datasets`.
    """
    if raw.get("facts"):
        raise ModelError(f"{origin.name}: model '{raw.get('name')}': {_FACTS_REMOVED}")
    if raw.get("datasets"):
        stray = [key for key in ("source", "joins", "dimensions", "measures") if raw.get(key)]
        if stray:
            raise ModelError(
                f"{origin.name}: model '{raw['name']}' declares both 'datasets' and "
                f"{', '.join(repr(k) for k in stray)} — those keys are the shorthand for a model "
                f"with a single dataset, and mixing the two would silently ignore them. Move each "
                f"into the 'datasets' entry it belongs to"
            )
        datasets: dict[str, Dataset] = {}
        for d in raw["datasets"]:
            dataset = _parse_dataset(d, origin, with_measures=True)
            if dataset.name in datasets:
                raise ModelError(
                    f"{origin.name}: model '{raw['name']}': duplicate dataset '{dataset.name}'")
            datasets[dataset.name] = dataset
        return datasets

    if not raw.get("source"):
        raise ModelError(
            f"{origin.name}: model '{raw['name']}' has no datasets — list the tables it reads "
            f"under 'datasets', or use 'source' for the single-table case"
        )
    name = raw["name"]
    root = Dataset(name=name, source=_parse_source(raw["source"], origin))
    root.dimensions = _parse_dimensions(raw.get("dimensions", []), origin.name)
    root.measures = _parse_measures(raw.get("measures", []), origin.name)
    datasets = {name: root}
    for j in raw.get("joins", []):
        join_name = j.get("name", "join")
        left_on, right_on, how = _parse_join_keys(j, origin.name, f"join '{join_name}'")
        if join_name in datasets:
            raise ModelError(
                f"{origin.name}: model '{name}': join '{join_name}' collides with the model's "
                f"own dataset name — rename the join"
            )
        datasets[join_name] = Dataset(name=join_name, source=_parse_source(j["source"], origin))
        root.joins.append(DatasetJoin(to=join_name, left_on=left_on, right_on=right_on, how=how))
    return datasets


def _parse_model(raw: dict, origin: Path) -> Model:
    try:
        model = Model(
            name=raw["name"],
            label=raw.get("label", raw["name"]),
            description=raw.get("description", ""),
        )
        model.datasets = _model_datasets(raw, origin)
        for imp in raw.get("dimension_imports", []):
            model.imports.append(_parse_import(imp, origin.name))
    except KeyError as exc:
        raise ModelError(f"{origin.name}: missing required key {exc}") from exc
    _check_dataset_graph(model.datasets, f"{origin.name}: model '{model.name}'")
    _assign_imports(model, _components(model.datasets))
    _split_parts(model)
    model.pipeline_lineage = _parse_lineage_section(raw.get("pipeline_lineage"))
    return model


class _BlockStrDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings (a measure's `from:` block,
    a pipeline's `sql:`) as literal `|` blocks instead of quoted strings full
    of \\n escapes."""


def _repr_str(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockStrDumper.add_representer(str, _repr_str)


def _measure_yaml_block(measure: dict) -> str:
    """Render a single measure to an indented yaml block, as used by both
    append_measure_yaml and replace_measure_yaml. yaml handles the quoting of
    the new block itself."""
    block = yaml.dump([measure], Dumper=_BlockStrDumper, default_flow_style=False, sort_keys=False, width=1000)
    return "".join("  " + line + "\n" for line in block.rstrip("\n").split("\n"))


def _yaml_block_end(lines: list[str], start: int, is_boundary: Callable[[str], bool]) -> tuple[int, int]:
    """Scan forward from `start` for the end of the yaml block beginning there:
    `end` is the index of the first following line matching `is_boundary` (or
    EOF), and `last_content` is the last non-blank line index inside the block
    — shared by append_measure_yaml (scanning to the end of `measures:`) and
    _measure_block_bounds (scanning to the end of one list entry)."""
    end = len(lines)
    last_content = start
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and is_boundary(line):
            end = i
            break
        if line.strip():
            last_content = i
    return end, last_content


def append_measure_yaml(text: str, measure: dict) -> str:
    """Insert a measure at the end of the `measures:` block of a model's yaml,
    preserving the rest of the file byte-for-byte (comments included)."""
    block = _measure_yaml_block(measure)

    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "measures:"), None)
    if start is None:
        return text.rstrip("\n") + "\n\nmeasures:\n" + block

    # the block ends before the next top-level key (or EOF); remember the last
    # line that actually belongs to it so trailing blanks stay trailing
    end, last_content = _yaml_block_end(lines, start, lambda line: not line.startswith((" ", "\t", "#")))
    insert_at = min(last_content + 1, end)
    return "\n".join(lines[:insert_at]) + "\n" + block + "\n".join(lines[insert_at:])


def _measure_block_bounds(lines: list[str], measure_name: str) -> Optional[tuple[int, int]]:
    """Find the [start, end) line range of a `  - name: <measure_name>` entry
    as rendered by append_measure_yaml — None if no such entry exists."""
    start = next(
        (i for i, line in enumerate(lines)
         if line.startswith("  - ") and line.strip() == f"- name: {measure_name}"),
        None,
    )
    if start is None:
        return None
    _, last_content = _yaml_block_end(
        lines, start, lambda line: line.startswith("  - ") or not line.startswith((" ", "\t"))
    )
    return start, last_content + 1


def replace_measure_yaml(text: str, measure_name: str, measure: dict) -> str:
    """Rewrite an existing measure's block in place, preserving the rest of
    the file (comments included) — the update counterpart of append_measure_yaml."""
    lines = text.split("\n")
    bounds = _measure_block_bounds(lines, measure_name)
    if bounds is None:
        raise ModelError(f"measure '{measure_name}' not found in yaml")
    start, end = bounds
    block = _measure_yaml_block(measure)
    return "\n".join(lines[:start]) + "\n" + block + "\n".join(lines[end:])


def remove_measure_yaml(text: str, measure_name: str) -> str:
    """Delete an existing measure's block, preserving the rest of the file."""
    lines = text.split("\n")
    bounds = _measure_block_bounds(lines, measure_name)
    if bounds is None:
        raise ModelError(f"measure '{measure_name}' not found in yaml")
    start, end = bounds
    return "\n".join(lines[:start] + lines[end:])


# ---------------------------------------------------------------------------
# pipeline_lineage: section (specs/014-polars-pipeline-module/) — a single
# top-level key entirely owned by the pipeline that targets this model,
# regenerated after every successful run. Same comment-preserving text-
# surgery family as append_measure_yaml/replace_measure_yaml above, but for
# one top-level key rather than one entry inside a list.
# ---------------------------------------------------------------------------

LINEAGE_BANNER_PREFIX = "# ── managed by pipeline"


def _lineage_yaml_block(section: dict) -> str:
    banner = f"{LINEAGE_BANNER_PREFIX} '{section['pipeline']}' — do not hand-edit this section ──\n"
    body = yaml.dump({"pipeline_lineage": section}, Dumper=_BlockStrDumper,
                      default_flow_style=False, sort_keys=False, width=1000)
    return banner + body


def replace_lineage_yaml(text: str, section: dict) -> str:
    """Regenerate the pipeline-owned `pipeline_lineage:` section (banner +
    block) — idempotent, appended at the end of the file when absent,
    replaced in place when present; everything else in the file (comments
    included) is preserved byte for byte. `section` is the plain dict shape
    from Pipeline lineage-section building (pipeline/updated/orphaned/fields)."""
    lines = text.split("\n")
    key_idx = next((i for i, line in enumerate(lines) if line.rstrip() == "pipeline_lineage:"), None)
    block = _lineage_yaml_block(section)
    if key_idx is None:
        return text.rstrip("\n") + "\n\n" + block
    start = key_idx - 1 if (key_idx > 0 and lines[key_idx - 1].startswith(LINEAGE_BANNER_PREFIX)) else key_idx
    end, _ = _yaml_block_end(lines, key_idx, lambda line: not line.startswith((" ", "\t", "#")))
    prefix = "\n".join(lines[:start])
    suffix = "\n".join(lines[end:])
    return (prefix + ("\n" if prefix else "")) + block + suffix


def _parse_editor_text(text: str, mapping_desc: str, parser: Callable[[dict, Path], object]):
    """Shared load/validate step for parse_model_text and parse_bundle_text:
    both take editor-supplied YAML text and hand a raw mapping to their
    respective `_parse_*` function, differing only in the expected shape."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ModelError(f"invalid yaml: {exc}")
    if not isinstance(raw, dict):
        raise ModelError(f"yaml must be a mapping with {mapping_desc}")
    return parser(raw, Path("<editor>"))


def parse_model_text(text: str) -> Model:
    """Parse and validate a model from editor-supplied YAML text."""
    return _parse_editor_text(text, "name / datasets (or source / dimensions / measures)", _parse_model)


def _load_yaml_dir(directory: Path, parser: Callable[[dict, Path], object]) -> dict:
    """Shared directory-of-yaml-files loader for load_models and
    load_dimension_bundles: parse every *.yml/*.yaml file and index the
    resulting objects (which both set .origin and .name) by name."""
    items: dict = {}
    if not directory.is_dir():
        return items
    for path in sorted(directory.glob("*.y*ml")):
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        item = parser(raw, path)
        item.origin = path
        items[item.name] = item
    return items


def load_models(models_dir: Path) -> dict[str, Model]:
    return _load_yaml_dir(models_dir, _parse_model)


# ---------------------------------------------------------------------------
# Dimension bundles (common dimensional models) and import resolution.
# A bundle groups reusable Datasets, declared once, that any fact Model can
# import by name instead of re-declaring the same source/joins/dimensions.
# ---------------------------------------------------------------------------

def _check_no_cross_dataset_collisions(bundle: DimensionBundle) -> None:
    owner_of: dict[str, str] = {}
    for ds in bundle.datasets.values():
        for dim_name in ds.dimensions:
            if dim_name in owner_of and owner_of[dim_name] != ds.name:
                raise ModelError(
                    f"dimension bundle '{bundle.name}': dimension '{dim_name}' is declared "
                    f"by both dataset '{owner_of[dim_name]}' and dataset '{ds.name}' — rename one"
                )
            owner_of[dim_name] = ds.name


def _parse_bundle(raw: dict, origin: Path) -> DimensionBundle:
    try:
        bundle = DimensionBundle(
            name=raw["name"],
            label=raw.get("label", raw["name"]),
            description=raw.get("description", ""),
        )
        for d in raw.get("datasets", []):
            dataset = _parse_dataset(d, origin)
            if dataset.name in bundle.datasets:
                raise ModelError(f"{origin.name}: bundle '{bundle.name}': duplicate dataset '{dataset.name}'")
            bundle.datasets[dataset.name] = dataset
    except KeyError as exc:
        raise ModelError(f"{origin.name}: missing required key {exc}") from exc
    if not bundle.datasets:
        raise ModelError(f"{origin.name}: dimension bundle '{bundle.name}' has no datasets")
    _check_dataset_graph(bundle.datasets, f"{origin.name}: dimension bundle '{bundle.name}'")
    _check_no_cross_dataset_collisions(bundle)
    return bundle


def parse_bundle_text(text: str) -> DimensionBundle:
    """Parse and validate a dimension bundle from editor-supplied YAML text."""
    return _parse_editor_text(text, "name / datasets", _parse_bundle)


def load_dimension_bundles(dimensions_dir: Path) -> dict[str, DimensionBundle]:
    return _load_yaml_dir(dimensions_dir, _parse_bundle)


# ---------------------------------------------------------------------------
# Structured spec <-> YAML: the guided modelling form edits a plain-dict spec
# (one key per YAML concept) and the server renders it to canonical YAML.
# model_to_spec() is the inverse, built from a freshly-parsed (unresolved)
# Model so the form can open an existing file. Round-trips are semantically
# lossless; comments and hand-formatting are not preserved on form saves.
# ---------------------------------------------------------------------------

GENERATED_HEADER = (
    "# Generated by the Modelling workspace form. Editing this file by hand is\n"
    "# fine — but a later save from the form rewrites it (comments included).\n"
)


def _dimension_to_spec(d: Dimension) -> dict:
    return {
        "name": d.name, "column": d.column, "label": d.label, "type": d.type,
        "description": d.description,
        "spine": ({"start": d.spine.start, "end": d.spine.end, "match": d.spine.match}
                  if d.spine else None),
        "geo": {"lat": d.geo.lat, "lon": d.geo.lon} if d.geo else None,
        "grain": d.grain,
        "synonyms": list(d.synonyms),
    }


def _measure_to_spec(m: Measure) -> dict:
    return {
        "name": m.name, "label": m.label, "expr": m.expr_source,
        "format": m.format, "description": m.description,
        "from": m.from_source, "emits": list(m.emits),
        "synonyms": list(m.synonyms),
    }


def _dataset_to_spec(ds: Dataset) -> dict:
    return {
        "name": ds.name,
        "source": {"path": ds.source.path, "format": ds.source.format},
        "dimensions": [_dimension_to_spec(d) for d in ds.dimensions.values()],
        "measures": [_measure_to_spec(m) for m in ds.measures.values()],
        "joins": [{"to": j.to, "left_on": j.left_on, "right_on": j.right_on, "how": j.how}
                  for j in ds.joins],
    }


def model_to_spec(model: Model) -> dict:
    """Form-facing dict for a parsed-but-unresolved Model (native dimensions
    only — imported dimensions live in the bundle, not this file).

    Both spellings of a model parse into `datasets`, so a file written the
    terse `source:`/`joins:` way opens in the form as the datasets it always
    was, and saving rewrites it in the general shape."""
    return {
        "name": model.name,
        "label": model.label,
        "description": model.description,
        "datasets": [_dataset_to_spec(ds) for ds in model.datasets.values()],
        "dimension_imports": [
            {"bundle": i.bundle, "from_dataset": i.from_dataset, "anchor_dataset": i.anchor_dataset,
             "left_on": i.left_on, "right_on": i.right_on, "how": i.how,
             "datasets": i.datasets, "match": i.match}
            for i in model.imports
        ],
    }


def bundle_to_spec(bundle: DimensionBundle) -> dict:
    """Form-facing dict for a parsed DimensionBundle — the common-model
    counterpart of model_to_spec."""
    return {
        "name": bundle.name,
        "label": bundle.label,
        "description": bundle.description,
        "datasets": [
            {k: v for k, v in _dataset_to_spec(ds).items() if k != "measures"}
            for ds in bundle.datasets.values()
        ],
    }


def _spec_dimension_entries(dims: list[dict]) -> list[dict]:
    """Spec dimension dicts -> tersest correct yaml entries (defaults omitted)."""
    out = []
    for d in dims:
        entry = {"name": d["name"]}
        if d.get("column") and d["column"] != d["name"]:
            entry["column"] = d["column"]
        if d.get("label"):
            entry["label"] = d["label"]
        if d.get("type", "categorical") != "categorical":
            entry["type"] = d["type"]
        if d.get("description"):
            entry["description"] = d["description"]
        if d.get("grain"):
            entry["grain"] = d["grain"]
        if d.get("spine"):
            entry["spine"] = {"start": d["spine"]["start"], "end": d["spine"]["end"]}
            if d["spine"].get("match", "overlap") != "overlap":
                entry["spine"]["match"] = d["spine"]["match"]
        if d.get("geo"):
            entry["geo"] = {"lat": d["geo"]["lat"], "lon": d["geo"]["lon"]}
        if d.get("synonyms"):
            entry["synonyms"] = list(d["synonyms"])
        out.append(entry)
    return out


def _spec_join_keys(entry: dict, spec: dict) -> None:
    """Emit the tersest correct key form: `on` when both sides match (scalar
    when single), `left_on`/`right_on` (scalars when single) otherwise."""
    left = [str(c) for c in spec.get("left_on") or []]
    right = [str(c) for c in spec.get("right_on") or []]
    scalar = lambda keys: keys[0] if len(keys) == 1 else keys
    if left and left == right:
        entry["on"] = scalar(left)
    else:
        entry["left_on"] = scalar(left)
        entry["right_on"] = scalar(right)
    if spec.get("how", "left") != "left":
        entry["how"] = spec["how"]


def _spec_header(spec: dict) -> dict:
    """name/label/description prelude shared by spec_to_yaml and
    bundle_spec_to_yaml (defaults omitted)."""
    doc: dict = {"name": spec["name"]}
    if spec.get("label"):
        doc["label"] = spec["label"]
    if spec.get("description"):
        doc["description"] = spec["description"]
    return doc


def _spec_measure_entries(measures: list[dict]) -> list[dict]:
    """Spec measure dicts -> tersest correct yaml entries (defaults omitted)."""
    out = []
    for m in measures:
        entry = {"name": m["name"]}
        if m.get("label"):
            entry["label"] = m["label"]
        if m.get("format", "number") != "number":
            entry["format"] = m["format"]
        if m.get("description"):
            entry["description"] = m["description"]
        if m.get("from"):
            entry["from"] = m["from"]
        if m.get("emits"):
            entry["emits"] = list(m["emits"])
        if m.get("synonyms"):
            entry["synonyms"] = list(m["synonyms"])
        entry["expr"] = m["expr"]
        out.append(entry)
    return out


def _spec_dataset_entry(ds: dict, with_measures: bool) -> dict:
    entry: dict = {
        "name": ds["name"],
        "source": {"format": ds["source"].get("format", "parquet"), "path": ds["source"]["path"]},
    }
    joins = []
    for j in ds.get("joins") or []:
        join_entry = {"to": j["to"]}
        _spec_join_keys(join_entry, j)
        joins.append(join_entry)
    if joins:
        entry["joins"] = joins
    entry["dimensions"] = _spec_dimension_entries(ds.get("dimensions") or [])
    if with_measures:
        entry["measures"] = _spec_measure_entries(ds.get("measures") or [])
    return entry


def spec_to_yaml(spec: dict) -> str:
    """Render a form spec dict to canonical model YAML (defaults omitted).

    Always the general `datasets:` shape, even for a single table — the terse
    `source:` spelling stays readable and supported for hand-written files, but
    one shape out of the generator means opening a model in the form and saving
    it never changes what it is."""
    doc = _spec_header(spec)
    doc["datasets"] = [_spec_dataset_entry(ds, with_measures=True)
                       for ds in spec.get("datasets") or []]

    imports = []
    for i in spec.get("dimension_imports") or []:
        entry = {"bundle": i["bundle"]}
        if i.get("from_dataset"):
            entry["from_dataset"] = i["from_dataset"]
        entry["anchor_dataset"] = i["anchor_dataset"]
        _spec_join_keys(entry, i)
        if i.get("how") == "between" and i.get("match", "overlap") != "overlap":
            entry["match"] = i["match"]
        if i.get("datasets") is not None:
            entry["datasets"] = list(i["datasets"])
        imports.append(entry)
    if imports:
        doc["dimension_imports"] = imports

    return _dump_generated(doc)


def _dump_generated(doc: dict) -> str:
    text = yaml.dump(doc, Dumper=_BlockStrDumper, sort_keys=False, default_flow_style=False, width=1000, allow_unicode=True)
    # yaml 1.1 quotes the boolean-ish `on` key; hand-written models use it bare
    # (the parser accepts both — see _parse_join_keys)
    text = re.sub(r"^(\s*(?:- )?)'on':", r"\1on:", text, flags=re.MULTILINE)
    return GENERATED_HEADER + text


def bundle_spec_to_yaml(spec: dict) -> str:
    """Render a form spec dict to canonical dimension-bundle YAML."""
    doc = _spec_header(spec)
    doc["datasets"] = [_spec_dataset_entry(ds, with_measures=False)
                       for ds in spec.get("datasets") or []]
    return _dump_generated(doc)


# ---------------------------------------------------------------------------
# Dataset discovery: group raw bucket objects into pickable "datasets" for the
# modelling workspace's source picker. Pure helpers (no S3 access) so they are
# unit-testable; app/api/datasets.py layers the bucket walk + model-mapping on top.
# ---------------------------------------------------------------------------

_EXT_FORMAT = {".parquet": "parquet", ".csv": "csv"}
_DELTA_MARKER = "/_delta_log/"
_ICEBERG_METADATA_RE = re.compile(r"^(.*)/metadata/\d+-[^/]+\.metadata\.json$")


def infer_format(keys: list[str]) -> tuple[Optional[str], bool]:
    """Infer a source format for a group of object keys from their extensions.
    Returns (format, ambiguous): format is None when no key has a recognized data
    extension; ambiguous is True when recognized extensions disagree (the picker
    warns but still lets the caller select, using the dominant format)."""
    counts: dict[str, int] = {}
    for key in keys:
        for ext, fmt in _EXT_FORMAT.items():
            if key.lower().endswith(ext):
                counts[fmt] = counts.get(fmt, 0) + 1
                break
    if not counts:
        return None, False
    dominant = max(counts, key=lambda f: counts[f])
    return dominant, len(counts) > 1


def _dirname(key: str) -> str:
    return key.rsplit("/", 1)[0] if "/" in key else ""


def _object_format(key: str) -> Optional[str]:
    for ext, fmt in _EXT_FORMAT.items():
        if key.lower().endswith(ext):
            return fmt
    return None


def model_source_matchers(
    models, bucket: str
) -> list[tuple[str, str, Callable[[str], bool]]]:
    """(model_name, role, match_fn) triples over each model's source/join/import
    globs — which bucket objects feed which model. Shared by the explorer and
    dataset-picker endpoints, which both tag bucket objects with their readers."""
    prefix = f"s3://{bucket}/"
    matchers: list[tuple[str, str, Callable[[str], bool]]] = []
    for m in models:
        sources: list[tuple[str, Source]] = []
        for part in m.parts:
            root_role = "source" if len(m.parts) == 1 else f"source: {part.name}"
            for i, name in enumerate(part.datasets):
                sources.append((root_role if i == 0 else f"join: {name}", m.datasets[name].source))
            sources += [(f"import: {b.bundle.name}.{ds}", b.bundle.datasets[ds].source)
                        for b in part.model.import_bindings for ds in b.included_datasets]
        for role, src in sources:
            if not src.path.startswith(prefix):
                continue
            rel = src.path[len(prefix):]
            if src.format in ("delta", "iceberg"):
                root = rel.rstrip("/") + "/"
                matchers.append((m.name, role, lambda k, r=root: k.startswith(r)))
            else:
                matchers.append((m.name, role, lambda k, p=rel: fnmatch.fnmatch(k, p)))
    return matchers


def per_model_stats(
    objects: list[dict], matchers: list[tuple[str, str, Callable[[str], bool]]], model_names,
) -> dict[str, dict]:
    """Per-model file count + byte total over ``objects`` (each ``{"key", "size"}``),
    zero-filled for every name in ``model_names``. An object matching a model via more
    than one role (its source and a join, say) still counts once — mirrors the
    per-object dedup the explorer and dataset-picker endpoints both need."""
    stats = {name: {"files": 0, "bytes": 0} for name in model_names}
    for o in objects:
        for name in {name for name, _role, match in matchers if match(o["key"])}:
            stats[name]["files"] += 1
            stats[name]["bytes"] += o.get("size", 0)
    return stats


def _table_root(key: str) -> Optional[tuple[str, str]]:
    """(root, format) if `key` belongs to a self-describing table directory —
    Delta's ``_delta_log/`` marker, or an Iceberg ``metadata/<version>-
    *.metadata.json`` file — else None."""
    if _DELTA_MARKER in key:
        return key.split(_DELTA_MARKER, 1)[0], "delta"
    m = _ICEBERG_METADATA_RE.match(key)
    if m:
        return m.group(1), "iceberg"
    return None


def group_objects(objects: list[dict], bucket: str) -> list[dict]:
    """Group bucket objects (each ``{"key", "size"}``) into pickable datasets.

    A Delta table (any object under a ``_delta_log/`` marker) or an Iceberg
    table (any ``metadata/<version>-*.metadata.json`` file) collapses into a
    single dataset rooted at the table directory; every other object groups
    by its directory prefix into a format-inferred glob source. Prefixes
    whose objects carry no recognized data extension are dropped (they cannot
    back a valid source). Pure — no S3 access; ``bucket`` only builds paths."""
    table_roots: dict[str, str] = {}   # root -> format, first-seen order
    for obj in objects:
        found = _table_root(obj["key"])
        if found:
            root, fmt = found
            table_roots.setdefault(root, fmt)

    def root_of(key: str) -> Optional[str]:
        for root in table_roots:
            if key == root or key.startswith(root + "/"):
                return root
        return None

    # bucket every object by its table root (or None) in a single pass,
    # rather than rescanning all objects against root_of once per root below
    table_members: dict[str, list[dict]] = {root: [] for root in table_roots}
    ungrouped: list[dict] = []
    for obj in objects:
        root = root_of(obj["key"])
        (table_members[root] if root is not None else ungrouped).append(obj)

    datasets: list[dict] = []

    for root, fmt in table_roots.items():
        members = table_members[root]
        datasets.append({
            "key": root,
            "path": f"s3://{bucket}/{root}",
            "format": fmt,
            "format_ambiguous": False,
            "object_count": len(members),
            "bytes": sum(o.get("size", 0) for o in members),
            "objects": [{"key": o["key"], "size": o.get("size", 0), "format": fmt} for o in members],
        })

    groups: dict[str, list[dict]] = {}
    for obj in ungrouped:
        groups.setdefault(_dirname(obj["key"]), []).append(obj)

    for prefix, members in groups.items():
        fmt, ambiguous = infer_format([o["key"] for o in members])
        if fmt is None:
            continue
        ext = next(e for e, f in _EXT_FORMAT.items() if f == fmt)
        glob = f"s3://{bucket}/{prefix + '/' if prefix else ''}*{ext}"
        datasets.append({
            "key": prefix,
            "path": glob,
            "format": fmt,
            "format_ambiguous": ambiguous,
            "object_count": len(members),
            "bytes": sum(o.get("size", 0) for o in members),
            "objects": [
                {"key": o["key"], "size": o.get("size", 0), "format": _object_format(o["key"]) or fmt}
                for o in members
            ],
        })

    datasets.sort(key=lambda d: d["key"])
    return datasets


def _bfs_reachable(bundle: DimensionBundle, start: str, allowed: set[str]) -> list[str]:
    """Datasets reachable from `start` walking only through `allowed` nodes —
    excluding a dataset also prunes anything reachable only through it."""
    edges = _dataset_edges(bundle.datasets)
    order = [start]
    frontier = [start]
    seen = {start}
    while frontier:
        node = frontier.pop()
        for neighbor in edges[node]:
            if neighbor in seen or neighbor not in allowed:
                continue
            seen.add(neighbor)
            order.append(neighbor)
            frontier.append(neighbor)
    return order


def dimension_sources(model: Model) -> dict[str, str]:
    """Best-effort label for where each of `model`'s dimensions comes from:
    the common-dimension bundle that supplied it via an import, else — when
    the model holds several fact tables — the dataset that declares it. What
    the Studio groups the dimension list into folders by (see Model.to_public).
    A model with a single fact table has one place its native dimensions can
    come from, so they read as the model's own label."""
    owners: dict[str, str] = {}
    for part in model.parts:
        for binding in part.model.import_bindings:
            for dim_name in binding.dimension_owners:
                owners.setdefault(dim_name, binding.bundle.label)
    if model.is_composite:
        for ds in model.datasets.values():
            for dim_name in ds.dimensions:
                owners.setdefault(dim_name, ds.name)
    return {name: owners.get(name, model.label) for name in model.dimensions}


def _resolve_part_imports(model: Model, bundles: dict[str, DimensionBundle]) -> Model:
    """Merge each of one part's declared imports into its dimensions and
    attach the ImportBinding metadata engine.scan() needs to build the join
    chain. A native dimension always shadows a same-named imported one; a
    same-named dimension offered by two different imports is a load-time
    error (subset one of the imports to resolve it). Mutates and returns
    `model` (a ModelPart's synthetic single-source Model, which for a
    single-part model is the model itself)."""
    native_names = set(model.dimensions)
    claimed: dict[str, str] = {}  # dimension name -> "bundle.dataset" that claimed it
    model.import_bindings = []

    for imp in model.imports:
        bundle = bundles.get(imp.bundle)
        if bundle is None:
            raise ModelError(f"model '{model.name}': imports unknown dimension bundle '{imp.bundle}'")
        if imp.anchor_dataset not in bundle.datasets:
            raise ModelError(
                f"model '{model.name}': import of '{imp.bundle}' anchors to unknown "
                f"dataset '{imp.anchor_dataset}'"
            )
        if imp.datasets is not None:
            unknown = [d for d in imp.datasets if d not in bundle.datasets]
            if unknown:
                raise ModelError(f"model '{model.name}': import of '{imp.bundle}' names unknown dataset(s) {unknown}")
            if imp.anchor_dataset not in imp.datasets:
                raise ModelError(
                    f"model '{model.name}': import of '{imp.bundle}' anchors to "
                    f"'{imp.anchor_dataset}', which is not in its own 'datasets' subset"
                )

        allowed = set(imp.datasets) if imp.datasets is not None else set(bundle.datasets)
        included = _bfs_reachable(bundle, imp.anchor_dataset, allowed)

        # naming a dataset that no chain of joins connects to the anchor used to
        # drop it silently: the import looked right in the yaml and in the form,
        # but its dimensions never appeared in the builder. Say so instead.
        if imp.datasets is not None:
            stranded = [d for d in imp.datasets if d not in set(included)]
            if stranded:
                raise ModelError(
                    f"model '{model.name}': import of '{imp.bundle}' names dataset(s) {stranded}, "
                    f"which no chain of joins connects to anchor '{imp.anchor_dataset}' — their "
                    f"dimensions would never load. Either declare a join to them in the bundle, or "
                    f"import them as their own entry (a disconnected calendar/date table anchors "
                    f"itself and joins with 'how: between')"
                )

        dimension_owners: dict[str, str] = {}
        for ds_name in included:
            for dim_name in bundle.datasets[ds_name].dimensions:
                dimension_owners[dim_name] = ds_name

        for dim_name, ds_name in dimension_owners.items():
            if dim_name in native_names:
                continue  # native shadows imported (FR-010)
            owner_tag = f"{imp.bundle}.{ds_name}"
            if dim_name in claimed and claimed[dim_name] != owner_tag:
                raise ModelError(
                    f"model '{model.name}': dimension '{dim_name}' is offered by both "
                    f"{claimed[dim_name]} and {owner_tag} — subset one of the imports"
                )
            claimed[dim_name] = owner_tag
            # engine._scan_bundle hands the bundle over with each dimension
            # already aliased to its dimension name, so from here the importing
            # model addresses it by that name and never by the bundle's raw
            # column — which is free to collide with one of the model's own.
            model.dimensions[dim_name] = replace(
                bundle.datasets[ds_name].dimensions[dim_name], column=dim_name,
            )

        model.import_bindings.append(ImportBinding(
            import_spec=imp, bundle=bundle,
            included_datasets=included, dimension_owners=dimension_owners,
        ))
    return model


# ---------------------------------------------------------------------------
# Parts: a model's datasets, split into the fact tables they actually form.
#
# A model's datasets don't have to be related to each other. Order lines
# joined to a product lookup are one fact table; monthly ad spend sitting
# alongside them, related to nothing, is a second. Each connected component of
# the relation graph becomes a ModelPart, and a part is what gets scanned.
#
# The rule that makes several parts in one model safe is that they are never
# joined together. Each is queried on its own and the per-part results are
# merged on the dimensions they share (engine._run_parts) — the "drill across a
# conformed dimension" shape. Joining them instead would give every row of one
# fact a copy of every matching row of the other and multiply both measures.
#
# The corollary is that parts can only be grouped by a dimension *every* one of
# them offers: the intersection, not the union. A dimension only one part knows
# about has no meaning for the others' rows, so there is no honest value to put
# on the row where they meet. Parts agree on a name because they declare or
# import one — importing the same common dimensional model into two of the
# model's fact tables is what conforms them, and it is the ordinary way to
# build this: two facts, each related to the shared calendar, neither related
# to the other.
#
# That intersection is taken over the parts a *query* actually reads, not over
# every part the model has (see shared_dimensions and engine._run_parts). A
# model holding sales, marketing and subscriptions still offers `channel` to a
# query that only measures the first two, because subscriptions contributes no
# rows to it. model.dimensions holds the all-parts intersection: the catalog
# that is safe whatever you ask for, which is what the builder opens on.
# ---------------------------------------------------------------------------

def _join_chain(
    datasets: dict[str, Dataset], members: list[str], root: str,
) -> tuple[list[str], list[Join]]:
    """Order one component's datasets into a join chain starting at `root`, and
    render each edge as the Join engine.scan() applies.

    Each join is applied with the already-accumulated side as the left
    operand and `how` taken from the edge as declared — so the root fact table
    (and anything already pulled in) is always preserved in full, gaining
    nullable columns for anything only reachable in the reverse of how the
    author happened to declare that particular edge. Same walk, and same
    reasoning, as engine._scan_bundle does from an import's anchor."""
    edge_by_pair = {(name, j.to): j
                    for name in members for j in datasets[name].joins if j.to in members}
    order = [root]
    joins: list[Join] = []
    remaining = [name for name in members if name != root]
    while remaining:
        progressed = False
        for name in list(remaining):
            edge, reversed_edge = None, False
            for joined in order:
                if (joined, name) in edge_by_pair:
                    edge = edge_by_pair[(joined, name)]
                    break
                if (name, joined) in edge_by_pair:
                    edge, reversed_edge = edge_by_pair[(name, joined)], True
                    break
            if edge is None:
                continue
            left_on, right_on = (edge.right_on, edge.left_on) if reversed_edge else (edge.left_on, edge.right_on)
            joins.append(Join(name=name, source=datasets[name].source,
                              left_on=left_on, right_on=right_on, how=edge.how))
            order.append(name)
            remaining.remove(name)
            progressed = True
        if not progressed:  # _components() only groups datasets that connect
            raise ModelError(
                f"model: internal error resolving join order for datasets {sorted(remaining)}")
    return order, joins


def _build_part(model: Model, members: list[str]) -> ModelPart:
    """Turn one connected component into the single-source Model the engine
    scans for it.

    The root — the table the joins fan out from — is the first member that
    declares a measure, since that is the fact table the rest look up against;
    a component of pure lookups (no measures anywhere) roots at its first
    declared dataset. Dimensions and measures are pooled across the whole
    component: they are evaluated against one joined scan, so a dimension
    declared on the order lines is free to read a column the product lookup
    brought in.
    """
    datasets = model.datasets
    root = next((name for name in members if datasets[name].measures), members[0])
    order, joins = _join_chain(datasets, members, root)

    part_model = Model(
        name=model.name if len(members) == len(datasets) else f"{model.name}.{root}",
        label=model.label, description=model.description,
        source=datasets[root].source, joins=joins,
    )
    owner_of: dict[str, str] = {}
    for name in order:
        for what, declared, target in (("dimension", datasets[name].dimensions, part_model.dimensions),
                                       ("measure", datasets[name].measures, part_model.measures)):
            for field_name, obj in declared.items():
                if field_name in owner_of:
                    raise ModelError(
                        f"model '{model.name}': {what} '{field_name}' is declared by both dataset "
                        f"'{owner_of[field_name]}' and dataset '{name}', which are related to each "
                        f"other — rename one (two datasets may only reuse a dimension name when "
                        f"they are separate fact tables, which is what conforms them)"
                    )
                owner_of[field_name] = name
                target[field_name] = obj
    part_model.imports = [imp for imp in model.imports if imp.from_dataset in set(members)]
    return ModelPart(name=root, datasets=order, model=part_model)


def shared_dimensions(parts: list[ModelPart]) -> dict[str, Dimension]:
    """The dimensions every one of `parts` offers, under the names they all
    use, in the first part's declaration order.

    Each is a fresh Dimension: `column` is never read (the engine delegates to
    each part, which knows its own), and a spine or a geo pair stays behind
    with the part that declares it. Types are checked in _check_shared_types at
    load time, so any part can supply the template."""
    conformed = set(parts[0].model.dimensions)
    for part in parts[1:]:
        conformed &= set(part.model.dimensions)
    out: dict[str, Dimension] = {}
    for name in parts[0].model.dimensions:
        if name not in conformed:
            continue
        template = parts[0].model.dimensions[name]
        out[name] = Dimension(
            name=name, column=name, type=template.type, label=template.label,
            description=template.description, synonyms=list(template.synonyms),
        )
    return out


def _check_shared_types(model: Model) -> None:
    """Every dimension name two or more parts offer has to mean the same kind
    of thing on both, or it can't be grouped across them. Checked over *pairs*
    rather than over the all-parts intersection, since a query reading a subset
    of the parts conforms on that subset's intersection."""
    types: dict[str, dict[str, str]] = {}   # dimension -> type -> part name
    for part in model.parts:
        for name, dim in part.model.dimensions.items():
            seen = types.setdefault(name, {})
            if dim.type not in seen and seen:
                other_type, other_part = next(iter(seen.items()))
                raise ModelError(
                    f"model '{model.name}': shared dimension '{name}' is {other_type} on "
                    f"'{other_part}' and {dim.type} on '{part.name}' — the fact tables have to "
                    f"agree on its type before it can be grouped across them"
                )
            seen.setdefault(dim.type, part.name)


def _check_unique_measures(model: Model) -> None:
    """Measure names are the model's public namespace, so they have to be
    unique across its fact tables — unlike dimension names, where a repeat is
    the conformance mechanism rather than a clash."""
    owner_of: dict[str, str] = {}
    for part in model.parts:
        for name in part.model.measures:
            if name in owner_of:
                raise ModelError(
                    f"model '{model.name}': measure '{name}' is declared on both fact table "
                    f"'{owner_of[name]}' and fact table '{part.name}' — rename one; a query names "
                    f"a measure without saying which table it came from"
                )
            owner_of[name] = part.name


def _assign_imports(model: Model, components: list[list[str]]) -> None:
    """Point every import at one of the model's datasets, and check it lands.

    With a single fact table there is only one thing it could relate to, so
    `from_dataset` may be omitted (which is what the terse `source:` spelling
    always does). With several, saying which one is the whole question — an
    import that doesn't say lands nowhere in particular, so it's an error."""
    for imp in model.imports:
        if not imp.from_dataset:
            if len(components) > 1:
                raise ModelError(
                    f"model '{model.name}': import of '{imp.bundle}' needs 'from_dataset' — this "
                    f"model has {len(components)} unrelated fact tables "
                    f"({', '.join(c[0] for c in components)}), so which one relates to the common "
                    f"model decides which measures the import's dimensions can be read alongside"
                )
            imp.from_dataset = components[0][0]
        if imp.from_dataset not in model.datasets:
            raise ModelError(
                f"model '{model.name}': import of '{imp.bundle}' relates from unknown dataset "
                f"'{imp.from_dataset}'"
            )


def _split_parts(model: Model) -> Model:
    """Split a model's datasets into parts and build the catalog its own file
    decides — everything short of the imports, which need the bundles.

    A single-part model *is* its part: parts[0].model is `model` itself, so its
    source, joins, dimensions and measures sit where they always have and every
    query path is unchanged. With several parts the model has no source of its
    own; it offers every part's measures, and only the dimensions all of them
    share. Called at the end of parsing, so a parsed-but-unresolved model reads
    exactly as it always did: its native catalog, imports not yet merged."""
    model.parts = [_build_part(model, members) for members in _components(model.datasets)]
    if len(model.parts) == 1:
        # the model is the part: adopt it wholesale rather than keeping a
        # near-copy of the same model alongside itself
        only = model.parts[0]
        model.source, model.joins = only.model.source, only.model.joins
        model.dimensions, model.measures = only.model.dimensions, only.model.measures
        model.parts = [ModelPart(name=only.name, datasets=only.datasets, model=model)]
        return model

    for part in model.parts:
        if not part.model.measures:
            raise ModelError(
                f"model '{model.name}': dataset '{part.name}' is related to nothing else in this "
                f"model and declares no measures, so it is a fact table with nothing to measure — "
                f"its dimensions would only narrow what the model's other fact tables can be "
                f"grouped by. Relate it to one of them, or give it a measure"
            )
    _check_shared_types(model)
    _check_unique_measures(model)
    model.source, model.joins = None, []
    model.dimensions = shared_dimensions(model.parts)
    model.measures = {}
    for part in model.parts:
        model.measures.update(part.model.measures)
    return model


def resolve_model(model: Model, bundles: dict[str, DimensionBundle]) -> Model:
    """Merge each part's imported dimensions in, against the bundles currently
    loaded — the half of resolution that needs to look outside the file.

    A model with several fact tables re-takes the intersection afterwards:
    importing the same common model into two of them is exactly what conforms
    them, so its dimensions have to be on board before "what can this be
    grouped by" has its final answer. Mutates and returns `model`; idempotent,
    so a hot-reload can re-resolve a model already in the registry."""
    if not model.is_composite:
        return _resolve_part_imports(model, bundles)
    for part in model.parts:
        _resolve_part_imports(part.model, bundles)
    _check_shared_types(model)
    model.import_bindings = []
    model.dimensions = shared_dimensions(model.parts)
    return model


def part_for_measure(model: Model, name: str) -> ModelPart:
    """The fact table a measure belongs to. Names are unique across a model's
    parts (_check_unique_measures), so this is unambiguous."""
    for part in model.parts:
        if name in part.model.measures:
            return part
    raise ModelError(f"unknown measure '{name}' in model '{model.name}'")


def fact_sources(model: Model) -> list[Source]:
    """The source behind each of the model's fact tables — what a pipeline
    matches its target path against (app/pipelines.py), and what "this model
    reads that object" means for a model with more than one."""
    return [part.model.source for part in model.parts if part.model.source]
