"""Sandbox notebooks: ad hoc, admin-authored polars/python scratch scripts
run against the same bucket pipelines read from (the interactive stepping
stone before a script is worth saving as a pipeline — see app/pipelines.py).

Trust model: identical carve-out to a pipeline's `script:` (Principle VI) —
real, unsandboxed Python at application-code trust, admin-gated for both
authoring and execution, process-isolated (app/sandbox_runner.py) purely for
crash/timeout containment, not as the trust boundary. This is not a new
eval-capable construct, just the existing pipeline-script carve-out applied
to throwaway exploratory code instead of a saved, materializing pipeline —
see the constitution's Principle VI note.

This module only builds/parses text: combining a notebook's cells into one
script, detecting `read("path")` bucket-scan calls, and rendering a starter
pipeline yaml for "convert to pipeline". Execution lives in
app/sandbox_runner.py, persistence in app/sandboxstore.py.
"""
from __future__ import annotations

import re

# Matches a `read("path"[, format="fmt"])` call as it appears in sandbox
# notebook source — the one bucket-access primitive the runner's namespace
# provides (see app/sandbox_runner.py's _make_read). Deliberately a simple
# regex, not a real parser: good enough to drive "convert to pipeline"'s
# source detection, never a source of truth for execution.
READ_RE = re.compile(
    r'read\(\s*(["\'])(?P<path>.*?)\1\s*(?:,\s*format\s*=\s*(["\'])(?P<format>\w+)\3\s*)?\)'
)
_OUTPUT_RE = re.compile(r'(?m)^output\s*=')
_SANITIZE_RE = re.compile(r'[^a-z0-9_]+')

SOURCE_FORMATS = ("parquet", "csv", "delta")


def combine_cells(sources: list[str]) -> str:
    """Join a notebook's cell sources into one script, exactly the order the
    cells run in — a blank line between cells keeps them visually distinct
    without changing execution semantics (blank lines are no-ops in Python)."""
    return "\n\n".join(s.rstrip() for s in sources if s.strip())


def _mask_comments(script: str) -> str:
    """Same length/line-structure as `script`, with every `# comment` region
    blanked to spaces — so a `read("...")` call merely *mentioned in a
    comment* (a very likely thing to write, e.g. an explanatory note) is
    never mistaken for a real source, while every real match's offsets still
    line up exactly with the original text for in-place rewriting. A `#`
    only starts a comment outside a quoted string and at a token boundary —
    mirrors static/js/yamlhighlight.js's splitComment."""
    out_lines = []
    for line in script.split("\n"):
        in_quote = None
        for i, c in enumerate(line):
            if in_quote:
                if c == in_quote and line[i - 1] != "\\":
                    in_quote = None
                continue
            if c in "\"'":
                in_quote = c
                continue
            if c == "#" and (i == 0 or line[i - 1].isspace()):
                line = line[:i] + " " * (len(line) - i)
                break
        out_lines.append(line)
    return "\n".join(out_lines)


def _infer_format(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith(".parquet"):
        return "parquet"
    return "delta"   # a bare table root, same default app/pipelines.py uses


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
    """Every distinct `read("path"[, format="fmt"])` call in the script, in
    first-appearance order, each assigned a generated pipeline source name
    (`{name, path, format}`) derived from the path — the seed for "convert
    to pipeline"'s `sources:` list. Matches inside comments are ignored
    (see _mask_comments)."""
    masked = _mask_comments(script)
    sources: list[dict] = []
    seen_paths: set[str] = set()
    taken_names: set[str] = set()
    for m in READ_RE.finditer(masked):
        path = m.group("path")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        fmt = m.group("format") or _infer_format(path)
        name = _unique_name(_name_from_path(path), taken_names)
        taken_names.add(name)
        sources.append({"name": name, "path": path, "format": fmt})
    return sources


def rewrite_reads_to_sources(script: str, sources: list[dict]) -> str:
    """Replace every `read("path"[, format=...])` call with `sources["name"]`
    (the pipeline script convention — app/pipeline_runner.py's `sources`
    dict), keyed by exact path text so repeated reads of the same path
    become the same declared source, matching pipeline semantics. Matches
    are located against the comment-masked text (see _mask_comments) but
    substituted into the real script, so comments are never touched or
    mistaken for call sites."""
    masked = _mask_comments(script)
    name_by_path = {s["path"]: s["name"] for s in sources}
    out = []
    last = 0
    for m in READ_RE.finditer(masked):
        name = name_by_path.get(m.group("path"))
        if name is None:
            continue
        out.append(script[last:m.start()])
        out.append(f'sources["{name}"]')
        last = m.end()
    out.append(script[last:])
    return "".join(out)


def has_output_assignment(script: str) -> bool:
    """Whether the script assigns a top-level `output` variable — the
    pipeline script contract (see contracts/pipeline-yaml.md). Convert-to-
    pipeline surfaces a warning rather than guessing when this is false."""
    return bool(_OUTPUT_RE.search(script))


def build_pipeline_yaml(name: str, script: str, sources: list[dict]) -> str:
    """Render a starter pipeline yaml from a sandbox notebook's combined,
    source-rewritten script. Target and materialization are left as
    clearly-marked placeholders — a sandbox notebook never declares a target,
    so the admin must set a real one before saving. Plain string templating
    (not yaml.dump) so the script's literal block style reads naturally,
    matching editor.js's hand-authored NEW_PIPELINE_TEMPLATE."""
    slug = _slugify((name or "my_pipeline").strip()) or "my_pipeline"
    lines = [
        f"# converted from sandbox notebook '{name}' — review target + materialization below before saving",
        f"name: {slug}",
        "sources:",
    ]
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
        "script: |",
    ]
    lines += [f"  {line}" if line.strip() else "" for line in script.split("\n")]
    return "\n".join(lines) + "\n"
