from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import crypto, service, storage, webhooks
from ..auth import new_api_key, require_admin
from ..db import ApiClient, AuditLog, Media, Verification, audit, get_db, utcnow
from ..schemas import CreateClient, Decision, verification_out

router = APIRouter(prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin)])


def _client_out(c: ApiClient) -> dict:
    return {"id": c.id, "name": c.name, "key_prefix": c.key_prefix, "webhook_url": c.webhook_url,
            "active": c.active, "created_at": c.created_at.isoformat() + "Z"}


@router.post("/clients", status_code=201)
def create_client(body: CreateClient, db: Session = Depends(get_db)):
    key = new_api_key()
    secret = secrets.token_urlsafe(32) if body.webhook_url else None
    c = ApiClient(id="cli_" + secrets.token_hex(8), name=body.name, key_hash=crypto.sha256_hex(key),
                  key_prefix=key[:10], webhook_url=str(body.webhook_url) if body.webhook_url else None,
                  webhook_secret=secret.encode() if secret else None)
    db.add(c)
    audit(db, "admin", "client.created", detail=c.id)
    db.flush()
    # The API key and webhook secret are shown exactly once.
    return {**_client_out(c), "api_key": key, "webhook_secret": secret}


@router.get("/clients")
def list_clients(db: Session = Depends(get_db)):
    return {"items": [_client_out(c) for c in db.scalars(select(ApiClient).order_by(ApiClient.created_at))]}


@router.delete("/clients/{cid}", status_code=204)
def revoke_client(cid: str, db: Session = Depends(get_db)):
    c = db.get(ApiClient, cid)
    if c is None:
        raise HTTPException(404, "client not found")
    c.active = False
    audit(db, "admin", "client.revoked", detail=c.id)


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    rows = db.execute(select(Verification.status, func.count()).group_by(Verification.status)).all()
    return {"by_status": {status: n for status, n in rows}}


@router.get("/verifications")
def list_verifications(status: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    q = select(Verification).where(Verification.status != "deleted")
    if status:
        q = q.where(Verification.status == status)
    rows = db.scalars(q.order_by(Verification.created_at.desc()).limit(max(1, min(limit, 500))))
    return {"items": [verification_out(v, service.required_media(v), admin=True) for v in rows]}


@router.get("/verifications/{vid}")
def get_verification(vid: str, db: Session = Depends(get_db)):
    v = db.get(Verification, vid)
    if v is None:
        raise HTTPException(404, "verification not found")
    out = verification_out(v, service.required_media(v), admin=True)
    log = db.scalars(select(AuditLog).where(AuditLog.verification_id == vid).order_by(AuditLog.id))
    out["audit"] = [{"at": a.at.isoformat() + "Z", "actor": a.actor, "action": a.action, "detail": a.detail}
                    for a in log]
    audit(db, "admin", "verification.viewed", vid)
    return out


@router.get("/media/{mid}")
def get_media(mid: str, db: Session = Depends(get_db)):
    m = db.get(Media, mid)
    if m is None:
        raise HTTPException(404, "media not found")
    data = storage.load(m.path, m.id)
    kind = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"
    audit(db, "admin", "media.viewed", m.verification_id, m.kind)
    return Response(data, media_type=kind, headers={"Cache-Control": "no-store"})


@router.post("/verifications/{vid}/decision")
def decide(vid: str, body: Decision, db: Session = Depends(get_db)):
    v = db.get(Verification, vid)
    if v is None:
        raise HTTPException(404, "verification not found")
    if v.status not in service.FINAL:
        raise HTTPException(409, f"cannot decide a verification that is '{v.status}'")
    v.status = body.decision
    v.decided_by = "admin"
    v.review_note = body.note
    v.completed_at = utcnow()
    if body.decision == "approved":
        v.reasons = []
    elif not v.reasons:
        v.reasons = ["rejected_by_reviewer"]
    audit(db, "admin", f"verification.manual_{body.decision}", v.id)
    db.flush()
    webhooks.notify(v, v.client)
    return verification_out(v, service.required_media(v), admin=True)


@router.delete("/verifications/{vid}", status_code=204)
def erase(vid: str, db: Session = Depends(get_db)):
    v = db.get(Verification, vid)
    if v is None:
        raise HTTPException(404, "verification not found")
    service.erase(db, v, "admin")
