"""Verification lifecycle: processing, duplicate detection, erasure, housekeeping."""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto, storage, webhooks
from .config import get_settings
from .db import Verification, audit, session_scope, utcnow
from .pipeline import face as facelib
from .pipeline import images, runner
from .pipeline.checks import PASS, REVIEW, check

log = logging.getLogger("ekyc.service")
FINAL = ("approved", "rejected", "review")
_process_lock = threading.Lock()  # one verification at a time: small boxes, big models


def required_media(v: Verification) -> list[str]:
    kinds = ["document_front", "selfie"]
    if v.require_liveness and v.liveness_challenge:
        kinds += [f"liveness_{i + 1}" for i in range(len(v.liveness_challenge))]
    return kinds


def allowed_media(v: Verification) -> list[str]:
    return required_media(v) + ["document_back"]


def purge_media(db: Session, v: Verification) -> None:
    for m in list(v.media):
        storage.delete(m.path)
        db.delete(m)
    v.media_purged = True


def delete_entirely(db: Session, v: Verification, actor: str) -> None:
    """Tests only: remove the verification itself, its images and data. The
    audit trail keeps one line saying it existed."""
    purge_media(db, v)
    audit(db, actor, "test.deleted", v.id)
    db.delete(v)


def erase(db: Session, v: Verification, actor: str) -> None:
    """Right-to-erasure: remove images, biometrics and extracted PII, keep the audit trail."""
    purge_media(db, v)
    v.claimed = v.result = v.face_embedding = v.doc_number_hash = None
    v.status = "deleted"
    audit(db, actor, "verification.erased", v.id)


def _duplicates(db: Session, v: Verification, embedding: np.ndarray | None, doc_hash: str | None) -> dict:
    s = get_settings()
    q = select(Verification).where(Verification.client_id == v.client_id, Verification.id != v.id,
                                   Verification.status.in_(("approved", "review")),
                                   Verification.is_test.is_(bool(v.is_test)))
    hits: list[str] = []
    code = None
    for other in db.scalars(q):
        if v.external_ref and other.external_ref == v.external_ref:
            continue  # same user re-verifying
        if doc_hash and other.doc_number_hash == doc_hash:
            hits.append(other.id)
            code = "duplicate_document"
        elif embedding is not None and other.face_embedding:
            if facelib.similarity(embedding, np.frombuffer(other.face_embedding, np.float32)) >= s.duplicate_face_threshold:
                hits.append(other.id)
                code = code or "duplicate_face"
    if hits:
        return check("duplicate", REVIEW, code, matches=hits[:10])
    return check("duplicate", PASS)


def process(verification_id: str) -> None:
    with _process_lock:
        try:
            _process(verification_id)
        except Exception:  # noqa: BLE001
            log.exception("processing crashed for %s", verification_id)


def _process(verification_id: str) -> None:
    with session_scope() as db:
        v = db.get(Verification, verification_id)
        if v is None or v.status != "processing":
            return
        media = {m.kind: storage.load(m.path, m.id) for m in v.media}
        args = (v.document_type, v.country, v.claimed, v.liveness_challenge if v.require_liveness else None)

    error = None
    outcome = None
    try:
        outcome = runner.run(media, *args)
    except images.ImageError:
        error = ("rejected", "unreadable_image")
    except Exception:  # noqa: BLE001
        log.exception("pipeline error for %s", verification_id)
        error = ("review", "processing_error")

    with session_scope() as db:
        v = db.get(Verification, verification_id)
        if v is None or v.status != "processing":
            return
        if outcome is None:
            v.status, v.reasons = error[0], [error[1]]
            v.result = {"checks": [], "extracted": {}, "scores": {}}
        else:
            doc_hash = None
            if outcome.document_number:
                kind = outcome.extracted.get("document_type") or v.document_type
                doc_hash = crypto.keyed_hash(f"{kind}:{''.join(outcome.document_number.upper().split())}")
            dup = _duplicates(db, v, outcome.selfie_embedding, doc_hash)
            status, reasons = outcome.status, list(outcome.reasons)
            if dup["status"] == REVIEW and status != "rejected":
                reasons.append(dup["code"])
                status = "review"
            v.status, v.reasons = status, reasons
            v.result = {"checks": outcome.checks + [dup], "extracted": outcome.extracted, "scores": outcome.scores}
            v.doc_number_hash = doc_hash
            if outcome.selfie_embedding is not None:
                v.face_embedding = outcome.selfie_embedding.astype(np.float32).tobytes()
        v.decided_by = "auto"
        v.completed_at = utcnow()
        audit(db, "system", f"verification.{v.status}", v.id, ",".join(v.reasons or []))
        db.flush()
        webhooks.notify(v, v.client)


def housekeeping() -> None:
    """Expire abandoned sessions and enforce the image retention period."""
    s = get_settings()
    now = utcnow()
    with session_scope() as db:
        stale = select(Verification).where(Verification.status == "created",
                                           Verification.created_at < now - timedelta(minutes=s.session_ttl_minutes))
        for v in db.scalars(stale):
            purge_media(db, v)
            v.status = "expired"
            audit(db, "system", "verification.expired", v.id)
        if s.test_retention_hours > 0:
            old_tests = select(Verification).where(Verification.is_test.is_(True),
                                                   Verification.created_at < now - timedelta(hours=s.test_retention_hours))
            for v in db.scalars(old_tests):
                delete_entirely(db, v, "system")
        if s.retention_days > 0:
            old = select(Verification).where(Verification.media_purged.is_(False),
                                             Verification.status.in_(("approved", "rejected")),
                                             Verification.completed_at < now - timedelta(days=s.retention_days))
            for v in db.scalars(old):
                purge_media(db, v)
                audit(db, "system", "media.purged", v.id)


def resume_interrupted() -> list[str]:
    """Verifications left in 'processing' by a restart."""
    with session_scope() as db:
        return list(db.scalars(select(Verification.id).where(Verification.status == "processing")))
