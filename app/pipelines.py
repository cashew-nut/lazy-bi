"""Pipeline definitions: hosted polars transformation scripts (specs/014-
polars-pipeline-module/). A pipeline is one YAML file in `pipelines/`,
loaded/hot-reloaded the same way a semantic model is — see `app/semantic.py`
for the sibling pattern this mirrors. A pipeline's script is real Python,
application-code trust level (Principle VI): this module only parses and
validates the *shape* of a pipeline (syntax-checks the script; never
executes it). Execution lives in `app/pipeline_runner.py`.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

SOURCE_FORMATS = ("parquet", "csv", "delta", "iceberg")
TARGET_FORMATS = ("delta", "parquet")  # iceberg is read-only — no write path yet
MATERIALIZATION_MODES = ("replace", "upsert")
DELETE_POLICIES = ("ignore", "sync", "soft_delete", "predicate")

# layers.yaml (or .yml) is metadata about pipelines, not a pipeline itself —
# load_pipelines skips it when globbing the directory (research U3).
RESERVED_FILENAMES = {"layers.yaml", "layers.yml"}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class PipelineError(Exception):
    pass


@dataclass
class PipelineSource:
    name: str
    format: str
    path: str
    layer: Optional[str] = None


@dataclass
class Target:
    path: str
    format: str = "delta"
    layer: Optional[str] = None


@dataclass
class Materialization:
    mode: str
    keys: list[str] = field(default_factory=list)
    on_delete: str = "ignore"
    soft_delete_column: Optional[str] = None
    delete_predicate: Optional[str] = None
    allow_empty_sync: bool = False


@dataclass
class LineageEntry:
    field: str
    sources: list[str] = field(default_factory=list)  # "source_name.column" refs
    transform: str = ""


@dataclass
class Pipeline:
    name: str
    label: str
    description: str
    sources: dict[str, PipelineSource] = field(default_factory=dict)  # insertion-ordered
    target: Optional[Target] = None
    materialization: Optional[Materialization] = None
    timeout_seconds: int = 600
    script: str = ""
    lineage: list[LineageEntry] = field(default_factory=list)
    origin: Optional[Path] = None  # yaml file the pipeline was loaded from

    def to_public(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "file": self.origin.name if self.origin else None,
            "sources": [
                {"name": s.name, "format": s.format, "path": s.path, "layer": s.layer}
                for s in self.sources.values()
            ],
            "target": {"path": self.target.path, "format": self.target.format,
                       "layer": self.target.layer} if self.target else None,
            "materialization": {
                "mode": self.materialization.mode,
                "keys": list(self.materialization.keys),
                "on_delete": self.materialization.on_delete,
                "soft_delete_column": self.materialization.soft_delete_column,
                "delete_predicate": self.materialization.delete_predicate,
                "allow_empty_sync": self.materialization.allow_empty_sync,
            } if self.materialization else None,
            "timeout_seconds": self.timeout_seconds,
            "lineage": [
                {"field": entry.field, "from": list(entry.sources), "transform": entry.transform}
                for entry in self.lineage
            ],
        }


@dataclass
class Layer:
    name: str
    label: str


def validate_script(source: str, owner: str) -> None:
    """Load-time syntax check only — a pipeline script cannot be meaningfully
    evaluated until a run has real source LazyFrames to bind (see
    app/pipeline_runner.py); mirrors semantic.validate_frame."""
    try:
        compile(source, f"<{owner}>", "exec")
    except SyntaxError as exc:
        raise PipelineError(f"{owner}: invalid script syntax: {exc}") from exc


def _check_name(name: str, kind: str, owner: str) -> None:
    if not name or not _NAME_RE.match(name):
        raise PipelineError(
            f"{owner}: {kind} '{name}' must match ^[a-z][a-z0-9_]*$"
        )


def _parse_pipeline_source(raw: dict, owner: str) -> PipelineSource:
    try:
        name = raw["name"]
        path = raw["path"]
    except KeyError as exc:
        raise PipelineError(f"{owner}: source missing required key {exc}") from exc
    _check_name(name, "source name", owner)
    fmt = raw.get("format", "parquet")
    if fmt not in SOURCE_FORMATS:
        raise PipelineError(f"{owner}: source '{name}': unsupported format '{fmt}'")
    return PipelineSource(name=name, format=fmt, path=path, layer=raw.get("layer"))


def _parse_target(raw: dict, owner: str) -> Target:
    try:
        path = raw["path"]
    except KeyError as exc:
        raise PipelineError(f"{owner}: target missing required key {exc}") from exc
    fmt = raw.get("format", "delta")
    if fmt not in TARGET_FORMATS:
        raise PipelineError(f"{owner}: target: unsupported format '{fmt}' (delta | parquet)")
    return Target(path=path, format=fmt, layer=raw.get("layer"))


def _parse_materialization(raw: dict, owner: str, target: Target) -> Materialization:
    mode = raw.get("mode")
    if mode not in MATERIALIZATION_MODES:
        raise PipelineError(f"{owner}: materialization.mode must be 'replace' or 'upsert'")
    mat = Materialization(mode=mode)
    if mode == "upsert":
        if target.format != "delta":
            raise PipelineError(f"{owner}: upsert mode requires target.format 'delta'")
        keys = raw.get("keys")
        keys = keys if isinstance(keys, list) else ([keys] if keys else [])
        if not keys:
            raise PipelineError(f"{owner}: upsert mode requires 'keys'")
        mat.keys = keys
        on_delete = raw.get("on_delete", "ignore")
        if on_delete not in DELETE_POLICIES:
            raise PipelineError(f"{owner}: unsupported on_delete '{on_delete}'")
        mat.on_delete = on_delete
        if on_delete == "soft_delete":
            col = raw.get("soft_delete_column")
            if not col:
                raise PipelineError(
                    f"{owner}: on_delete 'soft_delete' requires 'soft_delete_column'"
                )
            mat.soft_delete_column = col
        if on_delete == "predicate":
            pred = raw.get("delete_predicate")
            if not pred:
                raise PipelineError(
                    f"{owner}: on_delete 'predicate' requires 'delete_predicate'"
                )
            mat.delete_predicate = pred
        mat.allow_empty_sync = bool(raw.get("allow_empty_sync", False))
    else:  # replace: parquet or delta both fine; delete/keys fields are meaningless, ignored
        pass
    return mat


def _parse_lineage(raw_list: list, source_names: set[str], owner: str) -> list[LineageEntry]:
    entries: list[LineageEntry] = []
    seen: set[str] = set()
    for raw in raw_list:
        try:
            field_name = raw["field"]
        except KeyError as exc:
            raise PipelineError(f"{owner}: lineage entry missing required key {exc}") from exc
        if field_name in seen:
            raise PipelineError(f"{owner}: lineage: duplicate entry for field '{field_name}'")
        seen.add(field_name)
        from_refs = raw.get("from", [])
        from_refs = from_refs if isinstance(from_refs, list) else [from_refs]
        for ref in from_refs:
            src_name = ref.split(".", 1)[0]
            if src_name not in source_names:
                raise PipelineError(
                    f"{owner}: lineage field '{field_name}': unknown source '{src_name}' in '{ref}'"
                )
        entries.append(LineageEntry(field=field_name, sources=from_refs, transform=raw.get("transform", "")))
    return entries


def _parse_pipeline(raw: dict, origin: Path) -> Pipeline:
    try:
        name = raw["name"]
    except KeyError as exc:
        raise PipelineError(f"{origin.name}: missing required key {exc}") from exc
    _check_name(name, "pipeline name", origin.name)
    owner = f"pipeline '{name}'"
    pipeline = Pipeline(
        name=name,
        label=raw.get("label", name.replace("_", " ").title()),
        description=raw.get("description", ""),
    )

    raw_sources = raw.get("sources") or []
    if not raw_sources:
        raise PipelineError(f"{owner}: needs at least one source")
    for s in raw_sources:
        src = _parse_pipeline_source(s, owner)
        if src.name in pipeline.sources:
            raise PipelineError(f"{owner}: duplicate source name '{src.name}'")
        pipeline.sources[src.name] = src

    try:
        target_raw = raw["target"]
    except KeyError as exc:
        raise PipelineError(f"{owner}: missing required key {exc}") from exc
    pipeline.target = _parse_target(target_raw, owner)

    try:
        mat_raw = raw["materialization"]
    except KeyError as exc:
        raise PipelineError(f"{owner}: missing required key {exc}") from exc
    pipeline.materialization = _parse_materialization(mat_raw, owner, pipeline.target)

    timeout = raw.get("timeout_seconds", 600)
    if not isinstance(timeout, int) or not (1 <= timeout <= 3600):
        raise PipelineError(f"{owner}: timeout_seconds must be an integer in [1, 3600]")
    pipeline.timeout_seconds = timeout

    try:
        script = raw["script"]
    except KeyError as exc:
        raise PipelineError(f"{owner}: missing required key {exc}") from exc
    if not isinstance(script, str) or not script.strip():
        raise PipelineError(f"{owner}: 'script' must be a non-empty string")
    validate_script(script, owner)
    pipeline.script = script

    pipeline.lineage = _parse_lineage(raw.get("lineage", []) or [], set(pipeline.sources), owner)

    return pipeline


def parse_pipeline_text(text: str) -> Pipeline:
    """Parse and validate a pipeline from editor-supplied YAML text (no
    directory context — used by /api/pipelines/validate and as the first
    step of create/update)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PipelineError(f"invalid yaml: {exc}")
    if not isinstance(raw, dict):
        raise PipelineError("yaml must be a mapping with name / sources / target / materialization / script")
    return _parse_pipeline(raw, Path("<editor>"))


def _validate_layer_refs(pipelines: dict[str, "Pipeline"], layers: dict[str, Layer]) -> None:
    for p in pipelines.values():
        refs = [s.layer for s in p.sources.values() if s.layer]
        if p.target and p.target.layer:
            refs.append(p.target.layer)
        for layer_name in refs:
            if layer_name not in layers:
                raise PipelineError(
                    f"pipeline '{p.name}': references unknown layer '{layer_name}' "
                    f"— declare it in pipelines/layers.yaml"
                )


def _validate_unique_targets(pipelines: dict[str, "Pipeline"]) -> None:
    owner_of: dict[str, str] = {}
    for p in pipelines.values():
        path = p.target.path
        if path in owner_of and owner_of[path] != p.name:
            raise PipelineError(
                f"pipeline '{p.name}': target '{path}' is already owned by pipeline '{owner_of[path]}' "
                f"— a target may have at most one owning pipeline"
            )
        owner_of[path] = p.name


def load_pipelines(directory: Path, layers: dict[str, Layer]) -> dict[str, Pipeline]:
    """Load every pipeline in `directory` (skipping layers.yaml), then
    cross-validate layer references and target-path uniqueness across the
    whole loaded set — mirrors semantic.load_models's directory-loader shape,
    plus the two set-wide checks unique to pipelines."""
    pipelines: dict[str, Pipeline] = {}
    if not directory.is_dir():
        return pipelines
    for path in sorted(directory.glob("*.y*ml")):
        if path.name in RESERVED_FILENAMES:
            continue
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        if raw is None:  # empty file — skip quietly, same tolerance as layers.yaml
            continue
        pipeline = _parse_pipeline(raw, path)
        if pipeline.name in pipelines:
            raise PipelineError(
                f"{path.name}: duplicate pipeline name '{pipeline.name}' "
                f"(already defined in {pipelines[pipeline.name].origin.name})"
            )
        pipeline.origin = path
        pipelines[pipeline.name] = pipeline

    _validate_layer_refs(pipelines, layers)
    _validate_unique_targets(pipelines)
    return pipelines


def load_layers(directory: Path) -> dict[str, Layer]:
    """Load the optional, deployment-wide ordered layer list from
    pipelines/layers.yaml (or .yml). Absent, empty, or comment-only ⇒ {}
    (layers are simply unused everywhere — FR-020)."""
    layers: dict[str, Layer] = {}
    path = directory / "layers.yaml"
    if not path.is_file():
        path = directory / "layers.yml"
    if not path.is_file():
        return layers
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    if not raw:
        return layers
    for entry in raw.get("layers") or []:
        try:
            name = entry["name"]
        except KeyError as exc:
            raise PipelineError(f"layers.yaml: layer missing required key {exc}") from exc
        _check_name(name, "layer name", "layers.yaml")
        if name in layers:
            raise PipelineError(f"layers.yaml: duplicate layer '{name}'")
        layers[name] = Layer(name=name, label=entry.get("label", name.replace("_", " ").title()))
    return layers


def layers_to_yaml(layers: dict[str, Layer]) -> str:
    """Render the ordered layer dict back to layers.yaml text (PUT
    /api/lineage/layers) — plain, no comment preservation (there is no
    hand-authored content in this file worth preserving across a full
    replace, unlike model/pipeline yaml)."""
    doc = {"layers": [{"name": l.name, "label": l.label} for l in layers.values()]}
    return yaml.dump(doc, sort_keys=False, default_flow_style=False, width=1000)


# ---------------------------------------------------------------------------
# Lineage: validating declarations against a real run's output schema,
# matching a pipeline's target to a loaded model, and building the plain-dict
# payload semantic.replace_lineage_yaml regenerates into that model's yaml
# (specs/014-polars-pipeline-module/ US3).
# ---------------------------------------------------------------------------

def validate_lineage(lineage: list[LineageEntry], output_schema: list[dict]) -> list[dict]:
    """Compare declared lineage fields against a run's actual output schema
    (the runner-reported `[{name, dtype}]`). Never blocks the write (FR-018)
    — issues are informational, surfaced on the run and in the model's
    lineage section. Two kinds: `declared_missing` (declared but the output
    no longer has it) and `undeclared_field` (an output column nobody
    declared lineage for)."""
    output_fields = {f["name"] for f in output_schema}
    declared_fields = {entry.field for entry in lineage}
    issues: list[dict] = []
    for entry in lineage:
        if entry.field not in output_fields:
            issues.append({"kind": "declared_missing", "field": entry.field})
    for name in sorted(output_fields - declared_fields):
        issues.append({"kind": "undeclared_field", "field": name})
    return issues


def match_target_model(pipeline: Pipeline, models: dict) -> Optional[str]:
    """The name of the loaded model (if any) whose source scans this
    pipeline's target: delta targets match by exact path; parquet targets
    match when the model's source glob fnmatches the target's object key."""
    target = pipeline.target
    for name, model in models.items():
        src = model.source
        if target.format == "delta":
            if src.format == "delta" and src.path.rstrip("/") == target.path.rstrip("/"):
                return name
        elif src.format == "parquet" and fnmatch.fnmatch(target.path, src.path):
            return name
    return None


def _render_lineage_ref(ref: str, pipeline: Pipeline) -> str:
    """`source_name.column` -> `layer:source_name.column` when that source
    has a declared layer, else unchanged (data-model.md's model-section
    shape)."""
    src_name, _, _ = ref.partition(".")
    source = pipeline.sources.get(src_name)
    if source and source.layer:
        return f"{source.layer}:{ref}"
    return ref


def build_lineage_section(
    pipeline: Pipeline, output_schema: list[dict], issues: list[dict],
    updated: str, orphaned: bool = False,
) -> dict:
    """The plain dict semantic.replace_lineage_yaml regenerates into the
    matched model's yaml: declared lineage entries with layer-qualified
    source refs, marked stale where validation found the field missing from
    the output."""
    stale_fields = {i["field"] for i in issues if i["kind"] == "declared_missing"}
    fields = []
    for entry in pipeline.lineage:
        item = {
            "field": entry.field,
            "sources": [_render_lineage_ref(ref, pipeline) for ref in entry.sources],
            "transform": entry.transform,
        }
        if entry.field in stale_fields:
            item["stale"] = True
        fields.append(item)
    section = {"pipeline": pipeline.name, "updated": updated, "fields": fields}
    if orphaned:
        section["orphaned"] = True
    return section
