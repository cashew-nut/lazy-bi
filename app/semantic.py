"""Semantic layer: YAML model definitions on top of S3 files.

A model maps a file source (parquet/csv/delta on S3) to named dimensions and
measures, optionally joining in other sources (lookup/dimension tables).
Measures are written in polars expression syntax and evaluated in a namespace
containing only `pl`. Models are trusted configuration, same as the
application code — do not load YAML from untrusted users.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import polars as pl
import yaml

from . import measure_dsl

_EVAL_GLOBALS = {"__builtins__": {}, "pl": pl}

# frame snippets are multi-statement python where basics like list()/dict()/
# len() are legitimately useful; expose a small utility subset (the empty
# __builtins__ above is hygiene, not a sandbox — models are trusted
# configuration either way)
_FRAME_BUILTINS = {
    f.__name__: f for f in (
        abs, dict, enumerate, float, int, len, list, max, min,
        range, round, set, sorted, str, sum, tuple, zip,
    )
}

TIME_GRAINS = {"1d": "Day", "1w": "Week", "1mo": "Month", "1q": "Quarter", "1y": "Year"}
SOURCE_FORMATS = ("parquet", "csv", "delta")
JOIN_KINDS = ("left", "inner")


class ModelError(Exception):
    pass


def compile_expr(source: str, owner: str = "expression") -> pl.Expr:
    """Evaluate polars expression syntax (trusted config / single-user input)."""
    try:
        expr = eval(source, _EVAL_GLOBALS)  # noqa: S307 - see module docstring
    except Exception as exc:
        raise ModelError(f"{owner}: cannot evaluate expression: {exc}") from exc
    if not isinstance(expr, pl.Expr):
        raise ModelError(f"{owner}: expression is not a polars Expr")
    return expr


def validate_frame(source: str, owner: str) -> None:
    """Load-time syntax check for a measure's intermediary-frame snippet — it
    cannot be fully evaluated until query time, when a live scan exists."""
    try:
        compile(source, f"<{owner}>", "exec")
    except SyntaxError as exc:
        raise ModelError(f"{owner}: invalid frame syntax: {exc}") from exc


def compile_frame(source: str, lf: pl.LazyFrame, dims: list[str], owner: str) -> pl.LazyFrame:
    """Evaluate a measure's intermediary-frame snippet (trusted config, like
    compile_expr). The snippet sees `lf` (the filtered scan, with the query's
    dimension columns already materialized under their semantic names),
    `dims` (the list of those column names) and `pl`. It is either a single
    expression, or statements that assign the result to a variable named
    `frame`; either way it must produce a LazyFrame that still carries the
    `dims` columns so the engine can aggregate it at the query's grain."""
    ns: dict = {"__builtins__": _FRAME_BUILTINS, "pl": pl, "lf": lf, "dims": list(dims)}
    try:
        try:
            code = compile(source, f"<{owner}>", "eval")
        except SyntaxError:
            code = None
        if code is not None:
            result = eval(code, ns)  # noqa: S307 - see module docstring
        else:
            exec(compile(source, f"<{owner}>", "exec"), ns)  # noqa: S102
            if "frame" not in ns:
                raise ModelError(
                    f"{owner}: frame snippet must assign its result to a variable named 'frame'"
                )
            result = ns["frame"]
    except ModelError:
        raise
    except Exception as exc:
        raise ModelError(f"{owner}: cannot evaluate frame: {exc}") from exc
    if isinstance(result, pl.DataFrame):
        result = result.lazy()
    if not isinstance(result, pl.LazyFrame):
        raise ModelError(f"{owner}: frame did not produce a polars LazyFrame")
    return result


@dataclass
class Source:
    path: str            # s3://bucket/prefix/*.parquet | .../table (delta root)
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
    """A join from one Dataset to a sibling Dataset in the same
    DimensionBundle (as opposed to Join, which targets a raw Source)."""
    to: str
    left_on: list[str]
    right_on: list[str]
    how: str = "left"


@dataclass
class Spine:
    """Marks a time dimension as a generated timeline: a row is counted in every
    time bucket between its start and end columns (point-in-time semantics —
    'active as of the bucket start'). Null end = still active."""
    start: str
    end: str


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
    # optional intermediary step: a python snippet building a derived LazyFrame
    # (business logic) that expr_source then aggregates over — see compile_frame
    frame_source: Optional[str] = None
    # dimensions the frame computes itself (columns of the derived frame, e.g.
    # a per-entity milestone date): excluded from `dims` during the step, and
    # grouped — time grains included — on the frame's output afterwards
    frame_emits: list[str] = field(default_factory=list)
    # see Dimension.synonyms — same advisory-only contract, never a second
    # valid identifier for Model.measure()
    synonyms: list[str] = field(default_factory=list)

    def expr(self, schema: Optional["pl.Schema"] = None) -> pl.Expr:
        # framed measures keep the pre-existing eval-based path (authenticated-
        # only — see app/api/models.py); every other measure compiles through
        # the safe DSL, for both model and inline measures alike.
        if self.frame_source is not None:
            return compile_expr(self.expr_source, f"measure '{self.name}'").alias(self.name)
        try:
            return measure_dsl.compile_measure(self.expr_source, schema, alias=self.name)
        except measure_dsl.MeasureCompileError as exc:
            raise ModelError(f"measure '{self.name}': {exc}") from exc


@dataclass
class Dataset:
    """A single source + the dimensions it exposes, living inside a
    DimensionBundle - the bundle-scoped equivalent of a Model, minus
    measures (common dimensional models declare no measures)."""
    name: str
    source: Source
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    joins: list[DatasetJoin] = field(default_factory=list)


@dataclass
class DimensionBundle:
    """A named, reusable set of Datasets (plus the joins between them),
    independent of any single fact Model. See Model.imports."""
    name: str
    label: str
    description: str
    datasets: dict[str, Dataset] = field(default_factory=dict)
    origin: Optional[Path] = None

    def dataset(self, name: str) -> Dataset:
        try:
            return self.datasets[name]
        except KeyError:
            raise ModelError(f"unknown dataset '{name}' in dimension bundle '{self.name}'")


@dataclass
class Import:
    """A Model's reference to a DimensionBundle: an anchor (how the model's
    own source connects to one dataset in the bundle) plus an optional
    subset of the bundle's datasets to include (default: all of them)."""
    bundle: str
    anchor_dataset: str
    left_on: list[str]
    right_on: list[str]
    how: str = "left"
    datasets: Optional[list[str]] = None  # None = whole bundle


@dataclass
class ImportBinding:
    """Resolved, engine-facing form of an Import - computed once at
    load/hot-reload time by resolve_imports(), not part of the YAML shape."""
    import_spec: Import
    bundle: DimensionBundle
    included_datasets: list[str]           # BFS-reachable from anchor_dataset, subset-filtered
    dimension_owners: dict[str, str]        # imported dimension name -> owning dataset name


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
    source: Source
    joins: list[Join] = field(default_factory=list)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
    imports: list[Import] = field(default_factory=list)
    import_bindings: list[ImportBinding] = field(default_factory=list)  # populated by resolve_imports
    pipeline_lineage: Optional[LineageSection] = None  # tolerantly parsed; see _parse_lineage_section
    origin: Optional[Path] = None  # yaml file the model was loaded from

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
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "path": self.source.path,
            "format": self.source.format,
            "file": self.origin.name if self.origin else None,
            "joins": [{"name": j.name, "path": j.source.path, "format": j.source.format} for j in self.joins],
            "imports": [
                {"bundle": b.import_spec.bundle, "anchor_dataset": b.import_spec.anchor_dataset,
                 "datasets": b.import_spec.datasets}
                for b in self.import_bindings
            ],
            "dimensions": [
                {"name": d.name, "label": d.label, "type": d.type,
                 "description": d.description, "spine": bool(d.spine), "geo": bool(d.geo),
                 "synonyms": d.synonyms}
                for d in self.dimensions.values()
            ],
            "measures": [
                {"name": m.name, "label": m.label, "format": m.format,
                 "description": m.description, "expr": m.expr_source,
                 "frame": m.frame_source, "frame_emits": m.frame_emits,
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


def _parse_join_keys(j: dict, owner: str, join_desc: str) -> tuple[list[str], list[str], str]:
    """Shared on/left_on/right_on/how resolution for both Join (model -> raw
    source) and DatasetJoin (dataset -> sibling dataset in a bundle). YAML 1.1
    parses a bare `on:` key as boolean True — accept both."""
    on = j.get("on", j.get(True))
    left_on = _as_list(j["left_on"] if "left_on" in j else on)
    right_on = _as_list(j["right_on"] if "right_on" in j else on)
    how = j.get("how", "left")
    if not left_on or left_on == [None]:
        raise ModelError(f"{owner}: {join_desc} needs 'on' or 'left_on'/'right_on'")
    if how not in JOIN_KINDS:
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
            spine=Spine(start=spine_raw["start"], end=spine_raw["end"]) if spine_raw else None,
            geo=Geo(lat=geo_raw["lat"], lon=geo_raw["lon"]) if geo_raw else None,
            synonyms=_as_list(d["synonyms"]) if d.get("synonyms") else [],
        )
        if dim.spine and dim.type != "time":
            raise ModelError(f"{owner}: spine dimension '{dim.name}' must have type: time")
        dims[dim.name] = dim
    return dims


def _parse_import(raw: dict, owner: str) -> Import:
    anchor = raw.get("anchor_dataset")
    if not anchor:
        raise ModelError(f"{owner}: dimension_imports entry needs 'anchor_dataset'")
    left_on, right_on, how = _parse_join_keys(raw, owner, f"import of '{raw.get('bundle')}'")
    datasets = raw.get("datasets")
    if datasets is not None and not isinstance(datasets, list):
        raise ModelError(f"{owner}: import of '{raw.get('bundle')}': 'datasets' must be a list")
    return Import(
        bundle=raw["bundle"], anchor_dataset=anchor,
        left_on=left_on, right_on=right_on, how=how, datasets=datasets,
    )


def _parse_model(raw: dict, origin: Path) -> Model:
    try:
        model = Model(
            name=raw["name"],
            label=raw.get("label", raw["name"]),
            description=raw.get("description", ""),
            source=_parse_source(raw["source"], origin),
        )
        for j in raw.get("joins", []):
            left_on, right_on, how = _parse_join_keys(j, origin.name, f"join '{j.get('name', 'join')}'")
            model.joins.append(Join(
                name=j.get("name", "join"), source=_parse_source(j["source"], origin),
                left_on=left_on, right_on=right_on, how=how,
            ))
        model.dimensions = _parse_dimensions(raw.get("dimensions", []), origin.name)
        for m in raw.get("measures", []):
            meas = Measure(
                name=m["name"],
                label=m.get("label", m["name"].replace("_", " ").title()),
                expr_source=m["expr"],
                format=m.get("format", "number"),
                description=m.get("description", ""),
                frame_source=m.get("frame"),
                frame_emits=_as_list(m["frame_emits"]) if m.get("frame_emits") else [],
                synonyms=_as_list(m["synonyms"]) if m.get("synonyms") else [],
            )
            if meas.frame_source:
                validate_frame(meas.frame_source, f"measure '{meas.name}'")
            elif meas.frame_emits:
                raise ModelError(
                    f"{origin.name}: measure '{meas.name}': 'frame_emits' needs a 'frame'"
                )
            meas.expr()  # validate at load time
            model.measures[meas.name] = meas
        for imp in raw.get("dimension_imports", []):
            model.imports.append(_parse_import(imp, origin.name))
    except KeyError as exc:
        raise ModelError(f"{origin.name}: missing required key {exc}") from exc
    model.pipeline_lineage = _parse_lineage_section(raw.get("pipeline_lineage"))
    return model


class _BlockStrDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings (frame snippets) as literal
    `|` blocks instead of quoted strings full of \\n escapes."""


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
    return _parse_editor_text(text, "name / source / dimensions / measures", _parse_model)


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

def _parse_dataset_join(j: dict, origin: Path, owner: str) -> DatasetJoin:
    to = j.get("to")
    if not to:
        raise ModelError(f"{origin.name}: {owner}: dataset join needs 'to'")
    left_on, right_on, how = _parse_join_keys(j, origin.name, f"{owner}: join to '{to}'")
    return DatasetJoin(to=to, left_on=left_on, right_on=right_on, how=how)


def _parse_dataset(raw: dict, origin: Path) -> Dataset:
    try:
        dataset = Dataset(name=raw["name"], source=_parse_source(raw["source"], origin))
    except KeyError as exc:
        raise ModelError(f"{origin.name}: dataset missing required key {exc}") from exc
    owner = f"dataset '{dataset.name}'"
    dataset.dimensions = _parse_dimensions(raw.get("dimensions", []), f"{origin.name}: {owner}")
    for j in raw.get("joins", []):
        dataset.joins.append(_parse_dataset_join(j, origin, owner))
    return dataset


def _bundle_edges(bundle: DimensionBundle) -> dict[str, set[str]]:
    """Undirected adjacency: a DatasetJoin declared on either side makes both
    datasets reachable from each other once the bundle is walked from an
    arbitrary anchor."""
    edges: dict[str, set[str]] = {name: set() for name in bundle.datasets}
    for ds in bundle.datasets.values():
        for j in ds.joins:
            edges[ds.name].add(j.to)
            edges[j.to].add(ds.name)
    return edges


def _check_acyclic(bundle: DimensionBundle) -> None:
    edges = _bundle_edges(bundle)
    visited: set[str] = set()

    def dfs(node: str, parent: Optional[str]) -> None:
        visited.add(node)
        for neighbor in edges[node]:
            if neighbor == parent:
                continue
            if neighbor in visited:
                raise ModelError(
                    f"dimension bundle '{bundle.name}': cyclical join between "
                    f"datasets '{node}' and '{neighbor}'"
                )
            dfs(neighbor, node)

    for start in bundle.datasets:
        if start not in visited:
            dfs(start, None)


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

    for ds in bundle.datasets.values():
        for j in ds.joins:
            if j.to not in bundle.datasets:
                raise ModelError(
                    f"{origin.name}: bundle '{bundle.name}': dataset '{ds.name}' joins "
                    f"to unknown dataset '{j.to}'"
                )
    _check_acyclic(bundle)
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
        "spine": {"start": d.spine.start, "end": d.spine.end} if d.spine else None,
        "geo": {"lat": d.geo.lat, "lon": d.geo.lon} if d.geo else None,
        "synonyms": list(d.synonyms),
    }


def model_to_spec(model: Model) -> dict:
    """Form-facing dict for a parsed-but-unresolved Model (native dimensions
    only — imported dimensions live in the bundle, not this file)."""
    return {
        "name": model.name,
        "label": model.label,
        "description": model.description,
        "source": {"path": model.source.path, "format": model.source.format},
        "joins": [
            {"name": j.name, "path": j.source.path, "format": j.source.format,
             "left_on": j.left_on, "right_on": j.right_on, "how": j.how}
            for j in model.joins
        ],
        "dimension_imports": [
            {"bundle": i.bundle, "anchor_dataset": i.anchor_dataset,
             "left_on": i.left_on, "right_on": i.right_on, "how": i.how,
             "datasets": i.datasets}
            for i in model.imports
        ],
        "dimensions": [_dimension_to_spec(d) for d in model.dimensions.values()],
        "measures": [
            {"name": m.name, "label": m.label, "expr": m.expr_source,
             "format": m.format, "description": m.description,
             "frame": m.frame_source, "frame_emits": m.frame_emits,
             "synonyms": list(m.synonyms)}
            for m in model.measures.values()
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
            {"name": ds.name,
             "source": {"path": ds.source.path, "format": ds.source.format},
             "dimensions": [_dimension_to_spec(d) for d in ds.dimensions.values()],
             "joins": [{"to": j.to, "left_on": j.left_on, "right_on": j.right_on, "how": j.how}
                       for j in ds.joins]}
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
        if d.get("spine"):
            entry["spine"] = {"start": d["spine"]["start"], "end": d["spine"]["end"]}
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


def spec_to_yaml(spec: dict) -> str:
    """Render a form spec dict to canonical model YAML (defaults omitted)."""
    doc = _spec_header(spec)
    src = spec["source"]
    doc["source"] = {"format": src.get("format", "parquet"), "path": src["path"]}

    joins = []
    for j in spec.get("joins") or []:
        entry = {"name": j["name"],
                 "source": {"format": j.get("format", "parquet"), "path": j["path"]}}
        _spec_join_keys(entry, j)
        joins.append(entry)
    if joins:
        doc["joins"] = joins

    imports = []
    for i in spec.get("dimension_imports") or []:
        entry = {"bundle": i["bundle"], "anchor_dataset": i["anchor_dataset"]}
        _spec_join_keys(entry, i)
        if i.get("datasets") is not None:
            entry["datasets"] = list(i["datasets"])
        imports.append(entry)
    if imports:
        doc["dimension_imports"] = imports

    doc["dimensions"] = _spec_dimension_entries(spec.get("dimensions") or [])

    measures = []
    for m in spec.get("measures") or []:
        entry = {"name": m["name"]}
        if m.get("label"):
            entry["label"] = m["label"]
        if m.get("format", "number") != "number":
            entry["format"] = m["format"]
        if m.get("description"):
            entry["description"] = m["description"]
        if m.get("frame"):
            entry["frame"] = m["frame"]
        if m.get("frame_emits"):
            entry["frame_emits"] = list(m["frame_emits"])
        if m.get("synonyms"):
            entry["synonyms"] = list(m["synonyms"])
        entry["expr"] = m["expr"]
        measures.append(entry)
    doc["measures"] = measures

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
    entries = []
    for ds in spec.get("datasets") or []:
        entry: dict = {
            "name": ds["name"],
            "source": {"format": ds["source"].get("format", "parquet"), "path": ds["source"]["path"]},
            "dimensions": _spec_dimension_entries(ds.get("dimensions") or []),
        }
        joins = []
        for j in ds.get("joins") or []:
            join_entry = {"to": j["to"]}
            _spec_join_keys(join_entry, j)
            joins.append(join_entry)
        if joins:
            entry["joins"] = joins
        entries.append(entry)
    doc["datasets"] = entries
    return _dump_generated(doc)


# ---------------------------------------------------------------------------
# Dataset discovery: group raw bucket objects into pickable "datasets" for the
# modelling workspace's source picker. Pure helpers (no S3 access) so they are
# unit-testable; app/api/datasets.py layers the bucket walk + model-mapping on top.
# ---------------------------------------------------------------------------

_EXT_FORMAT = {".parquet": "parquet", ".csv": "csv"}
_DELTA_MARKER = "/_delta_log/"


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
        sources = (
            [("source", m.source)]
            + [(f"join: {j.name}", j.source) for j in m.joins]
            + [(f"import: {b.bundle.name}.{ds}", b.bundle.datasets[ds].source)
               for b in m.import_bindings for ds in b.included_datasets]
        )
        for role, src in sources:
            if not src.path.startswith(prefix):
                continue
            rel = src.path[len(prefix):]
            if src.format == "delta":
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


def group_objects(objects: list[dict], bucket: str) -> list[dict]:
    """Group bucket objects (each ``{"key", "size"}``) into pickable datasets.

    A Delta table (any object under a ``_delta_log/`` marker) collapses into a
    single ``delta`` dataset rooted at the table directory; every other object
    groups by its directory prefix into a format-inferred glob source. Prefixes
    whose objects carry no recognized data extension are dropped (they cannot
    back a valid source). Pure — no S3 access; ``bucket`` only builds paths."""
    delta_roots: list[str] = []
    for obj in objects:
        if _DELTA_MARKER in obj["key"]:
            root = obj["key"].split(_DELTA_MARKER, 1)[0]
            if root not in delta_roots:
                delta_roots.append(root)

    def delta_root_of(key: str) -> Optional[str]:
        for root in delta_roots:
            if key == root or key.startswith(root + "/"):
                return root
        return None

    # bucket every object by its delta root (or None) in a single pass, rather
    # than rescanning all objects against delta_root_of once per root below
    delta_members: dict[str, list[dict]] = {root: [] for root in delta_roots}
    non_delta: list[dict] = []
    for obj in objects:
        root = delta_root_of(obj["key"])
        (delta_members[root] if root is not None else non_delta).append(obj)

    datasets: list[dict] = []

    for root in delta_roots:
        members = delta_members[root]
        datasets.append({
            "key": root,
            "path": f"s3://{bucket}/{root}",
            "format": "delta",
            "format_ambiguous": False,
            "object_count": len(members),
            "bytes": sum(o.get("size", 0) for o in members),
            "objects": [{"key": o["key"], "size": o.get("size", 0), "format": "delta"} for o in members],
        })

    groups: dict[str, list[dict]] = {}
    for obj in non_delta:
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
    edges = _bundle_edges(bundle)
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


def resolve_imports(model: Model, bundles: dict[str, DimensionBundle]) -> Model:
    """Merge each of a model's declared imports into model.dimensions and
    attach the ImportBinding metadata engine.scan() needs to build the join
    chain. A native dimension always shadows a same-named imported one; a
    same-named dimension offered by two different imports is a load-time
    error (subset one of the imports to resolve it). Mutates and returns
    `model`; safe to call once per freshly-parsed model."""
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
            model.dimensions[dim_name] = bundle.datasets[ds_name].dimensions[dim_name]

        model.import_bindings.append(ImportBinding(
            import_spec=imp, bundle=bundle,
            included_datasets=included, dimension_owners=dimension_owners,
        ))
    return model
