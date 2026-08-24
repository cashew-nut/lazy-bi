"""Sandbox notebooks: ad hoc, admin-authored SQL run against the same bucket
pipelines read from (the interactive stepping stone before a query is worth
saving as a pipeline — see app/pipelines.py).

Trust model: the same boundary a pipeline's `sql:` sits behind (Principle VI).
A notebook cell keeps the table functions a measure may never name, so it can
read any bucket path — that I/O reach is what the admin gate measures, and it
is the whole of it now that nothing here executes Python. Process isolation
(app/sandbox_runner.py) is for runaway-query containment and the killable
timeout, not the trust boundary.

This module only builds and parses text: combining a notebook's cells into one
script, detecting the table-function calls that would become pipeline sources,
rendering a starter pipeline yaml for "convert to pipeline", and re-validating
whatever the coding agent (app/sandbox_agent.py) proposes before any of it can
reach a cell or a pipeline yaml. Execution lives in app/sandbox_runner.py,
persistence in app/sandboxstore.py.
"""
from __future__ import annotations

import json
import re

# Matches the table-function calls that read a bucket path — the things that
# become a pipeline's declared `sources:` when a notebook is converted.
# Deliberately a simple regex, not a real parser: good enough to drive
# "convert to pipeline"'s source detection, never a source of truth for
# execution.
READ_FUNCTIONS = {
    "read_parquet": "parquet", "parquet_scan": "parquet",
    "read_csv": "csv", "read_csv_auto": "csv",
    "delta_scan": "delta", "iceberg_scan": "iceberg",
}
READ_RE = re.compile(
    r"\b(?P<fn>" + "|".join(sorted(READ_FUNCTIONS, key=len, reverse=True))
    + r")\s*\(\s*(?P<quote>['\"])(?P<path>[^'\"]*)(?P=quote)\s*\)",
    re.IGNORECASE,
)
# `CREATE [OR REPLACE] [TEMP] TABLE|VIEW output` — the notebook equivalent of
# the pipeline contract's named result
_OUTPUT_RE = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?(?:table|view)\s+"
    r"(?:if\s+not\s+exists\s+)?\"?output\"?\b", re.IGNORECASE)
_SANITIZE_RE = re.compile(r'[^a-z0-9_]+')
# a scalar safe to write unquoted in the generated yaml (see _yaml_scalar)
_YAML_PLAIN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.\-]*$')

SOURCE_FORMATS = ("parquet", "csv", "delta", "iceberg")

# The `target` value a proposed cell uses to mean "append a new cell" rather
# than replace an existing one. Lives here, not in app/sandbox_agent.py, so
# the tool schema the model sees and the validation it's checked against
# can't drift apart.
NEW_CELL = "new"

# Caps on what one agent reply may propose — a reply is applied straight into
# an admin's notebook, so a runaway response can't bury the notebook it was
# meant to help with.
MAX_AGENT_CELLS = 12
MAX_LINEAGE_ENTRIES = 200
MAX_TRANSFORM_CHARS = 200


def combine_cells(sources: list[str]) -> str:
    """Join a notebook's cell sources into one script, exactly the order the
    cells run in. Each cell is terminated with a semicolon if it hasn't got
    one — combining SQL statements needs a separator where combining python
    cells only needed a newline."""
    parts = []
    for source in sources:
        text = source.rstrip().rstrip(";").rstrip()
        if text:
            parts.append(text + ";")
    return "\n\n".join(parts)


def _mask_comments(script: str) -> str:
    """Same length and line structure as `script`, with every SQL comment
    region blanked to spaces — so a `read_parquet(...)` call merely
    *mentioned in a comment* (a very likely thing to write, e.g. an
    explanatory note) is never mistaken for a real source, while every real
    match's offsets still line up exactly with the original text for in-place
    rewriting.

    Both comment forms, and neither inside a string literal: `-- to end of
    line`, and `/* ... */` across lines."""
    out = list(script)
    i, n = 0, len(script)
    quote = None
    block = False
    while i < n:
        char = script[i]
        if block:
            if script.startswith("*/", i):
                out[i] = out[i + 1] = " "
                i += 2
                block = False
                continue
            if char != "\n":
                out[i] = " "
            i += 1
            continue
        if quote:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in "\"'":
            quote = char
            i += 1
            continue
        if script.startswith("--", i):
            while i < n and script[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if script.startswith("/*", i):
            block = True
            out[i] = out[i + 1] = " "
            i += 2
            continue
        i += 1
    return "".join(out)


def _infer_format(function_name: str, path: str) -> str:
    """The source format a table-function call implies. The function name is
    the answer — unlike the python `read()` helper it replaces, SQL names the
    reader explicitly, so an iceberg table root is never mistaken for a delta
    one."""
    fmt = READ_FUNCTIONS.get(function_name.lower())
    if fmt:
        return fmt
    return "csv" if path.lower().endswith(".csv") else "parquet"


def _slugify(text: str) -> str:
    """Sanitize to `[a-z][a-z0-9_]*`, or "" if nothing usable survives —
    callers decide the fallback (see _name_from_path, which walks up path
    segments rather than settling for a generic name)."""
    name = _SANITIZE_RE.sub("_", text.lower()).strip("_")
    if name and not name[0].isalpha():
        name = f"src_{name}"
    return name


def _name_from_path(path: str) -> str:
    """A friendly pipeline source name for a bucket path: the deepest path
    segment that sanitizes to something non-empty, walking up from the
    basename — so a glob like 'sales/*.parquet' names itself 'sales' (the
    dataset), not a meaningless generic fallback, since '*.parquet' alone
    sanitizes to nothing."""
    segments = [seg for seg in path.rstrip("/").split("/") if seg and seg != "*"]
    for seg in reversed(segments):
        stem = seg.split(".")[0].replace("*", "")
        candidate = _slugify(stem)
        if candidate:
            return candidate
    return "source"


def _unique_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    base, n = name, 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def extract_reads(script: str) -> list[dict]:
    """Every distinct bucket-reading table-function call in the script, in
    first-appearance order, each assigned a generated pipeline source name
    (`{name, path, format}`) derived from the path — the seed for "convert to
    pipeline"'s `sources:` list. Matches inside comments are ignored (see
    _mask_comments)."""
    masked = _mask_comments(script)
    sources: list[dict] = []
    seen_paths: set[str] = set()
    taken_names: set[str] = set()
    for m in READ_RE.finditer(masked):
        path = m.group("path")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        fmt = _infer_format(m.group("fn"), path)
        name = _unique_name(_name_from_path(path), taken_names)
        taken_names.add(name)
        sources.append({"name": name, "path": path, "format": fmt})
    return sources


def rewrite_reads_to_sources(script: str, sources: list[dict]) -> str:
    """Replace every bucket-reading table-function call with the declared
    source's name — a pipeline registers each source as a view under that name
    (app/pipeline_runner.py), so the rewritten SQL reads `FROM sales` where the
    notebook read `FROM read_parquet('s3://.../sales/*.parquet')`.

    Keyed by exact path text, so repeated reads of the same path become the
    same declared source, matching pipeline semantics. Matches are located
    against the comment-masked text (see _mask_comments) but substituted into
    the real script, so comments are never touched or mistaken for call
    sites."""
    masked = _mask_comments(script)
    name_by_path = {s["path"]: s["name"] for s in sources}
    out = []
    last = 0
    for m in READ_RE.finditer(masked):
        name = name_by_path.get(m.group("path"))
        if name is None:
            continue
        out.append(script[last:m.start()])
        out.append(f'"{name}"')
        last = m.end()
    out.append(script[last:])
    return "".join(out)


def has_output_assignment(script: str) -> bool:
    """Whether the script satisfies the pipeline output contract: it ends on a
    SELECT, or it creates a relation named `output` (see
    contracts/pipeline-yaml.md). Convert-to-pipeline surfaces a warning rather
    than guessing when neither holds."""
    masked = _mask_comments(script)
    if _OUTPUT_RE.search(masked):
        return True
    statements = [s.strip() for s in masked.split(";") if s.strip()]
    if not statements:
        return False
    return statements[-1].lstrip("(").lstrip().upper().startswith(
        ("SELECT", "WITH", "FROM", "TABLE"))


def _sql_syntax_error(source: str) -> "str | None":
    """DuckDB's own complaint about a cell, or None if it parses.

    Parse only — nothing is bound and nothing runs. A syntax error is
    *reported*, not a rejection: this is a scratch notebook, and a half-written
    cell the admin fixes in place beats a silently discarded proposal."""
    from . import duck

    try:
        duck.cursor().extract_statements(source)
        return None
    except Exception as exc:
        return "syntax error: " + str(exc).strip().split("\n")[0]


def _yaml_scalar(value: object) -> str:
    """One-line yaml scalar, quoted unless it is unambiguously plain. JSON
    strings are valid yaml double-quoted scalars, so json.dumps is a correct
    (deliberately conservative) quoter — and keeps build_pipeline_yaml a
    readable hand-rendered template rather than a yaml.dump call whose block
    style would mangle the script."""
    text = " ".join(str(value).split())
    # ensure_ascii=False so a '×' in a transform stays a '×' in the file the
    # admin then reads and edits, rather than a × escape
    return text if _YAML_PLAIN_RE.match(text) else json.dumps(text, ensure_ascii=False)


def validate_agent_cells(raw_cells: object, known_ids: list[str]) -> tuple[list[dict], list[str]]:
    """Re-check the coding agent's proposed cells before they can be offered
    for one-click application (app/sandbox_agent.py is *unvalidated* output
    by contract, exactly like app/llm.py's). Returns
    `([{target_id, source, syntax_error}], warnings)`:

    - `target_id` is an existing cell's id (replace) or None (append a new
      cell). A target naming a cell that isn't in the notebook is downgraded
      to an append rather than dropped — the code is still useful, it just
      can't safely overwrite something it may not have seen.
    - a syntax error is *reported*, not a rejection: this is a scratch
      notebook, and a half-written cell the admin fixes in place beats a
      silently discarded proposal. Nothing is executed here either way.
    """
    warnings: list[str] = []
    if not isinstance(raw_cells, list):
        return [], ["the agent returned no usable cells"]
    known = set(known_ids)
    out: list[dict] = []
    seen_targets: set[str] = set()
    for raw in raw_cells:
        if not isinstance(raw, dict):
            continue
        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            warnings.append("dropped a proposed cell with no source")
            continue
        target = raw.get("target")
        target_id = None
        if isinstance(target, str) and target in known:
            if target in seen_targets:
                warnings.append(f"dropped a second proposal for cell '{target}'")
                continue
            seen_targets.add(target)
            target_id = target
        elif isinstance(target, str) and target and target != NEW_CELL:
            warnings.append(f"'{target}' isn't a cell in this notebook — offered as a new cell instead")
        source = source.rstrip()
        syntax_error = _sql_syntax_error(source)
        out.append({"target_id": target_id, "source": source, "syntax_error": syntax_error})
        if len(out) >= MAX_AGENT_CELLS:
            warnings.append(f"kept the first {MAX_AGENT_CELLS} proposed cells")
            break
    if not out and not warnings:
        warnings.append("the agent returned no usable cells")
    return out, warnings


def validate_lineage(
    raw_entries: object, source_names: list[str], output_columns: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Re-check agent-generated lineage against what the platform already
    knows, so a generated `lineage:` section can only ever say things the
    pipeline loader would accept (app/pipelines.py's _parse_lineage rejects
    an unknown source outright — a pipeline that fails to load is a worse
    outcome than a thinner lineage section).

    Dropped, with a warning each time: a field the run's real output schema
    doesn't contain, a duplicate field, and a `from` ref naming something
    that isn't a declared source. An entry whose refs were *all* dropped
    goes too — a field with invented provenance is worse than an absent one.
    `output_columns` empty means the notebook was never run, so field names
    can't be checked; they're taken at face value then.
    """
    warnings: list[str] = []
    if not isinstance(raw_entries, list):
        return [], ["the agent returned no usable lineage"]
    known_sources = set(source_names)
    known_fields = set(output_columns or [])
    entries: list[dict] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        field_name = raw.get("field")
        if not isinstance(field_name, str) or not field_name.strip():
            continue
        field_name = field_name.strip()
        if known_fields and field_name not in known_fields:
            warnings.append(f"dropped lineage for '{field_name}' — not a column the run produced")
            continue
        if field_name in seen:
            warnings.append(f"dropped a duplicate lineage entry for '{field_name}'")
            continue
        seen.add(field_name)
        raw_refs = raw.get("from") or []
        raw_refs = raw_refs if isinstance(raw_refs, list) else [raw_refs]
        refs = []
        dropped = False
        for ref in raw_refs:
            if not isinstance(ref, str) or not ref.strip():
                continue
            ref = ref.strip()
            if ref.split(".", 1)[0] not in known_sources:
                dropped = True
                continue
            if ref not in refs:
                refs.append(ref)
        if dropped and not refs:
            warnings.append(f"dropped lineage for '{field_name}' — it cited no declared source")
            seen.discard(field_name)
            continue
        if dropped:
            warnings.append(f"lineage for '{field_name}': dropped ref(s) to undeclared sources")
        transform = raw.get("transform")
        transform = " ".join(transform.split())[:MAX_TRANSFORM_CHARS] if isinstance(transform, str) else ""
        entries.append({"field": field_name, "from": refs, "transform": transform})
        if len(entries) >= MAX_LINEAGE_ENTRIES:
            warnings.append(f"kept the first {MAX_LINEAGE_ENTRIES} lineage entries")
            break
    return entries, warnings


def bucket_entries(objects: list[dict], bucket: str) -> list[dict]:
    """Bucket objects collapsed to the things a `read(...)` call can actually
    name: one entry per Delta/Iceberg table *root* (not its hundreds of
    internal files, which no one reads directly and which would swamp the
    agent's context budget), plus every standalone csv/parquet object. A
    table root's size is the roll-up of its members'."""
    roots: dict[str, str] = {}
    for obj in objects:
        key = obj.get("key", "")
        if "/_delta_log/" in key:
            roots[key.split("/_delta_log/", 1)[0]] = "delta"
        elif key.endswith(".metadata.json") and "/metadata/" in key:
            roots.setdefault(key.split("/metadata/", 1)[0], "iceberg")
    sizes = dict.fromkeys(roots, 0)
    files = []
    for obj in objects:
        key, size = obj.get("key", ""), obj.get("size") or 0
        root = next((r for r in roots if key.startswith(r + "/")), None)
        if root:
            sizes[root] += size
            continue
        fmt = "csv" if key.endswith(".csv") else "parquet" if key.endswith(".parquet") else None
        if fmt:
            files.append({"path": f"s3://{bucket}/{key}", "format": fmt, "size": size})
    tables = [{"path": f"s3://{bucket}/{r}", "format": f, "size": sizes[r]} for r, f in roots.items()]
    return sorted(tables + files, key=lambda e: e["path"])


def build_pipeline_yaml(
    name: str, script: str, sources: list[dict],
    lineage: list[dict] | None = None, description: str = "",
) -> str:
    """Render a starter pipeline yaml from a sandbox notebook's combined,
    source-rewritten SQL. Target and materialization are left as
    clearly-marked placeholders — a sandbox notebook never declares a target,
    so the admin must set a real one before saving. Plain string templating
    (not yaml.dump) so the script's literal block style reads naturally,
    matching editor.js's hand-authored NEW_PIPELINE_TEMPLATE.

    `description` and `lineage` are optional and, when the conversion asked
    for it, come from the coding agent — already re-validated by
    validate_lineage above, never raw model output."""
    slug = _slugify((name or "my_pipeline").strip()) or "my_pipeline"
    lines = [
        f"# converted from sandbox notebook '{name}' — review target + materialization below before saving",
        f"name: {slug}",
    ]
    if description:
        lines.append(f"description: {_yaml_scalar(description)}")
    lines.append("sources:")
    if sources:
        for s in sources:
            lines += [f"  - name: {s['name']}", f"    format: {s['format']}", f"    path: {s['path']}"]
    else:
        lines += ["  - name: raw", "    format: parquet", "    path: s3://REPLACE/ME/*.parquet   # TODO"]
    lines += [
        "target:",
        "  path: s3://REPLACE/ME/target   # TODO: set a real target path before saving",
        "  format: delta",
        "materialization:",
        "  mode: replace",
        "sql: |",
    ]
    lines += [f"  {line}" if line.strip() else "" for line in script.split("\n")]
    if lineage:
        lines.append("lineage:")
        for entry in lineage:
            refs = ", ".join(_yaml_scalar(r) for r in entry.get("from", []))
            lines += [f"  - field: {_yaml_scalar(entry['field'])}", f"    from: [{refs}]"]
            if entry.get("transform"):
                lines.append(f"    transform: {_yaml_scalar(entry['transform'])}")
    return "\n".join(lines) + "\n"
