"""The sandbox module's coding agent — the one seam that asks an LLM to write
polars for the open notebook, and to describe a converted pipeline's lineage.
Sibling of app/llm.py (the conversational-analytics seam) and deliberately the
same shape: everything above this module only ever sees a typed, *unvalidated*
`RawAgentCall`, re-checked by app/sandbox.py (validate_agent_cells /
validate_lineage) before it can reach the notebook or a pipeline yaml.

Nothing here executes anything. A proposal is text the admin applies (or
doesn't) into cells they then run themselves — the sandbox's existing
admin-gated `read`/exec path (app/sandbox_runner.py) is unchanged, and no new
eval-capable construct is introduced (constitution Principle VI).

**Data egress**: a request sends the notebook's cell sources, its last run's
stdout/error tails and result *schemas* (column names + dtypes — never result
rows), and the bucket's object paths to the configured LLM provider. Off
entirely unless CI_LLM_API_KEY is set, exactly like conversational analytics.

Cost/latency posture (this is a sandbox, where re-running a cell is the
cheap, fast feedback channel — so the agent buys none of the usual agentic
safety net):

- **One model call per request.** No tool-result loop, no self-critique pass,
  no "now verify it" round trip. The human runs the cell; a failure comes
  back as context on the next turn (a cell's error tail is part of the
  notebook context below).
- **No tests, no benchmarks, no defensive scaffolding** — stated as a hard
  rule in the system prompt, because that is where most of the tokens (and
  most of the latency) in a coding agent's answer normally go.
- **No extended thinking.** Adaptive thinking is what app/llm.py's chat path
  wants; here it is pure added latency on a task the model can answer in one
  shot.
- **Cached system prompt.** The polars doctrine below is long, static, and
  resent on every turn — it goes out as an explicit `cache_control` block so
  repeat turns pay the cache-read price for it, not the input price.
- **Bounded context.** Cell sources, output tails and the bucket listing are
  all truncated (config.SANDBOX_AGENT_*); result rows are never sent.
- **Constrained decisions.** Forced tool use with the target-cell enum built
  from the live notebook (_tools_for_notebook), so the model picks a real
  cell id instead of narrating one — same defense-in-depth reasoning as
  app/llm.py's _tools_for_catalog.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator, Literal, Protocol

from . import config
from .sandbox import NEW_CELL   # the tool schema and its validation share one constant

logger = logging.getLogger(__name__)

AgentToolKind = Literal["write_cells", "answer"]


class AgentError(Exception):
    """The LLM call itself failed (network/timeout/API error) — distinct from
    the model proposing bad *code*, which app/sandbox.py's validation and,
    ultimately, running the cell are what catch."""


@dataclass(frozen=True)
class CellContext:
    """One notebook cell as the agent sees it: its source plus whatever the
    last run left behind. `columns` is the displayed frame's schema only —
    result rows never leave the deployment (see the module docstring)."""
    id: str
    source: str
    status: str = "not run"            # "ok" | "error" | "not run"
    stdout: str = ""
    error: str = ""
    columns: list[dict] = field(default_factory=list)   # [{name, dtype}]
    row_count: int | None = None
    truncated: bool = False


@dataclass(frozen=True)
class NotebookContext:
    """The live (possibly unsaved) notebook the request is about, plus the
    bucket paths a `read(...)` call could name."""
    name: str
    cells: list[CellContext] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)     # [{path, format, size}]
    bucket: str = ""


@dataclass(frozen=True)
class AgentTurn:
    """A prior request/reply pair, for follow-ups ("now do the same for
    shipments"). Only the *text* of a past reply is kept — proposed code
    isn't resent, because code the admin applied is already in the notebook
    context, and code they didn't apply was code they rejected."""
    request: str
    reply: str


@dataclass(frozen=True)
class LineageContext:
    """Everything the lineage helper needs about a converted pipeline: the
    combined script (already source-rewritten to `sources["name"]`), the
    detected sources, and — when the notebook has been run — the output
    frame's column names, which are the field names lineage is declared per."""
    pipeline_name: str
    script: str
    sources: list[dict] = field(default_factory=list)        # [{name, path, format}]
    output_columns: list[dict] = field(default_factory=list)  # [{name, dtype}]


@dataclass(frozen=True)
class RawAgentCall:
    """The LLM's unvalidated tool call: which tool, and that tool's raw args."""
    kind: AgentToolKind
    args: dict


@dataclass(frozen=True)
class AgentStreamEvent:
    """One incremental update from assist_streaming(). Everything but "done"
    is display-only — the caller re-validates only the final RawAgentCall,
    identical to a non-streamed call, so streaming can never change what is
    trusted (same contract as app/llm.py's StreamEvent)."""
    kind: Literal["tool_name", "tool_input", "done"]
    tool_name: str | None = None
    tool_input: dict | None = None
    final: RawAgentCall | None = None


class CodingAgent(Protocol):
    def assist_streaming(
        self, request: str, notebook: NotebookContext, history: list[AgentTurn],
    ) -> Iterator[AgentStreamEvent]: ...

    def describe_lineage(self, context: LineageContext) -> dict: ...


# ── tools ─────────────────────────────────────────────────────────────────

_ASSIST_TOOLS = [
    {
        "name": "write_cells",
        "eager_input_streaming": True,
        "description": (
            "Write or rewrite notebook cells. Return ONLY the cells that "
            "change — never restate an untouched cell."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cells": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": "string",
                                "description": (
                                    "the id of the existing cell this replaces, or "
                                    f"'{NEW_CELL}' to append a new cell at the end."
                                ),
                            },
                            "source": {
                                "type": "string",
                                "description": "the complete new source of that cell.",
                            },
                        },
                        "required": ["target", "source"],
                    },
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "at most two short sentences on what changed and why. No "
                        "restating of the code, no usage instructions, no caveats."
                    ),
                },
            },
            "required": ["cells"],
        },
    },
    {
        "name": "answer",
        "eager_input_streaming": True,
        "description": (
            "Answer in prose, for a question that needs no code change "
            "(what a plan does, why a query is slow, what a dtype means)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]

_LINEAGE_TOOL = {
    "name": "describe_lineage",
    "eager_input_streaming": True,
    "description": (
        "Describe a pipeline's field-level lineage: for each column the "
        "script's `output` produces, which declared source columns it comes "
        "from and how it is derived."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "one sentence describing what this pipeline produces.",
            },
            "lineage": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": "an output column name, exactly as the script produces it.",
                        },
                        "from": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "the source columns it derives from, each written "
                                "'<source_name>.<column>' using a DECLARED source name. "
                                "Empty only for a genuine constant/literal column."
                            ),
                        },
                        "transform": {
                            "type": "string",
                            "description": (
                                "a short plain-English derivation, e.g. 'pass-through' or "
                                "'unit_price × quantity per order line'. One line, no code."
                            ),
                        },
                    },
                    "required": ["field", "from"],
                },
            },
        },
        "required": ["lineage"],
    },
}


def _tools_for_notebook(notebook: NotebookContext) -> list[dict]:
    """_ASSIST_TOOLS with write_cells' `target` constrained to this
    notebook's real cell ids (plus NEW_CELL) — the same defense-in-depth
    reasoning as app/llm.py's _tools_for_catalog: an unconstrained string
    invites an invented id that validation would only throw away later."""
    ids = [c.id for c in notebook.cells]
    if not ids:
        return _ASSIST_TOOLS
    tools = []
    for tool in _ASSIST_TOOLS:
        if tool["name"] != "write_cells":
            tools.append(tool)
            continue
        schema = tool["input_schema"]
        item = schema["properties"]["cells"]["items"]
        tools.append({
            **tool,
            "input_schema": {
                **schema,
                "properties": {
                    **schema["properties"],
                    "cells": {
                        **schema["properties"]["cells"],
                        "items": {
                            **item,
                            "properties": {
                                **item["properties"],
                                "target": {
                                    **item["properties"]["target"],
                                    "enum": [*ids, NEW_CELL],
                                },
                            },
                        },
                    },
                },
            },
        })
    return tools


# ── prompts ───────────────────────────────────────────────────────────────

# The performance doctrine is the point of this agent: polars written badly
# (eager collects, python-level row loops, per-column with_columns chains)
# is the single easiest way to lose the lazy-scan property this whole
# platform exists for (constitution Principle II).
_ASSIST_SYSTEM_PROMPT = (
    "You are a polars expert writing cells for an admin's sandbox notebook in "
    "a lazy BI platform. You write the fastest correct polars for the job, and "
    "nothing else.\n\n"

    "NOTEBOOK RUNTIME (this is the whole namespace — nothing else is "
    "importable or available):\n"
    "- `pl` — polars.\n"
    "- `read(path, format=None)` — the ONLY way to reach the bucket. Returns a "
    "LazyFrame. Format is inferred from the path (.csv -> csv, .parquet -> "
    "parquet, anything else -> delta); an Iceberg table needs an explicit "
    "read(path, format=\"iceberg\"). Never call pl.scan_parquet/scan_csv/"
    "scan_delta yourself — they would have no storage credentials.\n"
    "- `bucket` — the bucket name string.\n"
    "- Cells share one namespace and run top to bottom; a later cell sees an "
    "earlier cell's variables. There is no kernel between runs: a run replays "
    "every cell from the top, so keep expensive scans lazy.\n"
    "- A cell's last bare expression is auto-displayed, Jupyter style. End a "
    "cell with the frame you want to see; a LazyFrame is collected to a "
    "capped preview automatically, so `lf` alone is a fine last line — you do "
    "not need `.collect()` to look at something.\n\n"

    "PERFORMANCE RULES (in priority order):\n"
    "1. Stay lazy end to end. read() gives a LazyFrame; keep chaining on it "
    "and collect at most once, at the very end. Never collect an intermediate "
    "just to inspect it or to feed the next step.\n"
    "2. Filter and select as early as possible, directly on the scan, so "
    "predicate/projection pushdown drops row groups and columns before they "
    "leave the bucket. Filtering after a join or an aggregation when it could "
    "have been done before it is a bug, not a style choice.\n"
    "3. Expressions, never Python. No iter_rows, no map_elements/map_rows/"
    "apply, no lambdas over data, no Python loops over columns, no pandas. "
    "Use pl.when/then/otherwise, .over(...) window expressions, group_by(...)"
    ".agg(...), and the .str/.dt/.list/.struct namespaces.\n"
    "4. Batch expressions: one .with_columns(...)/.select(...) taking many "
    "expressions runs them in parallel; a chain of single-column calls does "
    "not. Use pl.col(a, b, c) / selectors (import as `cs` is NOT available — "
    "use pl.col with multiple names or a regex '^prefix_.*$') to hit groups "
    "of columns at once.\n"
    "5. Joins: join on the smallest frame you can, after filtering both "
    "sides; use how='semi'/'anti' when you only need filtering, not columns; "
    "join_asof for nearest-time alignment; and cast join keys to matching "
    "dtypes once rather than per join.\n"
    "6. Aggregate with group_by().agg([...]) and pl.len() for counts. Prefer "
    "a single group_by producing several aggregates over several passes.\n"
    "7. Big data: .collect(engine=\"streaming\") for larger-than-memory work, "
    ".head()/.limit() for previews, and .explain() when the question is "
    "whether pushdown is actually happening. Cast repeated string keys to "
    "pl.Categorical/pl.Enum when they drive group_by or joins.\n"
    "8. Use current polars API: group_by (not groupby), pl.len() (not "
    "pl.count()), .rename/.alias explicitly. No deprecated spellings.\n\n"

    "WHAT NOT TO WRITE (this is a sandbox — the human runs the cell and "
    "pastes the error back, which is faster than anything you could do):\n"
    "- No tests, no assertions-as-tests, no benchmarks, no timing harnesses.\n"
    "- No try/except around data code, no defensive existence checks, no "
    "logging, no print() unless the human asked to see something specific.\n"
    "- No `if __name__ == \"__main__\"`, no functions wrapping a one-liner, no "
    "config scaffolding.\n"
    "- Comments only where the reason is non-obvious. No comment that "
    "restates the line below it.\n\n"

    "REPLY RULES:\n"
    "- Call write_cells when code changes, `answer` when it does not. Exactly "
    "one tool call, always.\n"
    "- Return only the cells that change. Replacing a cell means giving its "
    "complete new source; use target '" + NEW_CELL + "' to append.\n"
    "- Notes are at most two short sentences. Don't restate the code, don't "
    "list caveats, don't offer next steps unless asked.\n"
    "- If a cell in the context failed, fix the actual cause shown in its "
    "error — do not paper over it with a try/except or a fallback path.\n"
    "- If the notebook is destined for a pipeline (the human will say so, or "
    "the notebook will already assign it), the final frame must be assigned "
    "to `output` — that is the pipeline script contract."
)

_LINEAGE_SYSTEM_PROMPT = (
    "You document field-level lineage for a polars pipeline script that was "
    "just converted from a sandbox notebook. You are given the script, its "
    "declared sources, and (when the notebook was run) the exact columns its "
    "output produces.\n\n"
    "Call describe_lineage exactly once.\n"
    "- One entry per output column, using the output column names given. If "
    "no output columns are given, infer them from the script's final "
    "`output` expression — only columns it demonstrably produces.\n"
    "- `from` lists the source columns the field derives from, each written "
    "'<source_name>.<column>' where <source_name> is one of the DECLARED "
    "source names, never a path, a variable name, or an intermediate frame. "
    "Trace through intermediate variables to the source a column actually "
    "came from. Use an empty list only for a genuine literal/constant.\n"
    "- `transform` is one short plain-English line: 'pass-through' for an "
    "unchanged column, otherwise the derivation in business terms (e.g. "
    "'unit_price × quantity per order line', 'summed per customer per "
    "month'). Never restate the code, never exceed one line.\n"
    "- Do not invent a column that the script does not produce, and do not "
    "guess a source column that does not appear in the script. Omit an entry "
    "you cannot ground in the script rather than fabricating its origin.\n"
    "- `description` is one sentence on what the pipeline produces."
)


def _truncate(text: str, limit: int, *, tail: bool = False) -> str:
    """Trim to `limit` characters, from the end when `tail` (a traceback's
    last lines are the ones that say what actually broke)."""
    if not text or len(text) <= limit:
        return text or ""
    return "…(truncated)…\n" + text[-limit:] if tail else text[:limit] + "\n…(truncated)…"


def _cell_text(index: int, cell: CellContext) -> str:
    lines = [f"### cell {index + 1} (id: {cell.id}) — last run: {cell.status}"]
    lines.append("```python")
    lines.append(_truncate(cell.source, config.SANDBOX_AGENT_CELL_CHARS))
    lines.append("```")
    if cell.stdout:
        lines.append("stdout: " + _truncate(cell.stdout, config.SANDBOX_AGENT_OUTPUT_CHARS, tail=True))
    if cell.error:
        lines.append("error: " + _truncate(cell.error, config.SANDBOX_AGENT_OUTPUT_CHARS, tail=True))
    if cell.columns:
        schema = ", ".join(f"{c.get('name')} {c.get('dtype')}" for c in cell.columns)
        rows = "unknown (preview truncated)" if cell.truncated else cell.row_count
        lines.append(f"result schema: {schema} | rows: {rows}")
    return "\n".join(lines)


def _notebook_text(notebook: NotebookContext) -> str:
    if not notebook.cells:
        return "(the notebook is empty)"
    return "\n\n".join(_cell_text(i, c) for i, c in enumerate(notebook.cells))


def _files_text(notebook: NotebookContext) -> str:
    """Bucket paths a read() call could name — the agent's only way to know
    what data exists. Capped: a big bucket's full listing is neither
    affordable nor useful."""
    if not notebook.files:
        return "(no bucket listing available)"
    shown = notebook.files[: config.SANDBOX_AGENT_FILES]
    lines = [f"- {f.get('path')} ({f.get('format')})" for f in shown]
    if len(notebook.files) > len(shown):
        lines.append(f"- …and {len(notebook.files) - len(shown)} more (listing truncated)")
    return "\n".join(lines)


def _history_text(history: list[AgentTurn]) -> str:
    if not history:
        return "(no earlier requests in this session)"
    turns = history[-config.SANDBOX_AGENT_HISTORY_TURNS:]
    return "\n".join(f"- asked: {t.request!r} -> replied: {t.reply}" for t in turns)


def build_assist_prompt(request: str, notebook: NotebookContext, history: list[AgentTurn]) -> str:
    return (
        f"Notebook: {notebook.name or '(unnamed)'} (bucket: {notebook.bucket or 'unknown'})\n\n"
        f"Cells:\n{_notebook_text(notebook)}\n\n"
        f"Bucket paths available to read():\n{_files_text(notebook)}\n\n"
        f"Earlier in this session:\n{_history_text(history)}\n\n"
        f"Request: {request}"
    )


def build_lineage_prompt(context: LineageContext) -> str:
    sources = "\n".join(
        f"- {s.get('name')} ({s.get('format')}) <- {s.get('path')}" for s in context.sources
    ) or "(no sources detected)"
    columns = ", ".join(
        f"{c.get('name')} {c.get('dtype')}" for c in context.output_columns
    ) or "(the notebook has not been run — infer them from the script)"
    return (
        f"Pipeline: {context.pipeline_name}\n\n"
        f"Declared sources (use these names in `from`):\n{sources}\n\n"
        f"Output columns:\n{columns}\n\n"
        f"Script:\n```python\n{_truncate(context.script, config.SANDBOX_AGENT_CELL_CHARS * 2)}\n```"
    )


def _system_blocks(text: str) -> list[dict]:
    """The system prompt as one explicitly cached block: it is long, static
    and resent on every turn, so a cache breakpoint here is the single
    biggest cost lever this feature has (see the module docstring)."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _log_and_wrap(exc: Exception) -> AgentError:
    """Same shared failure path as app/llm.py's: the user sees a generic
    message, the real cause is logged server-side so a bad key / network /
    proxy issue is actually diagnosable."""
    logger.warning("sandbox agent API call failed: %r (cause: %r)", exc, exc.__cause__)
    return AgentError(str(exc))


class AnthropicSandboxAgent:
    """Talks to the Anthropic Messages API with forced tool use, one call per
    request, no extended thinking (see the module docstring for why each of
    those is a cost/latency decision rather than an oversight)."""

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 lineage_model: str | None = None):
        self.api_key = api_key or config.LLM_API_KEY
        self.model = model or config.SANDBOX_AGENT_MODEL
        self.lineage_model = lineage_model or config.SANDBOX_LINEAGE_MODEL

    def _client(self):
        import anthropic

        return anthropic.Anthropic(api_key=self.api_key)

    def assist_streaming(
        self, request: str, notebook: NotebookContext, history: list[AgentTurn],
    ) -> Iterator[AgentStreamEvent]:
        """Streams the proposal as it is written — with eager_input_streaming
        on every tool, `event.snapshot` is already a parsed partial dict, so
        the caller can show code appearing live instead of a spinner. The
        stream is display-only; only the final "done" event's RawAgentCall is
        acted on, and even that is re-validated (app/sandbox.py)."""
        import anthropic

        try:
            with self._client().messages.stream(
                model=self.model,
                max_tokens=config.SANDBOX_AGENT_MAX_TOKENS,
                system=_system_blocks(_ASSIST_SYSTEM_PROMPT),
                tools=_tools_for_notebook(notebook),
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": build_assist_prompt(request, notebook, history)}],
            ) as stream:
                for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield AgentStreamEvent(kind="tool_name", tool_name=event.content_block.name)
                    elif event.type == "input_json":
                        snapshot = event.snapshot if isinstance(event.snapshot, dict) else {}
                        yield AgentStreamEvent(kind="tool_input", tool_input=snapshot)
                message = stream.get_final_message()
        except anthropic.APIError as exc:
            raise _log_and_wrap(exc) from exc

        for block in message.content:
            if block.type == "tool_use":
                yield AgentStreamEvent(kind="done", final=RawAgentCall(kind=block.name, args=block.input))
                return
        raise AgentError("model did not call any tool")

    def describe_lineage(self, context: LineageContext) -> dict:
        """One non-streamed call (the caller is a single POST that has
        nothing to show incrementally), on the cheaper lineage model. Returns
        the tool's raw args — unvalidated, exactly like assist_streaming's
        final call."""
        import anthropic

        try:
            response = self._client().messages.create(
                model=self.lineage_model,
                max_tokens=config.SANDBOX_LINEAGE_MAX_TOKENS,
                system=_system_blocks(_LINEAGE_SYSTEM_PROMPT),
                tools=[_LINEAGE_TOOL],
                tool_choice={"type": "tool", "name": _LINEAGE_TOOL["name"]},
                messages=[{"role": "user", "content": build_lineage_prompt(context)}],
            )
        except anthropic.APIError as exc:
            raise _log_and_wrap(exc) from exc

        for block in response.content:
            if block.type == "tool_use":
                return block.input
        raise AgentError("model did not call any tool")
