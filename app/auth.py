from __future__ import annotations

import hmac
import secrets

from fastapi import Depends, Header, HTTPException
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


def require_admin(x_admin_token: str = Header(default="")) -> str:
    if not hmac.compare_digest(x_admin_token.encode(), get_settings().admin_token.encode()):
        raise HTTPException(401, "invalid or missing X-Admin-Token")
    return "admin"
