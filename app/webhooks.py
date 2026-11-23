"""Signed webhook delivery so the calling app learns the outcome without polling.

Signature: X-EKYC-Signature = "sha256=" + HMAC_SHA256(secret, f"{timestamp}.{body}")
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time

import httpx

from .config import get_settings
from .db import ApiClient, Verification, audit, session_scope

log = logging.getLogger("ekyc.webhooks")
_BACKOFF = (0, 5, 30)


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def payload_for(v: Verification) -> dict:
    return {
        "event": "verification.completed",
        "id": v.id,
        "external_ref": v.external_ref,
        "status": v.status,
        "reasons": v.reasons or [],
        "decided_by": v.decided_by,
        "completed_at": v.completed_at.isoformat() + "Z" if v.completed_at else None,
    }


def _deliver(url: str, secret: bytes, payload: dict, verification_id: str) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    outcome = "failed"
    for delay in _BACKOFF:
        time.sleep(delay)
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-EKYC-Timestamp": ts,
                   "X-EKYC-Signature": sign(secret, ts, body)}
        try:
            r = httpx.post(url, content=body, headers=headers, timeout=get_settings().webhook_timeout_seconds)
            if r.status_code < 300:
                outcome = "delivered"
                break
            outcome = f"http_{r.status_code}"
        except httpx.HTTPError as exc:
            outcome = type(exc).__name__
    try:
        with session_scope() as db:
            audit(db, "system", "webhook." + ("delivered" if outcome == "delivered" else "failed"),
                  verification_id, outcome)
    except Exception:  # noqa: BLE001
        log.exception("could not record webhook outcome")


def notify(v: Verification, client: ApiClient) -> None:
    if not client.webhook_url or not client.webhook_secret:
        return
    threading.Thread(target=_deliver, daemon=True,
                     args=(client.webhook_url, client.webhook_secret, payload_for(v), v.id)).start()
