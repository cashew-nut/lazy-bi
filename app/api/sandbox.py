"""Sandbox notebook endpoints: ad hoc polars/python scratch scripts (see
app/sandbox.py's module docstring for the trust model — identical carve-out
to a pipeline's `script:`, admin-gated for both authoring and execution).
Reads (list/get saved notebooks) are open to any authenticated role, same as
pipeline definitions; create/update/delete/run/convert all require admin.

The coding agent (POST /sandbox/agent/stream, and convert's opt-in lineage
generation) is admin-gated too, for two reasons: its whole output is code
destined for an admin-trust notebook, and calling it spends money and sends
the notebook's code to a third party (app/sandbox_agent.py's docstring
details the egress). It is 503 unless CI_LLM_API_KEY is configured, exactly
like conversational analytics.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config, s3
from .. import sandbox as sandbox_mod
from .. import sandbox_agent
from ..auth import User, require_role
from ..registry import registry

router = APIRouter(tags=["sandbox"])

# One stateless client for the server-configured models; a request that picks
# a different coding model gets its own (mirrors app/api/chat.py's
# _translator / _translator_for pair).
_agent = sandbox_agent.AnthropicSandboxAgent()


class Cell(BaseModel):
    id: str
    source: str = ""


class NotebookIn(BaseModel):
    name: str
    cells: list[Cell] = []


class RunIn(BaseModel):
    cells: list[Cell]
    run_upto: int
    timeout_seconds: Optional[int] = None


class OutputColumn(BaseModel):
    name: str
    dtype: str = ""


class CellOutput(BaseModel):
    """A cell's last run as the browser still has it — the agent's view of
    "what happened when this was run". Sent by the client rather than read
    from storage because a run's output is deliberately never persisted
    (Constitution V) and because the live notebook may be unsaved."""
    status: str = "not run"      # ok | error | not run
    stdout: str = ""
    error: str = ""
    columns: list[OutputColumn] = []
    row_count: Optional[int] = None
    truncated: bool = False


class AgentCell(Cell):
    output: Optional[CellOutput] = None


class AgentTurnIn(BaseModel):
    request: str = ""
    reply: str = ""


class AgentIn(BaseModel):
    request: str
    name: str = ""
    cells: list[AgentCell] = []
    history: list[AgentTurnIn] = []
    llm_model: Optional[str] = None


class ConvertIn(BaseModel):
    name: str
    cells: list[Cell]
    # opt-in: ask the coding agent to fill in the pipeline's description and
    # field-level lineage. Off by default — conversion itself stays a pure,
    # free, offline text transform.
    with_lineage: bool = False
    # the last run's output schema, when the notebook has been run: the
    # ground truth for which field names a lineage entry may name at all
    output_columns: list[OutputColumn] = []


def _cells_out(cells: list[Cell]) -> list[dict]:
    return [{"id": c.id, "source": c.source} for c in cells]


def _sse(event: str, data: dict) -> str:
    """Same wire shape as app/api/chat.py's _sse — the frontend reads both
    with the one parseSSE() in static/js/chat.js."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _require_agent() -> None:
    if not config.LLM_ENABLED:
        raise HTTPException(status_code=503, detail="the sandbox coding agent is not configured")


def _agent_for(llm_model: Optional[str]):
    if not llm_model:
        return _agent
    if llm_model not in config.LLM_MODEL_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown llm_model '{llm_model}' (choose one of {config.LLM_MODEL_CHOICES})",
        )
    if llm_model == config.SANDBOX_AGENT_MODEL:
        return _agent
    return sandbox_agent.AnthropicSandboxAgent(model=llm_model)


def _bucket_files() -> list[dict]:
    """The bucket paths a `read(...)` call could name, collapsed to table
    roots + standalone files (sandbox.bucket_entries). One capped
    list_objects_v2 call, not a full paginated crawl: this runs on every
    agent request, and an unbounded listing would cost more latency than the
    model call it decorates. A bucket that can't be reached is not fatal —
    the agent just works from the notebook alone."""
    try:
        response = s3.client().list_objects_v2(
            Bucket=config.BUCKET, MaxKeys=config.SANDBOX_AGENT_FILES * 10)
        objects = [{"key": o["Key"], "size": o["Size"]} for o in response.get("Contents", [])]
    except Exception:
        return []
    return sandbox_mod.bucket_entries(objects, config.BUCKET)


@router.get("/sandbox/notebooks")
def list_notebooks():
    return registry.sandbox_store.list()


@router.get("/sandbox/notebooks/{nb_id}")
def get_notebook(nb_id: int):
    nb = registry.sandbox_store.get(nb_id)
    if not nb:
        raise HTTPException(status_code=404, detail=f"unknown sandbox notebook '{nb_id}'")
    return nb


@router.post("/sandbox/notebooks", status_code=201)
def create_notebook(body: NotebookIn, user: User = Depends(require_role("admin"))):
    nb = registry.sandbox_store.create(body.name, _cells_out(body.cells))
    registry.auth_store.record_audit("sandbox.create", user.display_name, actor_user_id=user.id, target=body.name)
    return nb


@router.put("/sandbox/notebooks/{nb_id}")
def update_notebook(nb_id: int, body: NotebookIn, user: User = Depends(require_role("admin"))):
    nb = registry.sandbox_store.update(nb_id, body.name, _cells_out(body.cells))
    if not nb:
        raise HTTPException(status_code=404, detail=f"unknown sandbox notebook '{nb_id}'")
    registry.auth_store.record_audit("sandbox.update", user.display_name, actor_user_id=user.id, target=body.name)
    return nb


@router.delete("/sandbox/notebooks/{nb_id}", status_code=204)
def delete_notebook(nb_id: int, user: User = Depends(require_role("admin"))):
    nb = registry.sandbox_store.get(nb_id)
    if not nb:
        raise HTTPException(status_code=404, detail=f"unknown sandbox notebook '{nb_id}'")
    registry.sandbox_store.delete(nb_id)
    registry.auth_store.record_audit("sandbox.delete", user.display_name, actor_user_id=user.id, target=nb["name"])


@router.post("/sandbox/run")
def run(body: RunIn, user: User = Depends(require_role("admin"))):
    if not body.cells or not (0 <= body.run_upto < len(body.cells)):
        raise HTTPException(status_code=400, detail="run_upto must index into a non-empty cells list")
    timeout = min(body.timeout_seconds or config.SANDBOX_TIMEOUT_DEFAULT, config.SANDBOX_TIMEOUT_MAX)
    job = {
        "cells": _cells_out(body.cells),
        "run_upto": body.run_upto,
        "bucket": config.BUCKET,
        "row_limit": config.SANDBOX_ROW_LIMIT,
        "storage": {"read": config.storage_options()},
    }
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.sandbox_runner"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not start sandbox runner: {exc}")

    try:
        stdout, stderr = proc.communicate(json.dumps(job), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # reap the process so its pipes don't leak
        registry.auth_store.record_audit("sandbox.run", user.display_name, actor_user_id=user.id, target="(timeout)")
        return {"ok": False, "error": f"run exceeded its {timeout}s timeout and was terminated", "cells": []}

    registry.auth_store.record_audit("sandbox.run", user.display_name, actor_user_id=user.id)
    if not stdout.strip():
        return {
            "ok": False, "cells": [],
            "error": f"runner exited with code {proc.returncode} without reporting a result "
                     f"(stderr: {stderr[-4000:]})",
        }
    try:
        return json.loads(stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"ok": False, "cells": [], "error": f"could not parse runner output: {exc}"}


@router.post("/sandbox/convert")
def convert(body: ConvertIn, user: User = Depends(require_role("admin"))):
    """Text-only transform (never executes anything): combines the
    notebook's cells, detects its `read(...)` bucket-scan calls as pipeline
    sources, rewrites the call sites to `sources["name"]`, and renders a
    starter pipeline yaml — the admin still fills in target + materialization
    and reviews the script before saving (see app/sandbox.py).

    With `with_lineage`, the one part a text transform genuinely cannot
    derive — the pipeline's description and its per-field lineage — is asked
    of the coding agent, then re-validated against the detected sources and
    the run's real output columns (sandbox.validate_lineage) before it is
    rendered. An agent failure degrades to a warning: the conversion still
    returns the same yaml it would have without the option, because losing a
    converted notebook to a flaky API call would be absurd."""
    script = sandbox_mod.combine_cells([c.source for c in body.cells])
    sources = sandbox_mod.extract_reads(script)
    rewritten = sandbox_mod.rewrite_reads_to_sources(script, sources)
    warnings = []
    if not sandbox_mod.has_output_assignment(rewritten):
        warnings.append(
            "no 'output = ...' assignment found — add one (the pipeline script contract) before saving"
        )
    lineage: list[dict] = []
    description = ""
    if body.with_lineage:
        _require_agent()
        lineage, description, lineage_warnings = _generate_lineage(body, rewritten, sources, user)
        warnings += lineage_warnings
    yaml_text = sandbox_mod.build_pipeline_yaml(body.name, rewritten, sources, lineage, description)
    return {"yaml": yaml_text, "sources": sources, "lineage": lineage, "warnings": warnings}


def _generate_lineage(body: ConvertIn, script: str, sources: list[dict], user: User):
    """The agent half of convert(): one call on the cheap lineage model, then
    validation. Returns (lineage, description, warnings) — never raises."""
    context = sandbox_agent.LineageContext(
        pipeline_name=body.name or "pipeline",
        script=script,
        sources=sources,
        output_columns=[c.model_dump() for c in body.output_columns],
    )
    try:
        raw = _agent.describe_lineage(context)
    except sandbox_agent.AgentError as exc:
        return [], "", [f"lineage generation failed ({exc}) — the yaml below has no lineage section"]
    lineage, warnings = sandbox_mod.validate_lineage(
        raw.get("lineage"), [s["name"] for s in sources], [c.name for c in body.output_columns],
    )
    description = raw.get("description") if isinstance(raw.get("description"), str) else ""
    registry.auth_store.record_audit(
        "sandbox.agent", user.display_name, actor_user_id=user.id,
        target=f"lineage notebook:{body.name!r} fields:{len(lineage)}",
    )
    if not lineage:
        warnings.append("the agent produced no usable lineage — declare it by hand before saving")
    elif not body.output_columns:
        warnings.append(
            "lineage was generated without a run to check field names against — run the notebook "
            "first for a grounded version"
        )
    return lineage, description, warnings


@router.post("/sandbox/agent/stream")
def agent_stream(body: AgentIn, user: User = Depends(require_role("admin"))):
    """The sandbox coding agent: one model call per request, streamed as SSE
    so proposed code appears as it's written (`tool_name` / `tool_input`
    events), ending with a `response` event carrying the validated proposal.

    Nothing here executes, applies, or saves anything — the reply is a
    proposal the admin applies into cells and runs themselves. That is also
    the design's feedback loop: a failing cell's error comes back as context
    on the next request, which is faster and cheaper than having the agent
    write and run its own checks (see app/sandbox_agent.py)."""
    _require_agent()
    if not body.request.strip():
        raise HTTPException(status_code=400, detail="request must not be empty")
    agent = _agent_for(body.llm_model)
    notebook = sandbox_agent.NotebookContext(
        name=body.name,
        cells=[_cell_context(c) for c in body.cells],
        files=_bucket_files(),
        bucket=config.BUCKET,
    )
    history = [sandbox_agent.AgentTurn(request=h.request, reply=h.reply) for h in body.history]
    known_ids = [c.id for c in body.cells]

    def gen():
        raw = None
        try:
            for event in agent.assist_streaming(body.request, notebook, history):
                if event.kind == "tool_name":
                    yield _sse("tool_name", {"tool_name": event.tool_name})
                elif event.kind == "tool_input":
                    yield _sse("tool_input", {"tool_input": event.tool_input})
                elif event.kind == "done":
                    raw = event.final
        except sandbox_agent.AgentError as exc:
            yield _sse("response", {
                "kind": "error", "text": f"the coding agent is temporarily unavailable: {exc}",
                "cells": [], "notes": "", "warnings": [],
            })
            return
        yield _sse("response", _agent_response(raw, known_ids, user, body.request))

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _cell_context(cell: AgentCell) -> sandbox_agent.CellContext:
    out = cell.output
    return sandbox_agent.CellContext(
        id=cell.id, source=cell.source,
        status=out.status if out else "not run",
        stdout=out.stdout if out else "",
        error=out.error if out else "",
        columns=[c.model_dump() for c in out.columns] if out else [],
        row_count=out.row_count if out else None,
        truncated=out.truncated if out else False,
    )


def _agent_response(raw, known_ids: list[str], user: User, request: str) -> dict:
    """Turn the agent's (unvalidated) tool call into the response the browser
    applies from, re-checking every proposed cell first
    (sandbox.validate_agent_cells)."""
    if raw is None:
        return {"kind": "error", "text": "the coding agent returned no reply",
                "cells": [], "notes": "", "warnings": []}
    if raw.kind == "answer":
        text = raw.args.get("text")
        registry.auth_store.record_audit(
            "sandbox.agent", user.display_name, actor_user_id=user.id,
            target=f"answer request:{request!r}",
        )
        return {"kind": "answer", "text": text if isinstance(text, str) else "",
                "cells": [], "notes": "", "warnings": []}
    cells, warnings = sandbox_mod.validate_agent_cells(raw.args.get("cells"), known_ids)
    notes = raw.args.get("notes")
    registry.auth_store.record_audit(
        "sandbox.agent", user.display_name, actor_user_id=user.id,
        target=f"write_cells request:{request!r} cells:{len(cells)}",
    )
    return {
        "kind": "cells" if cells else "error",
        "text": "" if cells else "the coding agent proposed nothing usable",
        "cells": cells, "notes": notes if isinstance(notes, str) else "", "warnings": warnings,
    }
