from __future__ import annotations

import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, service, storage
from ..auth import require_client
from ..config import get_settings
from ..db import ApiClient, Media, Verification, audit, get_db, utcnow
from ..pipeline import images, integrity, liveness, mykad
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


def issue_link(v: Verification, request: Request) -> dict:
    """A new hosted-verification link for `v` (any earlier one stops working).
    The token is shown once; only its hash is kept."""
    token = secrets.token_urlsafe(32)
    v.hosted_token_hash = crypto.sha256_hex(token)
    base = (get_settings().public_url or str(request.base_url)).rstrip("/")
    expires = v.created_at + timedelta(minutes=get_settings().session_ttl_minutes)
    return {"hosted_url": f"{base}/v/{token}", "hosted_url_expires_at": expires.isoformat() + "Z"}


@router.post("", status_code=201)
def create(body: CreateVerification, request: Request, client: ApiClient = Depends(require_client),
           db: Session = Depends(get_db)):
    s = get_settings()
    typed = body.claimed.document_number if body.claimed else None
    if typed and (body.country or "").upper() == "MYS" and body.document_type == "national_id":
        # Dashes and spaces are fine; the number itself must be a real NRIC.
        if not mykad.parse_number(typed):
            raise HTTPException(422, "MyKad number must be 12 digits, YYMMDD-PB-####, with a real date of "
                                     "birth and place-of-birth code (dashes optional)")
    live = s.liveness_default if body.require_liveness is None else body.require_liveness
    from ..pipeline.catalog import TYPES
    if body.document_type != "auto" and not TYPES[body.document_type].needs_selfie:
        live = False  # vehicle documents, medical reports, visas: no person to check
    v = Verification(
        id="ver_" + secrets.token_hex(12), client_id=client.id, external_ref=body.external_ref,
        document_type=body.document_type, country=body.country.upper() if body.country else None,
        require_liveness=live, liveness_challenge=liveness.new_challenge() if live else None,
        claimed=body.claimed.model_dump(mode="json", exclude_none=True) if body.claimed else None,
        is_test=body.test,
        consent=body.consent.model_dump(mode="json") if body.consent else None,
        redirect_url=body.redirect_url, created_at=utcnow(),
    )
    link = issue_link(v, request)
    db.add(v)
    audit(db, f"client:{client.id}", "verification.created", v.id,
          f"consent {body.consent.version}" if body.consent else "no consent recorded")
    db.flush()
    return {**verification_out(v, service.required_media(v)), **link}


@router.post("/{vid}/link")
def new_link(vid: str, request: Request, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    """A fresh hosted link for a verification that is still open (the old link stops working)."""
    v = _get(db, client, vid)
    _open(v)
    link = issue_link(v, request)
    audit(db, f"client:{client.id}", "verification.link_issued", v.id)
    return link


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
    return await store_media(db, v, kind, file)


async def store_media(db: Session, v: Verification, kind: str, file: UploadFile) -> dict:
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
    v.media.append(Media(id=mid, kind=kind, path=storage.save(v.id, mid, data), sha256=crypto.sha256_hex(data),
                           phash=integrity.phash(data)))
    db.flush()
    return {"kind": kind, "media_id": mid, "quality": images.quality(img),
            "uploaded_media": sorted(m.kind for m in v.media)}


@router.post("/{vid}/submit", status_code=202)
def submit(vid: str, background: BackgroundTasks, client: ApiClient = Depends(require_client),
           db: Session = Depends(get_db)):
    v = _get(db, client, vid)
    _open(v)
    return start_processing(db, v, f"client:{client.id}", background)


def start_processing(db: Session, v: Verification, actor: str, background: BackgroundTasks) -> dict:
    have = {m.kind for m in v.media}
    missing = [k for k in service.required_media(v) if k not in have]
    if missing:
        raise HTTPException(422, {"message": "missing required media", "missing": missing})
    v.status = "processing"
    v.submitted_at = utcnow()
    audit(db, actor, "verification.submitted", v.id)
    db.commit()
    background.add_task(service.process, v.id)
    return verification_out(v, service.required_media(v))


@router.delete("/{vid}", status_code=204)
def delete(vid: str, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    v = _get(db, client, vid)
    if v.status == "processing":
        raise HTTPException(409, "verification is processing, retry shortly")
    service.erase(db, v, f"client:{client.id}")


@router.post("/detect", tags=["verifications"])
async def detect_document(file: UploadFile = File(...), client: ApiClient = Depends(require_client)):
    """Upload one image of a document: get back what it is (type, country, card family)
    and whether a selfie / back side is needed. Nothing is stored."""
    from ..pipeline.detect import detect

    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"image larger than {get_settings().max_upload_mb} MB")
    try:
        return detect(data)
    except images.ImageError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/reauth", tags=["verifications"])
async def reauthenticate(external_ref: str = Form(...), selfie: UploadFile = File(...),
                         client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    """Is this the person who was verified as `external_ref`? For a driver going
    online or a random check mid-shift: the new selfie is matched against the
    face from that user's latest approved verification. Nothing is stored."""
    import numpy as np

    from ..pipeline import face as facelib
    s = get_settings()
    v = db.scalars(select(Verification).where(Verification.client_id == client.id,
                                              Verification.external_ref == external_ref,
                                              Verification.status == "approved",
                                              Verification.face_embedding.is_not(None))
                   .order_by(Verification.completed_at.desc()).limit(1)).first()
    if v is None:
        raise HTTPException(404, "no approved verification with a face for this external_ref")
    limit = s.max_upload_mb * 1024 * 1024
    data = await selfie.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "image too large")
    try:
        img = images.decode(data)
    except images.ImageError as exc:
        raise HTTPException(422, str(exc)) from exc
    faces = facelib.detect(img)
    if not faces:
        audit(db, f"client:{client.id}", "reauth.no_face", v.id)
        return {"match": False, "reason": "no_face", "verification_id": v.id}
    emb = facelib.embed(img, faces[0])
    sim = facelib.similarity(emb, np.frombuffer(v.face_embedding, np.float32))
    blocked = service.blocked_face(db, emb)
    match = sim >= s.face_match_threshold and not blocked
    audit(db, f"client:{client.id}", "reauth." + ("match" if match else "no_match"), v.id, f"{sim:.3f}")
    return {"match": match, "similarity": round(sim, 4), "threshold": s.face_match_threshold,
            "verification_id": v.id, "blocked": bool(blocked),
            "reason": None if match else ("face_blocklisted" if blocked else "face_mismatch")}
