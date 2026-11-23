from __future__ import annotations

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import crypto, service, storage, webhooks
from ..auth import new_api_key, require_admin
from ..config import get_settings
from ..db import ApiClient, AuditLog, Media, Verification, audit, get_db, utcnow
from ..pipeline import images, mykad
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


@router.post("/try", status_code=202)
async def try_verification(
    background: BackgroundTasks,
    client_id: str = Form(...),
    document_type: str = Form("auto"),
    country: str = Form(""),
    full_name: str = Form(""),
    document_number: str = Form(""),
    address: str = Form(""),
    document_front: UploadFile = File(...),
    selfie: UploadFile = File(...),
    document_back: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """The console's Try it: one upload, run as the chosen API client, so no
    API key has to be pasted (keys are only stored hashed and cannot be listed)."""
    c = db.get(ApiClient, client_id)
    if c is None or not c.active:
        raise HTTPException(404, "client not found or revoked")
    if document_type not in ("auto", "passport", "national_id", "driving_licence"):
        raise HTTPException(422, "unknown document type")
    country = country.strip().upper() or None
    if country and len(country) != 3:
        raise HTTPException(422, "country must be ISO alpha-3, e.g. MYS")
    if document_number and country == "MYS" and document_type == "national_id" and not mykad.parse_number(document_number):
        raise HTTPException(422, "MyKad number must be 12 digits, YYMMDD-PB-####, dashes optional")
    claimed = {k: v.strip() for k, v in (("full_name", full_name), ("document_number", document_number),
                                         ("address", address)) if v.strip()} or None
    v = Verification(id="ver_" + secrets.token_hex(12), client_id=c.id, external_ref="console-test",
                     document_type=document_type, country=country, require_liveness=False,
                     liveness_challenge=None, claimed=claimed)
    db.add(v)
    db.flush()
    limit = get_settings().max_upload_mb * 1024 * 1024
    for kind, f in (("document_front", document_front), ("selfie", selfie), ("document_back", document_back)):
        if f is None or not f.filename:
            continue
        data = await f.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"{kind} is larger than {get_settings().max_upload_mb} MB")
        try:
            images.decode(data)
        except images.ImageError as exc:
            raise HTTPException(422, f"{kind}: {exc}") from exc
        mid = "med_" + secrets.token_hex(12)
        v.media.append(Media(id=mid, kind=kind, path=storage.save(v.id, mid, data), sha256=crypto.sha256_hex(data)))
    v.status = "processing"
    v.submitted_at = utcnow()
    audit(db, "admin", "verification.console_test", v.id, detail=c.id)
    db.commit()
    background.add_task(service.process, v.id)
    return {"id": v.id, "status": v.status}
