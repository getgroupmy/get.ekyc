from __future__ import annotations

import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, service, storage
from ..auth import require_client
from ..config import get_settings
from ..db import ApiClient, Media, Verification, audit, get_db, utcnow
from ..pipeline import images, liveness, mykad
from ..schemas import CreateVerification, verification_out

router = APIRouter(prefix="/v1/verifications", tags=["verifications"])


def _get(db: Session, client: ApiClient, vid: str) -> Verification:
    v = db.get(Verification, vid)
    if v is None or v.client_id != client.id:
        raise HTTPException(404, "verification not found")
    return v


def _open(v: Verification) -> None:
    if v.status != "created":
        raise HTTPException(409, f"verification is '{v.status}', it no longer accepts changes")
    if v.created_at < utcnow() - timedelta(minutes=get_settings().session_ttl_minutes):
        raise HTTPException(410, "verification session expired, create a new one")


@router.post("", status_code=201)
def create(body: CreateVerification, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    s = get_settings()
    typed = body.claimed.document_number if body.claimed else None
    if typed and (body.country or "").upper() == "MYS" and body.document_type == "national_id":
        # Dashes and spaces are fine; the number itself must be a real NRIC.
        if not mykad.parse_number(typed):
            raise HTTPException(422, "MyKad number must be 12 digits, YYMMDD-PB-####, with a real date of "
                                     "birth and place-of-birth code (dashes optional)")
    live = s.liveness_default if body.require_liveness is None else body.require_liveness
    v = Verification(
        id="ver_" + secrets.token_hex(12), client_id=client.id, external_ref=body.external_ref,
        document_type=body.document_type, country=body.country.upper() if body.country else None,
        require_liveness=live, liveness_challenge=liveness.new_challenge() if live else None,
        claimed=body.claimed.model_dump(mode="json", exclude_none=True) if body.claimed else None,
        is_test=body.test,
    )
    db.add(v)
    audit(db, f"client:{client.id}", "verification.created", v.id)
    db.flush()
    return verification_out(v, service.required_media(v))


@router.get("")
def list_(external_ref: str | None = None, status: str | None = None, limit: int = 50,
          client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    q = select(Verification).where(Verification.client_id == client.id, Verification.status != "deleted")
    if external_ref:
        q = q.where(Verification.external_ref == external_ref)
    if status:
        q = q.where(Verification.status == status)
    rows = db.scalars(q.order_by(Verification.created_at.desc()).limit(max(1, min(limit, 200))))
    return {"items": [verification_out(v, service.required_media(v)) for v in rows]}


@router.get("/{vid}")
def get(vid: str, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    v = _get(db, client, vid)
    return verification_out(v, service.required_media(v))


@router.put("/{vid}/media/{kind}")
async def upload(vid: str, kind: str, file: UploadFile = File(...),
                 client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    v = _get(db, client, vid)
    _open(v)
    if kind not in service.allowed_media(v) or not re.fullmatch(r"[a-z_0-9]+", kind):
        raise HTTPException(422, f"kind must be one of {service.allowed_media(v)}")
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"image larger than {get_settings().max_upload_mb} MB")
    try:
        img = images.decode(data)
    except images.ImageError as exc:
        raise HTTPException(422, str(exc)) from exc
    for old in [m for m in v.media if m.kind == kind]:
        storage.delete(old.path)
        v.media.remove(old)
    db.flush()
    mid = "med_" + secrets.token_hex(12)
    v.media.append(Media(id=mid, kind=kind, path=storage.save(v.id, mid, data), sha256=crypto.sha256_hex(data)))
    db.flush()
    return {"kind": kind, "quality": images.quality(img), "uploaded_media": sorted(m.kind for m in v.media)}


@router.post("/{vid}/submit", status_code=202)
def submit(vid: str, background: BackgroundTasks, client: ApiClient = Depends(require_client),
           db: Session = Depends(get_db)):
    v = _get(db, client, vid)
    _open(v)
    have = {m.kind for m in v.media}
    missing = [k for k in service.required_media(v) if k not in have]
    if missing:
        raise HTTPException(422, {"message": "missing required media", "missing": missing})
    v.status = "processing"
    v.submitted_at = utcnow()
    audit(db, f"client:{client.id}", "verification.submitted", v.id)
    db.commit()
    background.add_task(service.process, v.id)
    return verification_out(v, service.required_media(v))


@router.delete("/{vid}", status_code=204)
def delete(vid: str, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    v = _get(db, client, vid)
    if v.status == "processing":
        raise HTTPException(409, "verification is processing, retry shortly")
    service.erase(db, v, f"client:{client.id}")
