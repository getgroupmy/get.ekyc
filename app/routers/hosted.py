"""Hosted verification: get.ekyc's own capture page, like other eKYC providers.

The hand-over, both ways:
  1. Your server creates a verification (API key) and gets a one-time
     `hosted_url`, then sends the user there (browser, in-app browser, webview).
  2. On that page the user agrees to the notice, photographs the document,
     takes a selfie and does the liveness turns. get.ekyc checks everything.
  3. get.ekyc sends the user back to your `redirect_url` with the verification
     id, and your server gets the signed webhook with the result.

The link's token is the only credential here: it opens one verification, only
while that verification is open, and nothing on this page reveals what was
read from the document.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from datetime import timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, service, storage
from ..config import get_settings
from ..db import Verification, audit, get_db, utcnow
from ..pipeline.catalog import TYPES
from .verifications import start_processing, store_media

router = APIRouter(prefix="/v1/hosted", tags=["hosted"])

CONSENT_VERSION = "hosted-2026-11"
CONSENT_TEXT = {
    "en": ("{client} uses GET eKYC to confirm who you are. You will photograph your identity document and "
           "take a selfie with a few head turns. We read the details on the document, compare your face with "
           "its photo, check that a live person is present, and screen against sanctions lists. Your photos, "
           "face data and details are encrypted, used only for this check and for fraud prevention, kept only "
           "as long as the law requires, and shared only with {client}."),
    "ms": ("{client} menggunakan GET eKYC untuk mengesahkan identiti anda. Anda akan mengambil gambar dokumen "
           "pengenalan dan swafoto sambil memusingkan kepala. Kami membaca butiran pada dokumen, membandingkan "
           "wajah anda dengan gambarnya, memastikan orang sebenar hadir, dan menyemak senarai sekatan. Gambar, "
           "data wajah dan butiran anda disulitkan, digunakan hanya untuk semakan ini dan pencegahan penipuan, "
           "disimpan hanya selama yang dikehendaki undang-undang, dan dikongsi hanya dengan {client}."),
}

# What the front of the document turned out to be, by media id (detection takes seconds).
_detected: OrderedDict[str, dict] = OrderedDict()


def _remember(mid: str, det: dict) -> None:
    _detected[mid] = det
    while len(_detected) > 500:
        _detected.popitem(last=False)


def _by_token(db: Session, token: str) -> Verification:
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,80}", token):
        raise HTTPException(404, "this link is not valid")
    v = db.scalar(select(Verification).where(Verification.hosted_token_hash == crypto.sha256_hex(token)))
    if v is None or v.status == "deleted":
        raise HTTPException(404, "this link is not valid")
    return v


def _expires(v: Verification):  # noqa: ANN202
    return v.created_at + timedelta(minutes=get_settings().session_ttl_minutes)


def _expired(v: Verification) -> bool:
    return v.status == "created" and utcnow() > _expires(v)


def _open(v: Verification) -> None:
    if v.status != "created":
        raise HTTPException(409, "this verification has already been sent")
    if _expired(v):
        raise HTTPException(410, "this link has expired")


def return_url(v: Verification, state: str) -> str | None:
    """Where the user goes back to, with what happened. A hint for the app's
    screen only: the result itself comes from the webhook or GET /v1/verifications/{id}."""
    if not v.redirect_url:
        return None
    parts = urlsplit(v.redirect_url)
    extra = {"verification_id": v.id, "status": state}
    if v.external_ref:
        extra["external_ref"] = v.external_ref
    query = (parts.query + "&" if parts.query else "") + urlencode(extra)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _consent_text(v: Verification, lang: str) -> str:
    return CONSENT_TEXT[lang].format(client=v.client.name if v.client else "This app")


@router.get("/{token}")
def session_info(token: str, db: Session = Depends(get_db)):
    v = _by_token(db, token)
    meta = TYPES.get(v.document_type)
    state = "expired" if _expired(v) else v.status
    return {
        "verification_id": v.id,
        "client": v.client.name if v.client else None,
        "state": state,
        "test": bool(v.is_test),
        "expires_at": _expires(v).isoformat() + "Z",
        "document_type": v.document_type,
        "document_name": meta.name if meta else None,
        "needs_selfie": meta.needs_selfie if meta else None,  # None = depends on the document (auto)
        "liveness_challenge": v.liveness_challenge if v.require_liveness else None,
        "consent_needed": v.consent is None,
        "consent_text": {lang: _consent_text(v, lang) for lang in CONSENT_TEXT},
        "uploaded": sorted(m.kind for m in v.media),
        "has_return": bool(v.redirect_url),
        "return_url": return_url(v, state) if state != "created" else None,
        "cancel_url": return_url(v, "cancelled"),
    }


class ConsentIn(BaseModel):
    accepted: bool
    lang: str = "en"


@router.post("/{token}/consent")
def give_consent(token: str, body: ConsentIn, db: Session = Depends(get_db)):
    v = _by_token(db, token)
    _open(v)
    if not body.accepted:
        raise HTTPException(422, "consent is needed to continue")
    lang = body.lang if body.lang in CONSENT_TEXT else "en"
    if v.consent is None:
        text = _consent_text(v, lang)
        v.consent = {"version": CONSENT_VERSION, "accepted_at": utcnow().isoformat() + "Z", "via": "hosted",
                     "lang": lang, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
        audit(db, "hosted", "verification.consent", v.id, f"{CONSENT_VERSION} {lang}")
    return {"ok": True}


def _summary(det: dict) -> dict:
    out = {k: det.get(k) for k in ("name", "category", "needs_selfie", "needs_back", "readable", "not_document")}
    if out["not_document"]:
        out["name"] = None  # nothing was recognised; do not name a guess
    if det.get("subtype") and det.get("document_type") == "national_id":
        out["name"] = det["subtype"]  # "MyKad", "MyPR", "NRIC/FIN": what the person holds
    return out


@router.put("/{token}/media/{kind}")
async def upload(token: str, kind: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    v = _by_token(db, token)
    _open(v)
    if v.consent is None:
        raise HTTPException(428, "accept the notice first")
    out = await store_media(db, v, kind, file)
    db.commit()
    resp = {"kind": kind, "uploaded": out["uploaded_media"]}
    if kind == "document_front":
        from ..pipeline.detect import detect
        data = storage.load(next(m.path for m in v.media if m.id == out["media_id"]), out["media_id"])
        try:
            det = await run_in_threadpool(detect, data)
        except Exception:  # noqa: BLE001 - detection is a convenience; the full check runs on submit
            det = {}
        _remember(out["media_id"], det)
        resp["document"] = _summary(det)
    return resp


@router.post("/{token}/submit", status_code=202)
async def submit(token: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    v = _by_token(db, token)
    _open(v)
    if v.consent is None:
        raise HTTPException(428, "accept the notice first")
    front = next((m for m in v.media if m.kind == "document_front"), None)
    if front is None:
        raise HTTPException(422, {"message": "missing required media", "missing": ["document_front"]})
    if v.document_type == "auto" and v.require_liveness:
        # A document with no person on it (vehicle papers…) needs no selfie or turns.
        # Decided from the front actually stored, so it cannot be swapped afterwards.
        det = _detected.get(front.id)
        if det is None:
            from ..pipeline.detect import detect
            try:
                det = await run_in_threadpool(detect, storage.load(front.path, front.id))
            except Exception:  # noqa: BLE001
                det = {}
            _remember(front.id, det)
        if det.get("needs_selfie") is False:
            v.require_liveness = False
    if v.document_type == "auto" and v.require_liveness and "selfie" not in {m.kind for m in v.media}:
        raise HTTPException(422, {"message": "missing required media", "missing": ["selfie"]})
    out = start_processing(db, v, "hosted", background)
    return {"state": out["status"]}


class EventIn(BaseModel):
    step: str
    detail: str | None = None


@router.post("/{token}/event", status_code=204)
def event(token: str, body: EventIn, db: Session = Depends(get_db)):
    """The page says which step the person reached (opened, consent, camera
    open or failed, uploaded, submit). Kept in the verification's audit trail
    so a check that stalls on a phone can be traced. At most 80 per check."""
    from sqlalchemy import func as _f

    from ..db import AuditLog
    v = _by_token(db, token)
    if not re.fullmatch(r"[a-z_]{2,40}", body.step):
        raise HTTPException(422, "bad step")
    n = db.scalar(select(_f.count()).select_from(AuditLog)
                  .where(AuditLog.verification_id == v.id, AuditLog.action.like("hosted.%")))
    if n >= 80:
        return
    detail = re.sub(r"[^\w .:/()-]", "", body.detail or "")[:200] or None
    audit(db, "hosted", f"hosted.{body.step}", v.id, detail)


@router.get("/{token}/status")
def status(token: str, db: Session = Depends(get_db)):
    """Only the state, never what was read: the page uses it to say goodbye."""
    v = _by_token(db, token)
    state = "expired" if _expired(v) else v.status
    final = state in service.FINAL or state == "expired"
    return {"state": state, "finished": final,
            "return_url": return_url(v, state if final else "submitted") if state != "created" else None}
