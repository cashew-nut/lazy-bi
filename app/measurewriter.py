"""AI-authored measures: the seam that writes a measure from a sentence.

Same architecture as app/llm.py (query translation) and app/composer.py (page
composition), applied to measure authoring: the model is forced through one
typed tool call (`write_measure` or `decline`), and what comes back is
*unvalidated* until this module has re-checked it — compiled through the very
same app/sqlgrammar.py path a hand-typed measure goes through, and then
actually **run** against the live source as an inline measure.

The difference from the other two seams is that measure authoring has an
oracle: a measure either compiles and returns rows or it doesn't. So a
proposal that fails is never handed back as a measure — the failure goes to
the model as a repair instruction and it tries again (MAX_ATTEMPTS times in
total), which is what makes a complex `from:` measure — the kind that computes
a per-entity intermediate result before aggregating it — worth asking for at
all. If it still fails, the caller gets the error and the last attempt, never
a measure that doesn't run.

The other half of "good at it" is context, rebuilt live per request by
build_context(): the fact table's real columns and types, every declared
dimension (with real sample values for the categorical ones, so a
`FILTER (WHERE …)` predicate matches what is actually stored), every existing
measure *with its formula* (the house style to match, and the sibling names a
window measure is allowed to read), and — in the visual scope — the query the
measure is being written into and the parameters it may reference. None of
that comes from the client: the client names a model and a scope, the server
introspects. What the model is told about the grammar is likewise built from
sqlgrammar's own constants rather than restated, so the prompt can't drift
from what the validator enforces.

Nothing here writes anything: a verified proposal goes back to the UI as a
draft, and it is saved (or not) through the ordinary author-gated measure
endpoints in app/api/models.py — one write path for a measure, the same one a
human typing into the measure lab uses.

Swappable by design: tests use a FakeWriter implementing the Writer protocol,
so the whole propose -> verify -> repair contract runs with zero network calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Literal, Optional, Protocol, Union

from . import config, engine, llmclient, semantic, sqlgrammar

Scope = Literal["model", "visual"]
SCOPES = ("model", "visual")

# The formats a measure can declare — the same three the measure lab's picker,
# the guided form and app/api/models.py's _validate_measure_body know about.
FORMATS = ("number", "currency", "percent")

MEASURE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

# One proposal plus two repairs. Every attempt is a full model call and (when
# it gets that far) one real query against the source, so the ceiling is
# deliberately low: past the second failure the cause is nearly always
# something no amount of re-prompting fixes — a column that isn't in the data
# — and the human is better served by seeing the error than by waiting.
MAX_ATTEMPTS = 3

# Sample values are what let a proposed `FILTER (WHERE channel = 'Online')`
# name a value that actually exists. Bounded twice: a dimension with more
# distinct values than this is closer to free text than a vocabulary (same
# reasoning as nlq.SAMPLE_VALUES_LIMIT, smaller because a measure predicate
# needs the vocabulary, not the whole census), and only the first
# SAMPLE_DIMENSIONS_LIMIT categorical dimensions are sampled at all since each
# one costs a SELECT DISTINCT against the source.
SAMPLE_VALUES_LIMIT = 60
SAMPLE_DIMENSIONS_LIMIT = 12

# How many rows the verifying dry run asks for. It exists to prove the measure
# compiles and executes, not to compute anything, so it stays tiny.
DRY_RUN_LIMIT = 5


# ── the context (built live, never taken from the client) ───────────────────

@dataclass(frozen=True)
class MeasureContext:
    """Everything the model is shown about where this measure will live.

    `scope` is what the measure is being written *for*, and it changes the
    rules rather than just the wording: a visual-scoped measure may reference
    the visual's declared `param()`s and its sibling inline measures and is
    checked against the query it is being written into; a model-scoped one may
    not reference a parameter at all (app/api/models.py refuses to save one),
    so the prompt never offers the construct and check() rejects it.
    """
    model: str
    label: str = ""
    description: str = ""
    dataset: str = ""                 # the fact table (model part) it belongs to
    source: str = ""                  # its path, for orientation only
    source_format: str = ""
    scope: Scope = "model"
    columns: list[dict] = field(default_factory=list)      # [{name, dtype}]
    dimensions: list[dict] = field(default_factory=list)   # [{name, column, type, label, description, sample_values?}]
    measures: list[dict] = field(default_factory=list)     # [{name, label, format, description, expr, from, emits, inline?}]
    parameters: list[dict] = field(default_factory=list)   # visual-declared [{name, type, values, default}]
    query: Optional[dict] = None      # the visual's current query, as the builder has it
    inline_measures: list[dict] = field(default_factory=list)  # the visual's other ad-hoc measures
    editing: Optional[dict] = None    # the measure being rewritten, if this is an edit
    schema_error: str = ""            # set when the source could not be scanned

    @property
    def taken_names(self) -> set:
        """Names a new measure may not claim — every declared dimension and
        measure, minus the one being rewritten."""
        names = {d["name"] for d in self.dimensions} | {m["name"] for m in self.measures}
        if self.editing and self.editing.get("name"):
            names.discard(self.editing["name"])
        return names


def target_part(model: semantic.Model, dataset: str = "") -> semantic.Model:
    """The single fact table a measure will belong to.

    A measure is scoped to one fact table (semantic.py's `_check_unique_measures`
    — measure names are the model's public namespace, but the *expression* only
    ever sees one part's columns), so a composite model has to say which. With
    one part the answer is that part; with several, `dataset` names it.
    """
    parts = list(model.parts)
    if dataset:
        for part in parts:
            if part.name == dataset or dataset in part.datasets:
                return part.model
        raise ValueError(
            f"'{dataset}' is not a fact table of model '{model.name}' "
            f"(it holds {', '.join(p.name for p in parts)})")
    if len(parts) > 1:
        raise ValueError(
            f"model '{model.name}' holds {len(parts)} unrelated fact tables "
            f"({', '.join(p.name for p in parts)}) — name the one the measure belongs to")
    return parts[0].model


def _sample_values(model: semantic.Model, dim: semantic.Dimension) -> Optional[list]:
    """Real stored values for a categorical dimension, or None.

    Same shape as nlq._dimension_sample_values and the same rule: never let
    building the context fail (or hang) because one dimension's source can't be
    scanned or turns out to be free text.
    """
    if dim.type != "categorical" or dim.spine:
        return None
    try:
        values = engine.dimension_values(model, dim.name, limit=SAMPLE_VALUES_LIMIT + 1)
    except Exception:
        return None
    if len(values) > SAMPLE_VALUES_LIMIT:
        return None
    return values or None


def _measure_entry(m: semantic.Measure) -> dict:
    entry = {"name": m.name, "label": m.label, "format": m.format,
             "description": m.description, "expr": m.expr_source}
    if m.from_source:
        entry["from"] = m.from_source
    if m.emits:
        entry["emits"] = list(m.emits)
    return entry


def build_context(model: semantic.Model, *, scope: Scope = "model",
                  dataset: str = "", query: Optional[dict] = None,
                  parameters: Optional[list] = None,
                  editing: Optional[dict] = None) -> MeasureContext:
    """Introspect everything the writer needs about one fact table.

    `model` is a whole model; the part `dataset` names (or its only part) is
    what the context — and every later check — is built against.
    """
    fact = target_part(model, dataset)
    part_name = next((p.name for p in model.parts if p.model is fact), fact.name)
    query = dict(query or {})
    inline = [dict(m) for m in (query.get("inline_measures") or []) if m.get("name")]

    columns: list[dict] = []
    schema_error = ""
    try:
        columns = [{"name": n, "dtype": str(t)} for n, t in engine.scan_schema(fact).items()]
    except Exception as exc:      # an unreachable bucket must not 500 the route
        schema_error = f"source not reachable: {exc}"

    dimensions = []
    sampled = 0
    for dim in fact.dimensions.values():
        entry = {"name": dim.name, "column": dim.column, "type": dim.type,
                 "label": dim.label, "description": dim.description}
        if dim.spine:
            entry["spine"] = {"start": dim.spine.start, "end": dim.spine.end}
        if not schema_error and sampled < SAMPLE_DIMENSIONS_LIMIT:
            values = _sample_values(fact, dim)
            if values is not None:
                entry["sample_values"] = values
                sampled += 1
        dimensions.append(entry)

    measures = [_measure_entry(m) for m in fact.measures.values()]
    for m in inline:
        measures.append({"name": m["name"], "label": m.get("label", ""),
                         "format": m.get("format", "number"), "description": "",
                         "expr": m.get("expr", ""),
                         **({"from": m["from"]} if m.get("from") else {}),
                         **({"emits": list(m["emits"])} if m.get("emits") else {}),
                         "inline": True})

    return MeasureContext(
        model=model.name, label=model.label, description=model.description,
        dataset=part_name,
        source=fact.source.path if fact.source else "",
        source_format=fact.source.format if fact.source else "",
        scope=scope if scope in SCOPES else "model",
        columns=columns, dimensions=dimensions, measures=measures,
        parameters=[dict(p) for p in (parameters or query.get("parameters") or [])],
        query=query or None, inline_measures=inline,
        editing=dict(editing) if editing else None,
        schema_error=schema_error,
    )


# ── the proposal (unvalidated) ──────────────────────────────────────────────

@dataclass(frozen=True)
class RawMeasure:
    """The model's unvalidated measure. Nothing in here is trusted until
    check()/verify() have run — the field names are the API's, not the
    grammar's, so a bad `format` or a `from:` block naming another table is
    an ordinary rejection rather than an exception somewhere downstream."""
    name: str
    expr: str
    label: str = ""
    format: str = "number"
    description: str = ""
    from_source: Optional[str] = None
    emits: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict:
        """The measure-shaped dict the API returns and the measure lab / model
        form load straight into their editors."""
        out = {"name": self.name, "label": self.label, "format": self.format,
               "description": self.description, "expr": self.expr,
               "synonyms": list(self.synonyms)}
        if self.from_source:
            out["from"] = self.from_source
            out["emits"] = list(self.emits)
        return out

    def to_query_dict(self) -> dict:
        """The same measure as an inline measure the engine can run."""
        out = {"name": self.name, "label": self.label or self.name,
               "format": self.format, "expr": self.expr}
        if self.from_source:
            out["from"] = self.from_source
            if self.emits:
                out["emits"] = list(self.emits)
        return out


@dataclass(frozen=True)
class RawDecline:
    reason: str


RawProposal = Union[RawMeasure, RawDecline]


@dataclass(frozen=True)
class Attempt:
    """One rejected proposal, fed back into the next call so the model repairs
    the cause instead of re-proposing it."""
    measure: RawMeasure
    error: str


@dataclass(frozen=True)
class WriteRequest:
    instruction: str
    context: MeasureContext
    history: list[dict] = field(default_factory=list)   # [{instruction, summary}]
    attempts: list[Attempt] = field(default_factory=list)
    thinking: Optional[bool] = None


@dataclass(frozen=True)
class WriteStreamEvent:
    """One incremental update from Writer.write_streaming(). Everything but
    "done" is display-only — only the final proposal is ever verified, and
    only a verified one reaches the caller as a measure."""
    kind: Literal["thinking", "draft", "done"]
    text: str = ""
    draft: Optional[dict] = None            # partial tool args, for a live view
    final: Optional[RawProposal] = None


class WriterError(Exception):
    """The model call itself failed (network/timeout/API error) — distinct
    from a bad *proposal*, which is check()/verify()'s business."""


class Writer(Protocol):
    def write_streaming(self, request: WriteRequest) -> Iterator[WriteStreamEvent]: ...


# ── the tools ───────────────────────────────────────────────────────────────

_WRITE_TOOL = {
    "name": "write_measure",
    "eager_input_streaming": True,
    "description": (
        "Define the measure. `expr` is a SQL aggregate over the fact table's "
        "rows; add `from` when the aggregate has to read rows you derive first "
        "(a per-entity intermediate result), and `emits` when that block "
        "computes a dimension of its own."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "snake_case, unique against every declared dimension and measure "
                    "listed in the prompt. Name what it measures, not how it is computed "
                    "(avg_order_value, not sum_div_count)."
                ),
            },
            "label": {"type": "string", "description": "Title Case display name, e.g. 'Average Order Value'."},
            "format": {
                "type": "string",
                "enum": list(FORMATS),
                "description": (
                    "currency for money, percent for a RATIO — a percent measure must "
                    "return a fraction (0.42), never 42, because the UI multiplies by 100."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "One or two sentences for a business reader: what the number means, "
                    "which rows it counts, and any condition it needs (a window measure "
                    "needs a time dimension in the query; a from: measure names the grain "
                    "it derives). This is what the chat assistant later reads to decide "
                    "whether this measure answers a question, so be precise about "
                    "denominators and exclusions."
                ),
            },
            "expr": {
                "type": "string",
                "description": (
                    "The SQL aggregate itself, and nothing else — no SELECT, no FROM, "
                    "no GROUP BY, no trailing semicolon or comment."
                ),
            },
            "from": {
                "type": ["string", "null"],
                "description": (
                    "Optional. A complete SELECT producing the rows `expr` aggregates, "
                    "using {model} for the fact scan and {dims} for the query's grouping "
                    "columns. Omit entirely for an ordinary measure."
                ),
            },
            "emits": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Only with `from`: declared dimension name(s) the block computes "
                    "itself rather than inheriting from the raw rows."
                ),
            },
            "synonyms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Other business terms for this measure, for the chat assistant's vocabulary.",
            },
            "rationale": {
                "type": "string",
                "description": (
                    "1-3 sentences to the author: why this shape (plain / window / from:), "
                    "what the intermediate step computes if there is one, and anything they "
                    "should check. Not stored with the measure."
                ),
            },
        },
        "required": ["name", "expr", "format", "description", "rationale"],
    },
}

_DECLINE_TOOL = {
    "name": "decline",
    "eager_input_streaming": True,
    "description": (
        "The measure cannot be written from the columns and dimensions listed "
        "— the data needed isn't there, the request needs a join this model "
        "doesn't have, or it isn't a measure at all (it asks for a filter, a "
        "dimension, or row-level output). Say plainly what is missing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    },
}

_TOOLS = [_WRITE_TOOL, _DECLINE_TOOL]


# ── the prompt ──────────────────────────────────────────────────────────────

def _aggregate_hint() -> str:
    """A real sample of the live function allowlist, not a remembered one.

    sqlgrammar derives what may be called from DuckDB's own catalog, so the
    only honest way to tell the model what it may use is to read the same
    catalog — and to read it the same way the validator does: the aggregate
    names, intersected with the allowlist, so nothing denied (`current_user`,
    `random`, the pg_* introspection family) is ever advertised as available.
    The prefix filter below only trims families that are aggregates in name
    only (list/struct/json builders); it narrows what is *shown*, never what
    is allowed, which is why the line says "include".

    Best-effort: with no connection the rule is stated without the list rather
    than failing the request."""
    noise = ("list_", "array_", "json", "map_", "struct_", "union_", "enum_",
             "has_", "inet_", "format_", "current_", "get_", "st_", "bit",
             "col_description", "generate_subscripts", "ago", "date_add",
             "days_in_month", "fill", "fmod", "fdiv")
    try:
        names = sorted(sqlgrammar.aggregate_functions() & sqlgrammar.allowed_functions())
    except Exception:
        return ""
    names = [n for n in names if not n.startswith(noise)]
    if not names:
        return ""
    return "Aggregates here include: " + ", ".join(names[:140]) + "."


_SYSTEM_PROMPT_HEAD = """\
You write measures for a BI semantic layer. A measure is one SQL aggregate, and \
the engine evaluates every measure the same way:

    SELECT <the query's dimensions>, <your expr>
    FROM   <the fact table's scan, or your own from: block>
    GROUP BY <the query's dimensions>

You never write the SELECT list, the GROUP BY or the WHERE — a query supplies \
those at runtime, differently every time. Your measure has to stay correct for \
every grouping and every filter anyone will ever apply to it. Write the \
aggregate, nothing else.

Answer with exactly one tool call: write_measure, or decline when the columns \
and dimensions listed simply cannot express what was asked.

THREE SHAPES — pick the simplest one that is actually correct.

1. PLAIN — an aggregate over the fact table's own rows. Most measures are this.
     SUM(unit_price * quantity)
     COUNT(DISTINCT customer_id)
     SUM(amount) FILTER (WHERE status = 'Refunded')
     SUM(amount) / NULLIF(COUNT(DISTINCT order_id), 0)
   Everything ordinary SQL allows inside an aggregate is allowed: arithmetic, \
CASE WHEN, CAST, COALESCE, NULLIF, IN, BETWEEN, LIKE/ILIKE, IS NULL, DISTINCT \
aggregates and aggregate FILTER (WHERE ...). A ratio of two aggregates \
(SUM(a) / SUM(b)) is a plain measure — the division happens after both sides \
are reduced, which is exactly right.

2. WINDOW — reads other measures' ALREADY-AGGREGATED values, after the group-by.
     SUM(revenue) OVER w                                      -- running total
     LAG(revenue) OVER w                                      -- previous period
     (revenue - LAG(revenue) OVER w) / NULLIF(LAG(revenue) OVER w, 0)
   `w` is supplied by the engine: PARTITION BY the query's other dimensions, \
ORDER BY its time dimension. Write `OVER w` verbatim — never a PARTITION BY or \
ORDER BY of your own, and never a window over a raw column.
   Inside a window expression a bare name is a SIBLING MEASURE (one of the \
declared measures listed below), never a source column. The query it runs in \
must have a time dimension and at least one plain measure, so say so in the \
description. A window measure cannot read another window measure, or a measure \
that has a from: block.

3. COMPLEX (from:) — the aggregate reads rows you DERIVE, not the fact rows. \
This is the shape for anything that has to be computed per entity before it can \
be aggregated.
     expr: MEDIAN(tenure_days)
     from: |
       SELECT {dims},
              date_diff('day', start_date, end_date) AS tenure_days,
              date_trunc('month', end_date)          AS churn_month
       FROM {model}
       WHERE end_date IS NOT NULL
     emits: [churn_month]

   {model}  the fact table's scan for this query, with the query's own filters \
already applied. It is the only table you may name, besides CTEs you declare \
yourself in the block.
   {dims}   the query's grouping columns, already quoted and comma-separated \
(e.g. "region", "order_date"). It is never empty — the engine carries a constant \
column when a query groups by nothing — so `SELECT {dims}, ...` and \
`GROUP BY {dims}, ...` are always safe to write. A time dimension arrives \
already truncated to the grain the query asked for. Inside {model} you can also \
name each declared dimension by its own name, as well as every source column.

   THE RULE THAT BREAKS MOST FIRST ATTEMPTS: every column in {dims} must survive \
to the block's output. Carry {dims} through every level — the final SELECT and \
its GROUP BY, and any CTE the final SELECT reads from. Dropping one fails the \
measure at query time naming the column it lost.

   `emits:` is for a dimension the BLOCK computes rather than the raw rows (a \
per-entity milestone date, a cohort month). An emitted dimension is withheld from \
{dims} while the block runs and grouped on the block's output afterwards, so a \
timeline buckets what you derived — output it as a column named exactly like the \
declared dimension. Emit only declared dimensions, and only ones the block \
really outputs.

   REACH FOR from: WHEN THE NUMBER IS:
   - an aggregate of an aggregate — the median/average/max of a per-customer, \
per-order or per-store total. AVG(SUM(x)) is not legal SQL: compute the SUM per \
entity in the block (GROUP BY {dims}, entity), then AVG it in expr.
   - about a coarser entity than the rows — fact rows are order LINES but the \
question is about ORDERS (average order value, orders over $500): collapse to \
one row per order in the block first.
   - a count of entities meeting a threshold — produce one row per qualifying \
entity in the block (GROUP BY ... HAVING ...), then COUNT(*) in expr.
   - about the first or last event per entity — cohort/first-touch/latest status: \
QUALIFY row_number() OVER (PARTITION BY entity ORDER BY ts) = 1 inside the block.
   - semi-additive — a balance or inventory level that must be taken at the \
latest date per entity and only then summed.
   - a weighted average or a rate of rates, where the weights have to be built \
per group before they can be combined.
   The block may use CTEs, joins between its own CTEs, window functions, QUALIFY, \
HAVING, DISTINCT. It may NOT name any table other than {model} and its own CTEs, \
and may not call a table function (read_parquet, glob, iceberg_scan, ...) — those \
are refused outright.

   Do NOT use from: when a plain aggregate is already correct. It costs an extra \
pass and is harder to read: SUM(a)/SUM(b), COUNT(DISTINCT x) and \
SUM(x) FILTER (WHERE ...) are all plain measures.

GROUNDING — everything you may name is in the prompt below.
- expr (when there is no from: block) may name SOURCE COLUMNS only, from the \
list below; joined lookup columns are already in it. A dimension's `name` is not \
a column — use the column it maps to. In a window expression, bare names are \
sibling measure names instead.
- Inside from:, name source columns and declared dimensions; the block's own \
output columns are what expr then aggregates.
- Never invent a column, a dimension, a table or a function. If what you need \
isn't listed, decline and say which column is missing.
- When a predicate compares a categorical column to a value, use one of that \
dimension's real sample values, exactly as stored (case included).

CORRECTNESS HABITS THAT MATTER HERE:
- Guard every division: `/ NULLIF(denominator, 0)`.
- Integer division truncates — multiply by 1.0 or CAST(... AS DOUBLE) when the \
result is a rate.
- COUNT(x) skips NULLs, COUNT(*) doesn't; COUNT(DISTINCT x) is the one that \
counts entities.
- A measure must be safe to re-aggregate at any grain: never assume the query \
groups by a particular dimension unless you emit it yourself.
- A percent-formatted measure returns a fraction — 0.42, not 42.
- No semicolons, no comments, no CTE in front of expr, no DDL, no subquery \
inside expr (a derived relation belongs in from:).

WRITE IT LIKE THE MEASURES ALREADY THERE: same naming, same label style, same \
level of description detail. If the ask is ambiguous between two readings, pick \
the one the existing measures imply and say what you assumed in the rationale.
"""


def _system_prompt() -> str:
    parts = [_SYSTEM_PROMPT_HEAD]
    hint = _aggregate_hint()
    if hint:
        parts.append(
            "\nThe function allowlist is DuckDB's own catalog minus I/O and "
            "nondeterministic functions, so DuckDB's scalar library (date_diff, "
            "date_trunc, regexp_matches, ...) is available. " + hint)
    parts.append(
        f"\nSize limits: expr up to {sqlgrammar.MAX_SQL_LEN} characters, a from: "
        f"block up to {sqlgrammar.MAX_RELATION_LEN}. Anything longer is refused.")
    return "".join(parts)


def _columns_text(ctx: MeasureContext) -> str:
    if ctx.schema_error:
        return ("  (the source could not be scanned right now: "
                f"{ctx.schema_error} — rely on the dimensions and measures below)")
    if not ctx.columns:
        return "  (no columns reported)"
    return "\n".join(f"  {c['name']} {c.get('dtype', '')}".rstrip() for c in ctx.columns)


def _dimensions_text(ctx: MeasureContext) -> str:
    if not ctx.dimensions:
        return "  (none declared)"
    lines = []
    for d in ctx.dimensions:
        line = f"  {d['name']} ({d['type']})"
        if d.get("column") and d["column"] != d["name"]:
            line += f" [column: {d['column']}]"
        if d.get("spine"):
            line += (f" [generated timeline over {d['spine']['start']}..{d['spine']['end']}"
                     " — not a column]")
        if d.get("label"):
            line += f" · {d['label']}"
        if d.get("description"):
            line += f" — {d['description']}"
        if d.get("sample_values"):
            line += " | values: " + ", ".join(str(v) for v in d["sample_values"])
        lines.append(line)
    return "\n".join(lines)


def _measures_text(ctx: MeasureContext) -> str:
    if not ctx.measures:
        return "  (none declared yet — this would be the first)"
    lines = []
    for m in ctx.measures:
        line = f"  {m['name']} ({m.get('format', 'number')})"
        if m.get("inline"):
            line += " [this visual's own]"
        line += f" = {m.get('expr', '')}"
        if m.get("from"):
            block = " ".join(str(m["from"]).split())
            line += f"  |  FROM: {block}"
        if m.get("emits"):
            line += f"  |  EMITS: {', '.join(m['emits'])}"
        if m.get("description"):
            line += f"\n      — {' '.join(str(m['description']).split())}"
        lines.append(line)
    return "\n".join(lines)


def _query_text(ctx: MeasureContext) -> str:
    """The visual the measure is being written into — which is what decides
    whether a window measure can work at all (it needs the time dimension),
    and what the verifying dry run will actually run."""
    q = ctx.query or {}
    dims = []
    for d in q.get("dimensions") or []:
        if isinstance(d, dict):
            dims.append(f"{d.get('name')}" + (f" (grain {d['grain']})" if d.get("grain") else ""))
        else:
            dims.append(str(d))
    lines = [
        "  grouped by: " + (", ".join(dims) if dims else "nothing (a single total)"),
        "  measures shown: " + (", ".join(q.get("measures") or []) or "(none yet)"),
    ]
    if q.get("filters"):
        lines.append("  filters: " + ", ".join(
            f"{f.get('field')} {f.get('op')} {f.get('values', f.get('value'))}"
            for f in q["filters"]))
    if ctx.parameters:
        lines.append("  parameters declared on this visual: " + ", ".join(
            f"{p.get('name')} ({p.get('type', 'int')}; values {p.get('values')})"
            for p in ctx.parameters))
        lines.append(
            "  A parameter is written param('name'). Its one legal position is the "
            "offset argument of LAG()/LEAD() — LAG(revenue, param('periods')) OVER w — "
            "and it must be an int-typed parameter there.")
    return "\n".join(lines)


def _editing_text(ctx: MeasureContext) -> str:
    e = ctx.editing or {}
    lines = [f"  name: {e.get('name', '')}",
             f"  label: {e.get('label', '')}",
             f"  format: {e.get('format', 'number')}",
             f"  expr: {e.get('expr', '')}"]
    if e.get("from"):
        lines.append("  from: |\n" + "\n".join("    " + ln for ln in str(e["from"]).splitlines()))
    if e.get("emits"):
        lines.append(f"  emits: {', '.join(e['emits'])}")
    if e.get("description"):
        lines.append(f"  description: {e['description']}")
    return "\n".join(lines)


def _attempts_text(attempts: list[Attempt]) -> str:
    lines = []
    for i, a in enumerate(attempts, start=1):
        lines.append(f"  attempt {i}:")
        lines.append(f"    expr: {a.measure.expr}")
        if a.measure.from_source:
            lines.append("    from: |\n" + "\n".join(
                "      " + ln for ln in str(a.measure.from_source).splitlines()))
        if a.measure.emits:
            lines.append(f"    emits: {', '.join(a.measure.emits)}")
        lines.append(f"    REJECTED: {a.error}")
    return "\n".join(lines)


def build_user_prompt(req: WriteRequest) -> str:
    ctx = req.context
    scope_line = (
        "This measure is being written ON A VISUAL: it travels with that visual "
        "(and can later be promoted to the model), so it may reference the "
        "visual's declared parameters and its own inline measures."
        if ctx.scope == "visual" else
        "This measure is being written INTO THE MODEL: every visual, dashboard and "
        "chat question against this model will be able to use it, and it must not "
        "reference param() — parameters are visual-scoped and a model measure "
        "carrying one is refused."
    )
    lines = [
        f"Model: {ctx.model}" + (f" ({ctx.label})" if ctx.label else "")
        + (f" — {ctx.description}" if ctx.description else ""),
        f"Fact table: {ctx.dataset}" + (f" · {ctx.source} ({ctx.source_format})" if ctx.source else ""),
        f"\n{scope_line}",
        f"\nSource columns — everything expr: and from: may name:\n{_columns_text(ctx)}",
        f"\nDeclared dimensions — what queries group by, and what {{dims}} carries "
        f"into a from: block:\n{_dimensions_text(ctx)}",
        f"\nMeasures already declared here — match their style; a window expression "
        f"reads these names:\n{_measures_text(ctx)}",
    ]
    if ctx.scope == "visual":
        lines.append(f"\nThe visual this measure is for:\n{_query_text(ctx)}")
    if ctx.editing:
        lines.append(
            "\nRewriting this existing measure (keep its name unless asked to rename, "
            f"and change only what the ask requires):\n{_editing_text(ctx)}")
    if req.history:
        lines.append("\nEarlier asks this session:")
        for h in req.history:
            lines.append(f"  - {h.get('instruction', '')!r} -> {h.get('summary', '')}")
    if req.attempts:
        lines.append(
            "\nYOUR EARLIER ATTEMPTS THIS TURN WERE REJECTED by the validator that "
            "compiles and runs the measure. Read each error, work out what actually "
            "caused it, and fix that — do not resend the same expression, and do not "
            "retreat to something trivially different that doesn't answer the ask:\n"
            + _attempts_text(req.attempts))
    lines.append(f"\nWrite this measure: {req.instruction.strip()}")
    return "\n".join(lines)


# ── the client ──────────────────────────────────────────────────────────────

def _proposal_from_args(name: str, args: dict) -> RawProposal:
    args = args or {}
    if name == "decline":
        return RawDecline(reason=str(args.get("reason") or "no reason given"))
    emits = args.get("emits") or []
    synonyms = args.get("synonyms") or []
    from_source = args.get("from")
    return RawMeasure(
        name=str(args.get("name") or "").strip(),
        expr=str(args.get("expr") or "").strip(),
        label=str(args.get("label") or "").strip(),
        format=str(args.get("format") or "number").strip(),
        description=str(args.get("description") or "").strip(),
        from_source=str(from_source).strip() if isinstance(from_source, str) and from_source.strip() else None,
        emits=[str(e) for e in emits if isinstance(e, (str, int))],
        synonyms=[str(s) for s in synonyms if isinstance(s, str)],
        rationale=str(args.get("rationale") or "").strip(),
    )


class LLMMeasureWriter:
    """Forced tool use against the configured provider, so the result is always
    one typed proposal. Provider-neutral: app/llmclient.py owns which API this
    reaches and how partial arguments arrive as they are written."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, client=None):
        self.api_key = api_key or config.LLM_API_KEY
        self.model = model or config.LLM_MODEL
        self.client = client or llmclient.build_client(api_key=self.api_key)

    def _request(self, request: WriteRequest) -> llmclient.ChatRequest:
        return llmclient.ChatRequest(
            model=self.model,
            max_tokens=4096,
            system=_system_prompt(),
            tools=_TOOLS,
            prompt=build_user_prompt(request),
            # a complex measure is a genuine reasoning problem — thinking is on
            # unless the caller turned it off, same as the composer
            thinking=config.LLM_THINKING_DEFAULT if request.thinking is None else request.thinking,
        )

    def write_streaming(self, request: WriteRequest) -> Iterator[WriteStreamEvent]:
        try:
            for event in self.client.stream(self._request(request)):
                if event.kind == "thinking":
                    yield WriteStreamEvent(kind="thinking", text=event.text)
                elif event.kind == "tool_input":
                    if isinstance(event.tool_input, dict):
                        yield WriteStreamEvent(kind="draft", draft=dict(event.tool_input))
                elif event.kind == "done":
                    yield WriteStreamEvent(
                        kind="done",
                        final=_proposal_from_args(event.final.name, event.final.args))
                    return
        except llmclient.LLMError as exc:
            raise WriterError(str(exc)) from exc
        raise WriterError("model did not propose a measure")


# The pre-multi-provider naming convention used by the other seams.
AnthropicMeasureWriter = LLMMeasureWriter


# ── verification: the same checks the save path runs, then a real query ─────

@dataclass(frozen=True)
class Verdict:
    """What happened when a proposal was checked. `ran` distinguishes "this
    measure executed against the live source and returned rows" from "it
    compiles, but the source couldn't be reached to prove it" — the caller
    reports which, rather than implying a check that never happened."""
    ok: bool
    error: str = ""
    ran: bool = False
    note: str = ""
    preview: Optional[dict] = None      # {value, rows, elapsed_ms}


def _expr_schema(ctx: MeasureContext, raw: RawMeasure, is_window: bool) -> Optional[dict]:
    """What names `expr` may use: sibling measures for a window expression,
    the fact scan's source columns otherwise — deliberately the same, stricter
    rule app/api/models.py's _validate_measure_body applies, so a measure that
    verifies here is one that can also be *saved* to the model (the engine
    would additionally accept the query's dimension names at run time, but a
    measure built on that could never be promoted)."""
    if is_window:
        return dict.fromkeys(
            (m["name"] for m in ctx.measures
             if m["name"] != raw.name and (ctx.scope == "visual" or not m.get("inline"))), "")
    if ctx.schema_error:
        return None        # unknown columns can't be checked; the grammar still is
    return {c["name"]: c.get("dtype", "") for c in ctx.columns}


def _parameter_error(ctx: MeasureContext, raw: RawMeasure) -> Optional[str]:
    """Parameter rules, per scope — the same ones app/api/models.py (a model
    measure may not reference one at all) and app/api/visuals.py (declared, and
    int-typed in LAG's offset position) enforce at save time."""
    texts = [raw.expr] + ([raw.from_source] if raw.from_source else [])
    referenced: set = set()
    for text in texts:
        referenced |= sqlgrammar.referenced_parameter_names(text)
    if not referenced:
        return None
    if ctx.scope != "visual":
        return ("this measure references param(" + ", ".join(sorted(referenced))
                + ") — parameters are visual-scoped and a model measure carrying one "
                  "cannot be saved; write it without a parameter")
    declared = {p.get("name"): (p.get("type") or "int") for p in ctx.parameters if p.get("name")}
    unknown = sorted(referenced - set(declared))
    if unknown:
        return (f"references parameter(s) {unknown} that this visual has not declared "
                f"(declared: {sorted(declared) or 'none'})")
    for name in sorted(sqlgrammar.lag_period_param_names(raw.expr)):
        if declared.get(name) != "int":
            return (f"LAG()/LEAD()'s offset argument references parameter '{name}' "
                    f"(type '{declared.get(name)}'), which must be int")
    return None


def check(ctx: MeasureContext, raw: RawMeasure) -> Optional[str]:
    """Static re-validation of an unvalidated proposal: the error text to send
    back to the model, or None if it passed.

    Everything here is a rule some save path already enforces — name shape and
    uniqueness, the format vocabulary, `emits` needing a `from`, the parameter
    rules, and finally the grammar itself through the identical
    sqlgrammar/semantic entry points a hand-typed measure goes through. The
    messages are written to be *repairable*: they name the rule and the fix,
    because their first reader is the model that has to try again."""
    if not raw.name:
        return "the measure has no name"
    if not MEASURE_NAME.match(raw.name):
        return f"'{raw.name}' is not a valid name — use snake_case (a-z, 0-9, _)"
    if raw.name in ctx.taken_names:
        return (f"'{raw.name}' is already the name of a dimension or measure on this "
                "model — choose another")
    if not raw.expr:
        return "the measure has no expression"
    if raw.format not in FORMATS:
        return f"format '{raw.format}' is not one of {', '.join(FORMATS)}"
    if raw.emits and not raw.from_source:
        return "'emits' only means something with a 'from' block"
    declared_dims = {d["name"] for d in ctx.dimensions}
    unknown_emits = [e for e in raw.emits if e not in declared_dims]
    if unknown_emits:
        return (f"emits names {unknown_emits}, which are not declared dimensions of this "
                f"fact table (declared: {', '.join(sorted(declared_dims)) or 'none'}) — "
                "emit a dimension that exists, or drop it")

    param_error = _parameter_error(ctx, raw)
    if param_error:
        return param_error
    try:
        # each parameter at its default, since a draft has no current selection
        parameter_values = engine.resolve_parameter_values(ctx.parameters, {}) if ctx.parameters else {}
    except engine.QueryError:
        # a malformed parameter *declaration* is the visual's problem, and
        # rejecting the measure for it would send the model chasing a fault it
        # can't fix; the measure is checked without substitutions instead
        parameter_values = {}

    try:
        if raw.from_source:
            semantic.validate_from_block(raw.from_source, f"measure '{raw.name}'")
            # the block's output columns are only known from a live scan, so the
            # aggregate itself is checked for shape here and against the real
            # derived relation by the dry run below
            sqlgrammar.compile_expression(raw.expr, None, parameter_values=parameter_values)
            return None
        is_window = sqlgrammar.is_window_expr(raw.expr)
        if is_window and not any(d["type"] == "time" for d in ctx.dimensions):
            return ("this is a window expression (it reads across already-aggregated "
                    "rows), but the fact table declares no time dimension for the engine "
                    "to order the window by — write it as a plain aggregate instead")
        sqlgrammar.compile_expression(
            raw.expr, _expr_schema(ctx, raw, is_window),
            window=is_window, parameter_values=parameter_values)
    except (semantic.ModelError, sqlgrammar.SqlCompileError, engine.QueryError) as exc:
        return str(exc)
    return None


def dry_run_query(ctx: MeasureContext, raw: RawMeasure) -> dict:
    """The smallest query that proves this measure runs.

    Shaped from the context rather than invented: in the visual scope it is the
    query the author is actually looking at (their dimensions, grain and
    filters, plus their other inline measures, since a window measure reads
    them), which makes a pass here mean "this works where you're putting it".
    Two shapes are added on top, because they are what the measure itself
    requires: a window measure gets a time dimension if the query has none (the
    engine has nothing to order `w` by otherwise), and an emitted dimension is
    grouped on, since emitting is exactly the thing that would otherwise go
    unexercised."""
    query = dict(ctx.query or {})
    dimensions = list(query.get("dimensions") or [])

    def _name(d):
        return d.get("name") if isinstance(d, dict) else d

    names = {_name(d) for d in dimensions}
    is_window = not raw.from_source and sqlgrammar.is_window_expr(raw.expr)
    if is_window and not any(
            any(dim["name"] == _name(d) and dim["type"] == "time" for dim in ctx.dimensions)
            for d in dimensions):
        time_dim = next((d for d in ctx.dimensions if d["type"] == "time" and not d.get("spine")),
                        next((d for d in ctx.dimensions if d["type"] == "time"), None))
        if time_dim:
            dimensions.append({"name": time_dim["name"], "grain": "1mo"})
            names.add(time_dim["name"])
    for emitted in raw.emits:
        if emitted not in names:
            dimensions.append({"name": emitted, "grain": "1mo"})
            names.add(emitted)

    if raw.from_source and not [d for d in dimensions if _name(d) not in raw.emits]:
        # A from: block is only really tested against a query that groups by
        # something: {dims} expands to the engine's constant column when a
        # query groups by nothing, so a block that forgot to carry the
        # dimensions through — the single most common way a complex measure
        # breaks — would sail through a dimensionless check and fail the first
        # time anyone grouped the chart. Group by one, so it can't.
        carrier = next((d for d in ctx.dimensions
                        if d["type"] == "categorical" and not d.get("spine")
                        and d["name"] not in raw.emits), None)
        carrier = carrier or next((d for d in ctx.dimensions
                                   if not d.get("spine") and d["name"] not in raw.emits), None)
        if carrier:
            entry = {"name": carrier["name"]}
            if carrier["type"] == "time":
                entry["grain"] = "1mo"
            dimensions.append(entry)

    inline = [m for m in ctx.inline_measures if m.get("name") != raw.name]
    return {
        "dimensions": dimensions,
        "measures": [raw.name],
        "inline_measures": [*inline, raw.to_query_dict()],
        "filters": list(query.get("filters") or []),
        "parameters": list(ctx.parameters),
        "parameter_values": {},
        "limit": DRY_RUN_LIMIT,
    }


def verify(model: semantic.Model, ctx: MeasureContext, raw: RawMeasure) -> Verdict:
    """check(), then actually run the thing.

    The dry run is what catches everything static checking structurally cannot:
    a column that exists in the block's SELECT but not in the source, a `from:`
    block that drops one of the query's dimensions, an emitted column the block
    never outputs, a type error inside an aggregate. Those come back as
    engine.QueryError with messages written for authors, which is exactly what
    the repair turn needs.

    A failure to reach the source is deliberately NOT a rejection: the measure
    compiled, so it is returned with `ran=False` and a note saying it could not
    be executed — a bucket being unreachable must not look like a bad measure.
    """
    error = check(ctx, raw)
    if error:
        return Verdict(ok=False, error=error)
    if ctx.schema_error:
        return Verdict(ok=True, ran=False, note=ctx.schema_error)
    try:
        fact = target_part(model, ctx.dataset)
    except ValueError as exc:
        return Verdict(ok=True, ran=False, note=str(exc))
    try:
        result = engine.run_query(fact, dry_run_query(ctx, raw))
    except (engine.QueryError, semantic.ModelError) as exc:
        return Verdict(ok=False, error=str(exc), ran=True)
    except Exception as exc:        # infrastructure, not the measure
        return Verdict(ok=True, ran=False, note=f"could not run a check query: {exc}")
    rows = result.get("rows") or []
    preview = {"value": rows[0].get(raw.name) if rows else None,
               "rows": result.get("row_count", len(rows)),
               "elapsed_ms": result.get("elapsed_ms")}
    return Verdict(ok=True, ran=True, preview=preview)


# ── propose -> verify -> repair ─────────────────────────────────────────────

@dataclass(frozen=True)
class Outcome:
    """The end of one authoring turn. `measure` is set only for "written", and
    only ever a proposal that passed verify()."""
    status: Literal["written", "declined", "failed"]
    measure: Optional[RawMeasure] = None
    verdict: Optional[Verdict] = None
    reason: str = ""                                    # declined
    error: str = ""                                     # failed
    attempts: list[Attempt] = field(default_factory=list)


@dataclass(frozen=True)
class LoopEvent:
    """One step of the loop, for a caller streaming progress. "verifying" and
    "rejected" are what make the repair visible instead of looking like a
    stall — a complex measure that takes two tries should say so."""
    kind: Literal["thinking", "draft", "verifying", "rejected", "outcome"]
    text: str = ""
    draft: Optional[dict] = None
    attempt: int = 1
    error: str = ""
    outcome: Optional[Outcome] = None


def run_streaming(writer: Writer, model: semantic.Model, request: WriteRequest,
                  *, max_attempts: int = MAX_ATTEMPTS) -> Iterator[LoopEvent]:
    """Ask, verify, and — while it still fails — ask again with the error.

    Every proposal is re-verified against the *live* model handed in here, never
    against the context snapshot the writer was shown, which can be stale by the
    time the answer arrives (the same rule nlq.resolve() follows). The loop ends
    the moment a proposal verifies, the model declines, or the attempts run out;
    a failed turn still carries every attempt and its error, so the author sees
    what was tried rather than a bare "couldn't do it".
    """
    attempts: list[Attempt] = []
    for attempt in range(1, max_attempts + 1):
        proposal: Optional[RawProposal] = None
        for event in writer.write_streaming(
                WriteRequest(instruction=request.instruction, context=request.context,
                             history=request.history, attempts=attempts,
                             thinking=request.thinking)):
            if event.kind == "thinking":
                yield LoopEvent(kind="thinking", text=event.text, attempt=attempt)
            elif event.kind == "draft":
                yield LoopEvent(kind="draft", draft=event.draft, attempt=attempt)
            elif event.kind == "done":
                proposal = event.final
        if proposal is None:
            raise WriterError("model did not propose a measure")
        if isinstance(proposal, RawDecline):
            yield LoopEvent(kind="outcome", attempt=attempt,
                            outcome=Outcome(status="declined", reason=proposal.reason,
                                            attempts=attempts))
            return

        yield LoopEvent(kind="verifying", attempt=attempt, draft=proposal.to_dict())
        verdict = verify(model, request.context, proposal)
        if verdict.ok:
            yield LoopEvent(kind="outcome", attempt=attempt,
                            outcome=Outcome(status="written", measure=proposal,
                                            verdict=verdict, attempts=attempts))
            return
        attempts = [*attempts, Attempt(measure=proposal, error=verdict.error)]
        yield LoopEvent(kind="rejected", attempt=attempt, error=verdict.error,
                        draft=proposal.to_dict())

    last = attempts[-1]
    yield LoopEvent(kind="outcome", attempt=max_attempts,
                    outcome=Outcome(status="failed", measure=last.measure,
                                    error=last.error, attempts=attempts))


def run(writer: Writer, model: semantic.Model, request: WriteRequest,
        *, max_attempts: int = MAX_ATTEMPTS) -> Outcome:
    """run_streaming() without the progress — one verified measure, a decline,
    or a failure carrying every attempt."""
    outcome = None
    for event in run_streaming(writer, model, request, max_attempts=max_attempts):
        if event.kind == "outcome":
            outcome = event.outcome
    if outcome is None:             # unreachable: the loop always ends in an outcome
        raise WriterError("the measure writer produced no outcome")
    return outcome
