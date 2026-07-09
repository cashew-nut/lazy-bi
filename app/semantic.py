"""Semantic layer: YAML model definitions on top of S3 files.

A model maps a file source (parquet/csv/delta on S3) to named dimensions and
measures, optionally joining in other sources (lookup/dimension tables).
Measures are written in polars expression syntax and evaluated in a namespace
containing only `pl`. Models are trusted configuration, same as the
application code — do not load YAML from untrusted users.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import polars as pl
import yaml

_EVAL_GLOBALS = {"__builtins__": {}, "pl": pl}

TIME_GRAINS = {"1d": "Day", "1w": "Week", "1mo": "Month", "1q": "Quarter", "1y": "Year"}
SOURCE_FORMATS = ("parquet", "csv", "delta")
JOIN_KINDS = ("left", "inner")


class ModelError(Exception):
    pass


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


@dataclass
class Measure:
    name: str
    label: str
    expr_source: str
    format: str = "number"  # number | currency | percent
    description: str = ""

    def expr(self) -> pl.Expr:
        try:
            expr = eval(self.expr_source, _EVAL_GLOBALS)  # noqa: S307 - trusted model config
        except Exception as exc:
            raise ModelError(f"measure '{self.name}': cannot evaluate expression: {exc}") from exc
        if not isinstance(expr, pl.Expr):
            raise ModelError(f"measure '{self.name}': expression is not a polars Expr")
        return expr.alias(self.name)


@dataclass
class Model:
    name: str
    label: str
    description: str
    source: Source
    joins: list[Join] = field(default_factory=list)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
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
            "dimensions": [
                {"name": d.name, "label": d.label, "type": d.type,
                 "description": d.description, "spine": bool(d.spine), "geo": bool(d.geo)}
                for d in self.dimensions.values()
            ],
            "measures": [
                {"name": m.name, "label": m.label, "format": m.format,
                 "description": m.description, "expr": m.expr_source}
                for m in self.measures.values()
            ],
        }


def _parse_source(raw: dict, origin: Path) -> Source:
    source = Source(path=raw["path"], format=raw.get("format", "parquet"))
    if source.format not in SOURCE_FORMATS:
        raise ModelError(f"{origin.name}: unsupported source format '{source.format}'")
    return source


def _as_list(v) -> list[str]:
    return v if isinstance(v, list) else [v]


def _parse_model(raw: dict, origin: Path) -> Model:
    try:
        model = Model(
            name=raw["name"],
            label=raw.get("label", raw["name"]),
            description=raw.get("description", ""),
            source=_parse_source(raw["source"], origin),
        )
        for j in raw.get("joins", []):
            # YAML 1.1 parses a bare `on:` key as boolean True — accept both
            on = j.get("on", j.get(True))
            join = Join(
                name=j.get("name", "join"),
                source=_parse_source(j["source"], origin),
                left_on=_as_list(j["left_on"] if "left_on" in j else on),
                right_on=_as_list(j["right_on"] if "right_on" in j else on),
                how=j.get("how", "left"),
            )
            if not join.left_on or join.left_on == [None]:
                raise ModelError(f"{origin.name}: join '{join.name}' needs 'on' or 'left_on'/'right_on'")
            if join.how not in JOIN_KINDS:
                raise ModelError(f"{origin.name}: join '{join.name}': unsupported how '{join.how}'")
            model.joins.append(join)
        for d in raw.get("dimensions", []):
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
            )
            if dim.spine and dim.type != "time":
                raise ModelError(f"{origin.name}: spine dimension '{dim.name}' must have type: time")
            model.dimensions[dim.name] = dim
        for m in raw.get("measures", []):
            meas = Measure(
                name=m["name"],
                label=m.get("label", m["name"].replace("_", " ").title()),
                expr_source=m["expr"],
                format=m.get("format", "number"),
                description=m.get("description", ""),
            )
            meas.expr()  # validate at load time
            model.measures[meas.name] = meas
    except KeyError as exc:
        raise ModelError(f"{origin.name}: missing required key {exc}") from exc
    return model


def parse_model_text(text: str) -> Model:
    """Parse and validate a model from editor-supplied YAML text."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ModelError(f"invalid yaml: {exc}")
    if not isinstance(raw, dict):
        raise ModelError("yaml must be a mapping with name / source / dimensions / measures")
    return _parse_model(raw, Path("<editor>"))


def load_models(models_dir: Path) -> dict[str, Model]:
    models: dict[str, Model] = {}
    for path in sorted(models_dir.glob("*.y*ml")):
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        model = _parse_model(raw, path)
        model.origin = path
        models[model.name] = model
    return models
