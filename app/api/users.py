"""Admin user management + per-user personal access tokens (spec 011).

Users: admin-only, no self-service registration, no hard deletion —
deactivate-only so provenance/audit stay attributable. Tokens: any
signed-in user manages *their own*; the secret appears exactly once in the
create response and is stored only as a hash.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from ..auth import User, get_current_user, require_role
from ..authstore import LastAdminError
from ..registry import registry

router = APIRouter(tags=["users"])


class UserIn(BaseModel):
    username: str
    display_name: str = ""
    role: str
    password: str


class UserPatch(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class TokenIn(BaseModel):
    name: str


def _admin_out(row: dict) -> dict:
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"],
            "is_active": row["is_active"], "created_at": row["created_at"]}


def _hash_or_422(password: str) -> str:
    try:
        return auth.hash_password(password)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── user management (admin) ──────────────────────────────────

@router.get("/users", dependencies=[Depends(require_role("admin"))])
def list_users():
    return [_admin_out(u) for u in registry.auth_store.list_users()]


@router.post("/users", status_code=201)
def create_user(body: UserIn, admin: User = Depends(require_role("admin"))):
    store = registry.auth_store
    password_hash = _hash_or_422(body.password)
    try:
        row = store.create_user(body.username, body.display_name, body.role, password_hash)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409,
                            detail=f"username '{body.username.strip().lower()}' already exists")
    store.record_audit("user_created", admin.username, actor_user_id=admin.id,
                       target=row["username"])
    return _admin_out(row)


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch,
               admin: User = Depends(require_role("admin"))):
    store = registry.auth_store
    before = store.get_user(user_id)
    if not before:
        raise HTTPException(status_code=404, detail="unknown user")
    password_hash = _hash_or_422(body.password) if body.password is not None else None
    try:
        row = store.update_user(
            user_id, display_name=body.display_name, role=body.role,
            is_active=body.is_active, password_hash=password_hash)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except LastAdminError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # deactivation and password reset end every session the account holds;
    # a role change deliberately does not — the new role binds on the next
    # request through the session→user join
    if body.is_active is False or body.password is not None:
        store.revoke_sessions_for_user(user_id)
    audit = {"role": ("user_role_changed", body.role is not None and body.role != before["role"]),
             "deact": ("user_deactivated", body.is_active is False and before["is_active"]),
             "react": ("user_reactivated", body.is_active is True and not before["is_active"]),
             "pw": ("password_reset", body.password is not None)}
    for action, happened in audit.values():
        if happened:
            store.record_audit(action, admin.username, actor_user_id=admin.id,
                               target=before["username"])
    return _admin_out(row)


# ── personal access tokens (own account, any role) ───────────

@router.get("/tokens")
def list_tokens(user: User = Depends(get_current_user)):
    return registry.auth_store.list_tokens(user.id)


@router.post("/tokens", status_code=201)
def create_token(body: TokenIn, user: User = Depends(get_current_user)):
    secret, row = auth.mint_token(registry.auth_store, user.id, body.name)
    registry.auth_store.record_audit("token_created", user.username,
                                     actor_user_id=user.id, target=row["name"])
    # the only response that ever carries the secret (FR-013)
    return {"id": row["id"], "name": row["name"], "token": secret}


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: int, user: User = Depends(get_current_user)):
    if not registry.auth_store.revoke_token(token_id, user.id):
        raise HTTPException(status_code=404, detail="unknown token")
    registry.auth_store.record_audit("token_revoked", user.username,
                                     actor_user_id=user.id, target=str(token_id))
