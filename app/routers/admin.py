from __future__ import annotations

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import crypto, service, storage, webhooks
from ..auth import new_api_key, require_admin
from ..config import get_settings
from ..db import ApiClient, AuditLog, Media, SampleDoc, Verification, audit, get_db, utcnow
from ..pipeline import catalog, images, integrity, mykad
from ..pipeline.detect import detect as detect_document
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


def _usage(db: Session) -> dict[str, dict]:
    rows = db.execute(select(Verification.client_id, Verification.is_test, Verification.status, func.count())
                      .group_by(Verification.client_id, Verification.is_test, Verification.status)).all()
    out: dict[str, dict] = {}
    for cid, is_test, status, n in rows:
        u = out.setdefault(cid, {"uses": 0, "approved": 0, "rejected": 0, "review": 0, "tests": 0})
        if is_test:
            u["tests"] += n
            continue
        u["uses"] += n
        if status in ("approved", "rejected", "review"):
            u[status] += n
    return out


@router.get("/clients")
def list_clients(db: Session = Depends(get_db)):
    usage = _usage(db)
    empty = {"uses": 0, "approved": 0, "rejected": 0, "review": 0, "tests": 0}
    items = []
    for c in db.scalars(select(ApiClient).order_by(ApiClient.created_at)):
        u = usage.get(c.id, empty)
        items.append({**_client_out(c), **u, "deletable": not c.active and u["uses"] == 0 and u["tests"] == 0})
    return {"items": items}


@router.delete("/clients/{cid}/permanent", status_code=204)
def delete_client(cid: str, db: Session = Depends(get_db)):
    """Only a revoked key that was never used: its verifications would otherwise
    lose the record of which app sent them."""
    c = db.get(ApiClient, cid)
    if c is None:
        raise HTTPException(404, "client not found")
    if c.active:
        raise HTTPException(409, "revoke the key first")
    if db.scalar(select(func.count()).select_from(Verification).where(Verification.client_id == cid)):
        raise HTTPException(409, "this key has been used; it stays, revoked, as a record")
    db.delete(c)
    audit(db, "admin", "client.deleted", detail=cid)


@router.delete("/clients/{cid}", status_code=204)
def revoke_client(cid: str, db: Session = Depends(get_db)):
    c = db.get(ApiClient, cid)
    if c is None:
        raise HTTPException(404, "client not found")
    c.active = False
    audit(db, "admin", "client.revoked", detail=c.id)


@router.get("/stats")
def stats(kind: str = "real", db: Session = Depends(get_db)):
    q = select(Verification.status, func.count()).group_by(Verification.status)
    if kind in ("test", "real"):
        q = q.where(Verification.is_test.is_(kind == "test"))
    rows = db.execute(q).all()
    tests = db.scalar(select(func.count()).select_from(Verification).where(Verification.is_test.is_(True)))
    return {"by_status": {status: n for status, n in rows}, "tests": tests,
            "test_retention_hours": get_settings().test_retention_hours}


@router.delete("/tests", status_code=200)
def delete_all_tests(db: Session = Depends(get_db)):
    n = 0
    for v in db.scalars(select(Verification).where(Verification.is_test.is_(True))):
        service.delete_entirely(db, v, "admin")
        n += 1
    return {"deleted": n}


@router.post("/verifications/{vid}/test")
def mark_test(vid: str, test: bool = True, db: Session = Depends(get_db)):
    """Mark a verification as a test (or back to real), e.g. a staff run made through the API."""
    v = db.get(Verification, vid)
    if v is None:
        raise HTTPException(404, "verification not found")
    v.is_test = test
    audit(db, "admin", "verification.marked_" + ("test" if test else "real"), v.id)
    return {"id": v.id, "test": v.is_test}


@router.get("/verifications")
def list_verifications(status: str | None = None, kind: str = "all", limit: int = 100, db: Session = Depends(get_db)):
    q = select(Verification).where(Verification.status != "deleted")
    if status:
        q = q.where(Verification.status == status)
    if kind in ("test", "real"):
        q = q.where(Verification.is_test.is_(kind == "test"))
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
    """A test is deleted entirely; a real verification is erased (images and
    personal data removed, the record and audit trail kept)."""
    v = db.get(Verification, vid)
    if v is None:
        raise HTTPException(404, "verification not found")
    if v.is_test:
        service.delete_entirely(db, v, "admin")
    else:
        service.erase(db, v, "admin")


SLOTS = ("document_front", "document_back", "selfie")


async def _read_upload(f: UploadFile | None, kind: str) -> bytes | None:
    if f is None or not f.filename:
        return None
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await f.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{kind} is larger than {get_settings().max_upload_mb} MB")
    try:
        images.decode(data)
    except images.ImageError as exc:
        raise HTTPException(422, f"{kind}: {exc}") from exc
    return data


def _sample_bytes(db: Session, sample_id: str, kind: str) -> bytes:
    sd = db.get(SampleDoc, sample_id)
    if sd is None:
        raise HTTPException(404, f"saved {kind} not found")
    return storage.load(sd.path, sd.id)


# --- saved sample documents ------------------------------------------------------------
def _sample_out(sd: SampleDoc) -> dict:
    return {"id": sd.id, "label": sd.label, "kind": sd.kind, "is_default": sd.is_default,
            "created_at": sd.created_at.isoformat() + "Z"}


@router.get("/samples")
def list_samples(db: Session = Depends(get_db)):
    return {"items": [_sample_out(x) for x in db.scalars(select(SampleDoc).order_by(SampleDoc.kind, SampleDoc.created_at))]}


@router.post("/samples", status_code=201)
async def add_sample(label: str = Form(...), kind: str = Form(...), make_default: bool = Form(False),
                     file: UploadFile = File(...), db: Session = Depends(get_db)):
    if kind not in SLOTS:
        raise HTTPException(422, f"kind must be one of {SLOTS}")
    data = await _read_upload(file, kind)
    if data is None:
        raise HTTPException(422, "no file")
    sid = "smp_" + secrets.token_hex(10)
    sd = SampleDoc(id=sid, label=label.strip()[:120] or kind, kind=kind, path=storage.save("samples", sid, data))
    db.add(sd)
    if make_default:
        _make_default(db, sd)
    audit(db, "admin", "sample.added", detail=sid)
    db.flush()
    return _sample_out(sd)


def _make_default(db: Session, sd: SampleDoc) -> None:
    for other in db.scalars(select(SampleDoc).where(SampleDoc.kind == sd.kind)):
        other.is_default = other.id == sd.id


@router.post("/samples/{sid}/default")
def set_default_sample(sid: str, db: Session = Depends(get_db)):
    sd = db.get(SampleDoc, sid)
    if sd is None:
        raise HTTPException(404, "sample not found")
    _make_default(db, sd)
    return _sample_out(sd)


@router.delete("/samples/{sid}", status_code=204)
def delete_sample(sid: str, db: Session = Depends(get_db)):
    sd = db.get(SampleDoc, sid)
    if sd is None:
        raise HTTPException(404, "sample not found")
    storage.delete(sd.path)
    db.delete(sd)
    audit(db, "admin", "sample.deleted", detail=sid)


@router.get("/samples/{sid}/image")
def sample_image(sid: str, db: Session = Depends(get_db)):
    sd = db.get(SampleDoc, sid)
    if sd is None:
        raise HTTPException(404, "sample not found")
    data = storage.load(sd.path, sd.id)
    kind = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"
    return Response(data, media_type=kind, headers={"Cache-Control": "no-store"})


# --- detect only (nothing stored) ----------------------------------------------------------
@router.post("/detect")
async def detect(file: UploadFile | None = File(None), sample_id: str = Form(""), db: Session = Depends(get_db)):
    data = await _read_upload(file, "document") if file else (_sample_bytes(db, sample_id, "document") if sample_id else None)
    if data is None:
        raise HTTPException(422, "send a file or a sample_id")
    return detect_document(data)


# --- the console's Try it ---------------------------------------------------------------------
@router.post("/try", status_code=202)
async def try_verification(
    background: BackgroundTasks,
    client_id: str = Form(...),
    document_type: str = Form("auto"),
    country: str = Form(""),
    full_name: str = Form(""),
    document_number: str = Form(""),
    address: str = Form(""),
    plate: str = Form(""),
    document_front: UploadFile | None = File(None),
    document_back: UploadFile | None = File(None),
    selfie: UploadFile | None = File(None),
    document_front_sample: str = Form(""),
    document_back_sample: str = Form(""),
    selfie_sample: str = Form(""),
    db: Session = Depends(get_db),
):
    """Run as the chosen API client (keys are stored hashed and cannot be listed),
    with uploaded images or saved samples. Always a test."""
    c = db.get(ApiClient, client_id)
    if c is None or not c.active:
        raise HTTPException(404, "client not found or revoked")
    if document_type != "auto" and document_type not in catalog.TYPES:
        raise HTTPException(422, "unknown document type")
    country = country.strip().upper() or None
    if country and len(country) != 3:
        raise HTTPException(422, "country must be ISO alpha-3, e.g. MYS")
    if document_number and country == "MYS" and document_type == "national_id" and not mykad.parse_number(document_number):
        raise HTTPException(422, "MyKad number must be 12 digits, YYMMDD-PB-####, dashes optional")
    media: dict[str, bytes] = {}
    uploads = {"document_front": document_front, "document_back": document_back, "selfie": selfie}
    picks = {"document_front": document_front_sample, "document_back": document_back_sample, "selfie": selfie_sample}
    for kind in SLOTS:
        data = await _read_upload(uploads[kind], kind)
        if data is None and picks[kind]:
            data = _sample_bytes(db, picks[kind], kind)
        if data is not None:
            media[kind] = data
    if "document_front" not in media:
        raise HTTPException(422, "a document front is needed (upload one or pick a saved one)")
    claimed = {k: v.strip() for k, v in (("full_name", full_name), ("document_number", document_number),
                                         ("address", address), ("plate", plate)) if v.strip()} or None
    v = Verification(id="ver_" + secrets.token_hex(12), client_id=c.id, external_ref="console-test",
                     document_type=document_type, country=country, require_liveness=False,
                     liveness_challenge=None, claimed=claimed, is_test=True)
    db.add(v)
    db.flush()
    for kind, data in media.items():
        mid = "med_" + secrets.token_hex(12)
        v.media.append(Media(id=mid, kind=kind, path=storage.save(v.id, mid, data), sha256=crypto.sha256_hex(data),
                           phash=integrity.phash(data)))
    v.status = "processing"
    v.submitted_at = utcnow()
    audit(db, "admin", "verification.console_test", v.id, detail=c.id)
    db.commit()
    background.add_task(service.process, v.id)
    return {"id": v.id, "status": v.status}
