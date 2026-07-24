"""Sandbox notebook endpoints: ad hoc polars/python scratch scripts (see
app/sandbox.py's module docstring for the trust model — identical carve-out
to a pipeline's `script:`, admin-gated for both authoring and execution).
Reads (list/get saved notebooks) are open to any authenticated role, same as
pipeline definitions; create/update/delete/run/convert all require admin.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import config
from .. import sandbox as sandbox_mod
from ..auth import User, require_role
from ..registry import registry

router = APIRouter(tags=["sandbox"])


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


class ConvertIn(BaseModel):
    name: str
    cells: list[Cell]


def _cells_out(cells: list[Cell]) -> list[dict]:
    return [c.model_dump() for c in cells]


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
    and reviews the script before saving (see app/sandbox.py)."""
    script = sandbox_mod.combine_cells([c.source for c in body.cells])
    sources = sandbox_mod.extract_reads(script)
    rewritten = sandbox_mod.rewrite_reads_to_sources(script, sources)
    warnings = []
    if not sandbox_mod.has_output_assignment(rewritten):
        warnings.append(
            "no 'output = ...' assignment found — add one (the pipeline script contract) before saving"
        )
    yaml_text = sandbox_mod.build_pipeline_yaml(body.name, rewritten, sources)
    return {"yaml": yaml_text, "sources": sources, "warnings": warnings}
