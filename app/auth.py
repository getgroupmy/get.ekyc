from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto
from .config import get_settings
from .db import ApiClient, get_db


def new_api_key() -> str:
    return "ek_" + secrets.token_urlsafe(32)


def require_client(x_api_key: str = Header(default=""), db: Session = Depends(get_db)) -> ApiClient:
    client = None
    if x_api_key:
        client = db.scalar(select(ApiClient).where(ApiClient.key_hash == crypto.sha256_hex(x_api_key)))
    if client is None or not client.active:
        raise HTTPException(401, "invalid or missing X-API-Key")
    return client


SESSION_COOKIE = "ekyc_session"
SESSION_HOURS = 12
REMEMBER_DAYS = 30


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), dk)
    except Exception:  # noqa: BLE001
        return False


def _session_key() -> bytes:
    # Derived from the storage key and the admin token: changing the token signs everyone out.
    s = get_settings()
    return hmac.new(s.storage_key_bytes, b"session|" + s.admin_token.encode(), hashlib.sha256).digest()


def make_session(username: str, remember: bool) -> tuple[str, int]:
    max_age = REMEMBER_DAYS * 86400 if remember else SESSION_HOURS * 3600
    exp = int(time.time()) + max_age
    body = f"{username}|{exp}"
    sig = hmac.new(_session_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}", max_age


def read_session(value: str | None) -> str | None:
    if not value or value.count("|") != 2:
        return None
    username, exp, sig = value.split("|")
    good = hmac.new(_session_key(), f"{username}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or not exp.isdigit() or int(exp) < time.time():
        return None
    return username


def admin_ctx(request: Request, x_admin_token: str = Header(default="")):
    """Who is calling: the admin token (owner) or a signed-in console user and their role."""
    from .permissions import ROLES, Ctx
    if x_admin_token and hmac.compare_digest(x_admin_token.encode(), get_settings().admin_token.encode()):
        return Ctx("admin", None, "owner")
    user = read_session(request.cookies.get(SESSION_COOKIE))
    if user:
        from .db import AdminUser, session_scope
        with session_scope() as db:  # a deleted user's cookie stops working; role changes apply at once
            row = db.get(AdminUser, user)
            if row is not None and row.role in ROLES:
                return Ctx(f"admin:{user}", user, row.role)
    raise HTTPException(401, "sign in, or send X-Admin-Token")


def require_admin(request: Request, x_admin_token: str = Header(default="")) -> str:
    return admin_ctx(request, x_admin_token).actor


def need(perm: str):
    """Dependency: the caller must hold `perm`."""
    def check(request: Request, x_admin_token: str = Header(default="")):
        ctx = admin_ctx(request, x_admin_token)
        if not ctx.can(perm):
            raise HTTPException(403, f"your role ({ctx.role}) cannot do this ({perm})")
        return ctx
    return check
