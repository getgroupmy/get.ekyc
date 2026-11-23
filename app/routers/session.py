"""Console sign-in with a username and password (session cookie). The admin
token keeps working as a fallback and to create the first admin."""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import SESSION_COOKIE, check_password, make_session, read_session
from ..db import AdminUser, audit, get_db, utcnow

router = APIRouter(prefix="/admin/session", tags=["admin sign-in"])
_failures: dict[str, list[float]] = defaultdict(list)
LOCK_AFTER, LOCK_SECONDS = 5, 15 * 60


class Login(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)
    remember: bool = False


def _throttled(key: str) -> bool:
    now = time.time()
    _failures[key] = [t for t in _failures[key] if now - t < LOCK_SECONDS]
    return len(_failures[key]) >= LOCK_AFTER


@router.post("/login")
def login(body: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    key = f"{ip}|{body.username.lower()}"
    if _throttled(key):
        raise HTTPException(429, "too many attempts; try again in 15 minutes")
    user = db.get(AdminUser, body.username.strip().lower())
    if user is None or not check_password(body.password, user.password_hash):
        _failures[key].append(time.time())
        audit(db, "system", "admin.login_failed", detail=body.username.strip().lower()[:60])
        raise HTTPException(401, "wrong username or password")
    _failures.pop(key, None)
    value, max_age = make_session(user.username, body.remember)
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(SESSION_COOKIE, value, max_age=max_age, httponly=True, samesite="strict",
                        secure=https, path="/admin")
    user.last_login_at = utcnow()
    audit(db, f"admin:{user.username}", "admin.login", detail="remember 30 days" if body.remember else None)
    return {"username": user.username}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    """Who is signed in (cookie, or the admin token if sent), their role and permissions."""
    from ..auth import admin_ctx
    exists = bool(db.scalar(select(func.count()).select_from(AdminUser)))
    try:
        ctx = admin_ctx(request, request.headers.get("x-admin-token", ""))
    except HTTPException:
        return {"username": None, "role": None, "permissions": [], "admins_exist": exists}
    return {"username": ctx.username or "token", "role": ctx.role, "permissions": sorted(ctx.perms),
            "sees_all": ctx.sees_all, "admins_exist": exists}


class NewPassword(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=10, max_length=200)


@router.post("/password")
def change_password(body: NewPassword, request: Request, db: Session = Depends(get_db)):
    """Any signed-in user can change their own password."""
    from ..auth import hash_password
    user = read_session(request.cookies.get(SESSION_COOKIE))
    row = db.get(AdminUser, user) if user else None
    if row is None:
        raise HTTPException(401, "sign in first")
    if not check_password(body.current, row.password_hash):
        raise HTTPException(403, "current password is wrong")
    row.password_hash = hash_password(body.new)
    audit(db, f"admin:{user}", "admin.password_changed", detail=user)
    return {"ok": True}
