"""Session endpoints: login, logout, me, password change.

The password form is the demo-default login backend; a future OIDC
backend would be a sibling router that resolves a user and calls the same
auth.establish_session — nothing downstream changes (research R7).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth, config
from ..auth import User, get_current_user
from ..registry import registry

router = APIRouter()


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


def _user_out(user: User) -> dict:
    return {"id": user.id, "username": user.username,
            "display_name": user.display_name, "role": user.role}


def _set_session_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME, value,
        max_age=config.SESSION_MAX_DAYS * 86400,
        httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/",
    )


@router.post("/auth/login")
def login(body: LoginIn, response: Response):
    """Public (middleware allowlist). Identical 401s for unknown username
    and wrong password, with a real hash verification on both paths."""
    store = registry.auth_store
    row = store.get_user_by_username(body.username)
    if not row or not row["is_active"]:
        auth.burn_a_hash_check(body.password)
        store.record_audit("login_failed", body.username.strip().lower())
        raise HTTPException(status_code=401, detail="invalid credentials")
    remaining = auth.lockout_remaining(row)
    if remaining:
        store.record_audit("lockout", row["username"], actor_user_id=row["id"])
        raise HTTPException(status_code=423,
                            detail=f"account temporarily locked; retry in {remaining}s")
    if not auth.verify_password(row["password_hash"], body.password):
        auth.register_login_failure(store, row)
        store.record_audit("login_failed", row["username"], actor_user_id=row["id"])
        raise HTTPException(status_code=401, detail="invalid credentials")
    auth.register_login_success(store, row)
    if auth.password_needs_rehash(row["password_hash"]):
        store.update_user(row["id"], password_hash=auth.hash_password(body.password))
    cookie = auth.establish_session(store, row["id"])
    _set_session_cookie(response, cookie)
    store.record_audit("login", row["username"], actor_user_id=row["id"])
    return {"user": _user_out(auth.principal_from_row(row))}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response,
           user: User = Depends(get_current_user)):
    cookie = request.cookies.get(auth.COOKIE_NAME)
    if cookie:
        auth.revoke_session(registry.auth_store, cookie)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    registry.auth_store.record_audit("logout", user.username, actor_user_id=user.id)


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return _user_out(user)


@router.post("/auth/password", status_code=204)
def change_password(body: PasswordIn, request: Request,
                    user: User = Depends(get_current_user)):
    """Verify the current password, rehash, and end every *other* session
    for the account (the one making the change stays alive)."""
    store = registry.auth_store
    row = store.get_user(user.id)
    if not auth.verify_password(row["password_hash"], body.current_password):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    try:
        new_hash = auth.hash_password(body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    store.update_user(user.id, password_hash=new_hash)
    cookie = request.cookies.get(auth.COOKIE_NAME, "")
    keep = auth.session_id_for(store, cookie) if cookie else None
    store.revoke_sessions_for_user(user.id, except_session_id=keep)
    store.record_audit("password_changed", user.username, actor_user_id=user.id)
