# Sandbox

**Source:** `app/sandbox.py` (445 lines) · `app/sandbox_runner.py`
(131 lines) · `app/sandbox_agent.py` (524 lines) · `app/sandboxstore.py`
(80 lines)

The Sandbox is a multi-cell SQL scratch notebook over the same bucket
pipelines read from — the place to explore a dataset or prototype a
transformation *before* it's worth saving as a pipeline. It shares
pipelines' trust boundary (admin-gated, real bucket I/O reach) but not
their execution model (synchronous, unqueued, since a sandbox run never
writes anything).

## Execution model

Cells run **top-to-bottom in one DuckDB session** per run: a
`CREATE OR REPLACE TEMP VIEW` in an early cell is visible to every later
one, exactly like a real notebook kernel. There is **no persistent session
between separate runs** — clicking RUN on cell *n* replays cells
`0..n` from scratch every time. That trades a bit of redundant
recomputation for never having stale/drifted state to reason about, which
matters more in a tool whose whole point is fast iterative exploration.

```
app/api/sandbox.py: POST /sandbox/run
    │  subprocess.Popen(["python", "-m", "app.sandbox_runner"], ...)
    │  stdin: {cells, run_upto, bucket, row_limit}, timeout = min(requested, SANDBOX_TIMEOUT_MAX)
    ▼
sandbox_runner.run_job()
    │  one duck.cursor(), shared across every cell up to run_upto
    │  each cell: parse_statements() (app/pipelines.py's tokenizer — the
    │  SAME statement splitter pipelines use, not a semicolon split, so a
    │  string literal or comment containing one is never cut in half)
    │  runs every statement; the LAST one's rows (if any) become the
    │  cell's displayed preview, capped at row_limit (200 default)
    │  a cell that errors stops the run — later cells report ok:null
    ▼
    one JSON result line: {ok, cells: [{id, ok, error, display}, ...]}
```

Same isolation reasoning as a pipeline run — a subprocess so a runaway
query's timeout can be enforced by killing the OS process, and so it can
never take the main app down (`CI_SANDBOX_TIMEOUT_DEFAULT`/`_MAX`, 30s/120s
by default: much shorter than a pipeline's, matching its interactive
purpose). Unlike a pipeline run, a sandbox run is **read-only** — no
materialization — so it answers its HTTP request directly, with no queue:
concurrent sandbox runs are safe and desirable for an interactive tool.

Credentials are already configured on the shared connection (the same
`duck.py` secrets every query uses), so a cell reaches the bucket with
DuckDB's own readers directly — `read_parquet('s3://…')`, `read_csv`,
`delta_scan`, `iceberg_scan` — and never writes a `CREATE SECRET` or a
`SET` itself.

## `app/sandbox.py`: text transforms, not execution

This module never runs anything — it builds and parses text: combining a
notebook's cells into one script, detecting the bucket-reading calls that
would become a pipeline's declared sources, rendering a starter pipeline
YAML, and (the security-relevant part) **re-validating whatever the coding
agent proposes** before any of it can reach a cell or a pipeline YAML.

- **`combine_cells(sources)`** — joins cell sources in run order, each
  terminated with `;` if it lacks one.
- **`extract_reads(script)`** — every distinct bucket-reading table-function
  call (`READ_RE`, matched against a comment-masked copy of the text so a
  path mentioned only in a `--` note is never mistaken for a real source),
  each assigned a generated pipeline source name derived from the path
  (`_name_from_path` — `sales/*.parquet` names itself `sales`, walking up
  path segments past anything that sanitizes to nothing, like a bare `*`).
- **`rewrite_reads_to_sources(script, sources)`** — replaces each matched
  call site with the declared source's bare name (`"sales"`), which is what
  a pipeline registers each source as a view under.
- **`has_output_assignment(script)`** — checks the same pipeline contract
  (`sql:` must end on a `SELECT` or create a relation named `output`)
  without executing anything, so convert-to-pipeline can warn rather than
  guess when it isn't met.
- **`validate_agent_cells(raw_cells, known_ids)`** — re-checks every cell
  the coding agent proposed: a `target` naming a cell that isn't in the
  notebook is **downgraded to an append** rather than dropped (the code is
  still useful, it just can't safely overwrite something it may not have
  seen); a syntax error is *reported on the proposal*, not silently
  discarded — this is a scratch notebook, and a half-broken cell an admin
  fixes in place beats a proposal that vanished.
- **`validate_lineage(raw_entries, source_names, output_columns)`** — the
  same re-validation discipline applied to agent-generated lineage: a field
  the run's real output doesn't contain, a duplicate, or a `from` ref
  naming an undeclared source is dropped with a warning rather than
  producing a pipeline YAML the loader would reject outright.

## The coding agent (`app/sandbox_agent.py`)

`◈ AGENT` — admin-only, and only when `CI_LLM_API_KEY` is configured (the
same key [conversational analytics](conversational-analytics.md) uses) —
writes SQL cells *for the notebook that's currently open*. It's the sibling
seam to `app/llm.py` (chat's translator): same "typed, unvalidated proposal,
re-checked before it can act" contract, same provider-neutral
`llmclient.ChatRequest`/`ChatRequest.stream()` machinery (see
[Conversational Analytics → Any provider](conversational-analytics.md#any-provider-a-url-and-a-key)).

**What it sees**: every cell's source, the last run's stdout/error tails
(truncated, `SANDBOX_AGENT_OUTPUT_CHARS`), each result's **schema only**
(column names + dtypes — `CellContext.columns`; result *rows* are never
sent), and the bucket's paths collapsed to what a reader call can name
(`_bucket_files`, capped at `SANDBOX_AGENT_FILES`). A reply is a set of
proposed cells the admin explicitly **APPLY**s or **APPLY + RUN**s — never
applied, run, or saved on the agent's own behalf.

**Deliberately tuned for a fast interactive loop, not autonomy** — because a
sandbox already has the fastest feedback channel there is: run the cell.
Concretely:

- **One model call per request.** No tool-result loop, no self-critique
  pass — a failing cell's error is simply context for the *next* request.
- **No tests, benchmarks, or defensive scaffolding** — a hard rule stated
  directly in the system prompt, since that's where a coding agent's tokens
  and latency usually go, and this is a scratch tool where re-running the
  cell is cheaper than generated test code would be.
- **No extended thinking**, a **cached system prompt**
  (`ChatRequest.cache_system=True` — the SQL performance doctrine below is
  long, static, and resent every turn), and a **bounded context**.
- **Model per request**: `CI_SANDBOX_AGENT_MODEL` (default:
  `config.LLM_MODEL`); the panel's own dropdown can override it per call.

The system prompt is a **SQL performance brief**, not a generic coding
one — project and filter in the scan so pushdown drops row groups before
they leave the bucket, read each path once (put a repeated glob in a
`TEMP VIEW`), one pass with `FILTER (WHERE …)` rather than a query per
condition, a window function instead of a self-join, semi/anti joins for
filtering-only joins, `USING SAMPLE` rather than `LIMIT` for a real sample,
`EXPLAIN ANALYZE` when the question is whether pushdown is actually
happening.

**Convert to pipeline** (`→ CONVERT TO PIPELINE`) is the bridge from
scratch exploration to a saved, scheduled-by-hand transformation: combine
→ detect sources → rewrite call sites → render a starter pipeline YAML with
placeholder `target:`/`materialization:` the admin must fill in and review
before saving — a text-transform assist, never a silent one-click pipeline.
**`→ CONVERT + LINEAGE`** (shown only when the agent is configured)
additionally asks the agent for the one part text-transformation genuinely
can't derive: the pipeline's `description:` and its field-level `lineage:`
(`LLMSandboxAgent.describe_lineage`, on the cheaper `CI_SANDBOX_LINEAGE_MODEL`
— Haiku by default, since this is mechanical summarization of a script the
platform already parsed). The generated section is re-validated
(`sandbox.validate_lineage`) before it's ever rendered; running the
notebook first gives the *grounded* version (real output columns to check
field names against) — without a run there are no columns to check, and the
response says so. A flaky model call degrades to a warning, never to losing
the plain conversion.

## Persistence (`app/sandboxstore.py`)

`SandboxStore` persists `{name, cells}` — **only the code**, never
execution state or output. `RUN` replays from scratch on every call
precisely because nothing about a run is ever saved.

## API surface

See [API Layer](api-layer.md#sandbox) for the full route table.
`GET /api/sandbox/notebooks[/{id}]` read at any role;
`POST`/`PUT`/`DELETE /api/sandbox/notebooks[/{id}]`,
`POST /api/sandbox/run`, `POST /api/sandbox/convert`, and
`POST /api/sandbox/agent/stream` all require **admin**.
