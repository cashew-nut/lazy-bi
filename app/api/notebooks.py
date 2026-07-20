"""Notebooks CRUD: freeform HTML pages with live visuals/dashboards embedded.

A notebook's `html` is a body fragment authored (by hand today, by an LLM
later) using the app's own layout primitives — native <details> for
collapsibles, the `.nb-tabs` convention for tabs, and `.nb-visual`/
`.nb-dashboard` marker elements the client hydrates into live charts after
render. The server treats it as opaque text; structure/safety is enforced
client-side at render time (see notebook.js).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_role
from ..registry import registry

router = APIRouter(tags=["notebooks"])


class NotebookIn(BaseModel):
    name: str
    html: str = ""


@router.get("/notebooks")
def list_notebooks():
    return registry.store.list_notebooks()


@router.get("/notebooks/{notebook_id}")
def get_notebook(notebook_id: int):
    notebook = registry.store.get_notebook(notebook_id)
    if not notebook:
        raise HTTPException(status_code=404, detail="notebook not found")
    return notebook


@router.post("/notebooks", status_code=201, dependencies=[Depends(require_role("author"))])
def create_notebook(n: NotebookIn):
    return registry.store.create_notebook(n.name, n.html)


@router.put("/notebooks/{notebook_id}", dependencies=[Depends(require_role("author"))])
def update_notebook(notebook_id: int, n: NotebookIn):
    updated = registry.store.update_notebook(notebook_id, n.name, n.html)
    if not updated:
        raise HTTPException(status_code=404, detail="notebook not found")
    return updated


@router.delete("/notebooks/{notebook_id}", status_code=204, dependencies=[Depends(require_role("author"))])
def delete_notebook(notebook_id: int):
    if not registry.store.delete_notebook(notebook_id):
        raise HTTPException(status_code=404, detail="notebook not found")
