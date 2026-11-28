"""The improvement set: real checks of the last few days (improvement_days,
7 by default), which the owner may download to measure and improve the engine.

Nothing is copied: the set is the verifications themselves, so a check that is
erased or deleted (the person's right, or the app's) leaves the set at once,
and once its days are over it is no longer offered. Test runs are not in it.
The hosted page's notice tells the person, and API clients say so in theirs.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import storage
from .config import get_settings
from .db import Verification, utcnow

FINAL = ("approved", "review", "rejected")


def _query(days: int):
    since = utcnow() - timedelta(days=days)
    return (select(Verification)
            .where(Verification.is_test.is_(False), Verification.status.in_(FINAL),
                   Verification.media_purged.is_(False), Verification.completed_at >= since)
            .order_by(Verification.completed_at.desc()))


def summary(db: Session) -> dict:
    days = get_settings().improvement_days
    if days <= 0:
        return {"days": 0, "count": 0, "by_status": {}, "by_type": {}}
    rows = list(db.scalars(_query(days)))
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for v in rows:
        by_status[v.status] = by_status.get(v.status, 0) + 1
        t = (v.result or {}).get("extracted", {}).get("document_type") or v.document_type
        by_type[t] = by_type.get(t, 0) + 1
    return {"days": days, "count": len(rows), "by_status": by_status, "by_type": by_type}


def export_zip(db: Session, limit: int = 500) -> tuple[bytes, int]:
    """One folder per check: its photos and what get.ekyc made of them (and
    what a reviewer decided, the answer to learn from). No client keys, no
    external_ref, no claimed details."""
    days = get_settings().improvement_days
    if days <= 0:
        return b"", 0
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for v in db.scalars(_query(days).limit(limit)):
            result = v.result or {}
            label = {
                "verification_id": v.id, "client": v.client.name if v.client else None,
                "requested_type": v.document_type, "country": v.country,
                "status": v.status, "decided_by": v.decided_by, "reasons": v.reasons or [],
                "completed_at": v.completed_at.isoformat() + "Z" if v.completed_at else None,
                "liveness_challenge": v.liveness_challenge,
                "extracted": result.get("extracted"), "checks": result.get("checks"), "scores": result.get("scores"),
                "engine_version": result.get("engine_version"),
            }
            zf.writestr(f"{v.id}/result.json", json.dumps(label, indent=1, default=str))
            for m in v.media:
                ext = "jpg"
                try:
                    zf.writestr(f"{v.id}/{m.kind}.{ext}", storage.load(m.path, m.id))
                except FileNotFoundError:
                    continue
            n += 1
        zf.writestr("README.txt", (
            f"get.ekyc improvement set: real checks of the last {days} days ({n}).\n"
            "Personal data: keep it encrypted, use it only to improve get.ekyc, and delete it\n"
            f"within {days} days of downloading. Each folder: the photos and result.json.\n"))
    return buf.getvalue(), n
