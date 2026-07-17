"""Model memory endpoints: the admin-curated pool of facts the chat
assistant has learned about each semantic model (app/memorystore.py).

Reads are open to any authenticated user — a memory's whole purpose is to
be merged into the chat catalog every user already sees (nlq.build_catalog),
so listing it leaks nothing new. Mutations are admin-only: memories change
how the assistant interprets everyone's questions, the same blast radius as
editing the model yaml itself.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import User, require_role
from ..memorystore import validate_memory
from ..registry import registry
from .deps import get_model

router = APIRouter(tags=["memories"])


class MemoryIn(BaseModel):
    kind: str
    subject: str = ""
    content: str


class MemoryPatch(BaseModel):
    subject: Optional[str] = None
    content: Optional[str] = None


def _get_model_memory(name: str, memory_id: int) -> dict:
    """404 both for an unknown id and for an id that exists on a *different*
    model — a memory is only addressable under the model it belongs to."""
    memory = registry.memory_store.get(memory_id)
    if not memory or memory["model"] != name:
        raise HTTPException(status_code=404, detail="memory not found")
    return memory


@router.get("/models/{name}/memories")
def list_memories(name: str):
    get_model(name)  # 404 for unknown model
    return registry.memory_store.list_for_model(name)


@router.post("/models/{name}/memories", status_code=201)
def create_memory(name: str, body: MemoryIn, user: User = Depends(require_role("admin"))):
    model = get_model(name)
    error = validate_memory(model, body.kind, body.subject.strip(), body.content.strip())
    if error:
        raise HTTPException(status_code=400, detail=error)
    memory = registry.memory_store.add(
        name, body.kind, body.subject, body.content,
        source="admin", created_by=user.username,
    )
    if memory is None:
        raise HTTPException(status_code=409, detail="an identical memory already exists (or the model is at its memory cap)")
    registry.auth_store.record_audit(
        "memory_create", user.username, actor_user_id=user.id,
        target=f"model:{name} memory:{memory['id']} kind:{memory['kind']} "
               f"subject:{memory['subject']!r} content:{memory['content']!r}",
    )
    return memory


@router.patch("/models/{name}/memories/{memory_id}")
def update_memory(name: str, memory_id: int, body: MemoryPatch,
                  user: User = Depends(require_role("admin"))):
    model = get_model(name)
    current = _get_model_memory(name, memory_id)
    subject = body.subject.strip() if body.subject is not None else current["subject"]
    content = body.content.strip() if body.content is not None else current["content"]
    error = validate_memory(model, current["kind"], subject, content)
    if error:
        raise HTTPException(status_code=400, detail=error)
    updated = registry.memory_store.update(memory_id, subject=subject, content=content)
    registry.auth_store.record_audit(
        "memory_update", user.username, actor_user_id=user.id,
        target=f"model:{name} memory:{memory_id} subject:{subject!r} content:{content!r}",
    )
    return updated


@router.delete("/models/{name}/memories/{memory_id}", status_code=204)
def delete_memory(name: str, memory_id: int, user: User = Depends(require_role("admin"))):
    get_model(name)
    _get_model_memory(name, memory_id)
    registry.memory_store.delete(memory_id)
    registry.auth_store.record_audit(
        "memory_delete", user.username, actor_user_id=user.id,
        target=f"model:{name} memory:{memory_id}",
    )
