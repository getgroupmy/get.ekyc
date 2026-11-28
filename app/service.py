"""Verification lifecycle: processing, duplicate detection, erasure, housekeeping."""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto, sanctions, storage, webhooks
from .config import get_settings
from .db import Media, Verification, audit, session_scope, utcnow
from .pipeline import face as facelib
from .pipeline import images, runner
from .pipeline import integrity
from .pipeline.checks import PASS, REVIEW, check

log = logging.getLogger("ekyc.service")
FINAL = ("approved", "rejected", "review")
_process_lock = threading.Lock()  # one verification at a time: small boxes, big models


_version_cache: list = []


def _engine_version() -> str | None:
    """The get.ekyc commit that made this decision (see the console's Changelog)."""
    if not _version_cache:
        from . import system
        _version_cache.append(system.version().get("commit"))
    return _version_cache[0]


def required_media(v: Verification) -> list[str]:
    from .pipeline.catalog import TYPES
    meta = TYPES.get(v.document_type)
    # auto: the selfie is optional when uploading; identity documents without one go to review
    kinds = ["document_front"] + (["selfie"] if meta and meta.needs_selfie else [])
    if v.require_liveness and v.liveness_challenge:
        kinds += [f"liveness_{i + 1}" for i in range(len(v.liveness_challenge))]
    return kinds


def allowed_media(v: Verification) -> list[str]:
    extra = ["document_back"] + ([] if "selfie" in required_media(v) else ["selfie"])
    return required_media(v) + extra


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


def _details_differ(a: dict, b: dict) -> bool:
    from rapidfuzz import fuzz
    if a.get("date_of_birth") and b.get("date_of_birth") and a["date_of_birth"] != b["date_of_birth"]:
        return True
    na, nb = (a.get("full_name") or "").upper(), (b.get("full_name") or "").upper()
    return bool(na and nb and fuzz.token_set_ratio(na, nb) < 80)


def _sanctions_check(extracted: dict) -> dict | None:
    """People only (identity and driver documents with a name)."""
    if not get_settings().sanctions_enabled or extracted.get("document_category") == "vehicle":
        return None
    if not extracted.get("full_name"):
        return None
    if not sanctions.entries():
        return check("sanctions", "skipped", "sanctions_list_unavailable")
    hits = sanctions.screen(extracted["full_name"], extracted.get("date_of_birth"))
    if hits:
        return check("sanctions", REVIEW, "sanctions_possible_match", matches=hits)
    return check("sanctions", PASS, lists=sorted({e.source for e in sanctions.entries()}))


def rescreen_all() -> int:
    """After a list update: screen every approved real verification again (BNM AML/CFT 27)."""
    n = 0
    with session_scope() as db:
        for v in db.scalars(select(Verification).where(Verification.status == "approved",
                                                       Verification.is_test.is_(False))):
            ex = (v.result or {}).get("extracted") or {}
            hits = sanctions.screen(ex.get("full_name"), ex.get("date_of_birth")) if ex.get("full_name") else []
            if hits:
                v.status, v.decided_by = "review", "sanctions_rescreen"
                v.reasons = list(v.reasons or []) + ["sanctions_possible_match"]
                v.result = {**v.result, "checks": v.result.get("checks", []) +
                            [check("sanctions", REVIEW, "sanctions_possible_match", matches=hits, rescreen=True)]}
                audit(db, "system", "sanctions.rescreen_hit", v.id)
                db.flush()
                webhooks.notify(v, v.client)
                n += 1
    return n


def blocked_face(db: Session, embedding: np.ndarray | None) -> dict | None:
    """A match against the blocklist, across all clients."""
    from .db import BlockedFace
    if embedding is None:
        return None
    s = get_settings()
    best = None
    for b in db.scalars(select(BlockedFace)):
        sim = facelib.similarity(embedding, np.frombuffer(b.embedding, np.float32))
        if sim >= s.duplicate_face_threshold and (best is None or sim > best[0]):
            best = (sim, b)
    if best:
        return {"blocked_id": best[1].id, "similarity": round(best[0], 4), "reason": best[1].reason}
    return None


def _duplicates(db: Session, v: Verification, embedding: np.ndarray | None, doc_hash: str | None,
                details: dict | None = None) -> dict:
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
            # The same ID number with a different name or date of birth: an edited copy.
            if details and _details_differ(details, (other.result or {}).get("extracted") or {}):
                code = "document_details_changed"
            elif code != "document_details_changed":
                code = "duplicate_document"
        elif embedding is not None and other.face_embedding:
            if facelib.similarity(embedding, np.frombuffer(other.face_embedding, np.float32)) >= s.duplicate_face_threshold:
                hits.append(other.id)
                code = code or "duplicate_face"
    # the same photo already used by someone else (resized or recompressed counts)
    mine = {m.kind: m.phash for m in v.media if m.phash}
    if mine:
        others = db.execute(select(Media.phash, Media.kind, Verification.id, Verification.external_ref)
                            .join(Verification, Media.verification_id == Verification.id)
                            .where(Verification.client_id == v.client_id, Verification.id != v.id,
                                   Verification.is_test.is_(bool(v.is_test)), Media.phash.is_not(None))).all()
        for ph, kind, oid, oref in others:
            if v.external_ref and oref == v.external_ref:
                continue
            if any(integrity.hamming(ph, h) <= integrity.SAME_IMAGE for h in mine.values()):
                if oid not in hits:
                    hits.append(oid)
                code = code or "image_reused"
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
            dup = _duplicates(db, v, outcome.selfie_embedding, doc_hash, outcome.extracted)
            status, reasons = outcome.status, list(outcome.reasons)
            if dup["status"] == REVIEW and status != "rejected":
                reasons.append(dup["code"])
                status = "review"
            blocked = None if v.is_test else blocked_face(db, outcome.selfie_embedding)
            extra = [dup]
            if blocked:
                extra.append(check("blocklist", "fail", "face_blocklisted", **blocked))
                status = "rejected"
                reasons = ["face_blocklisted"] + [r for r in reasons if r != "manual_approval_required"]
            v.status, v.reasons = status, reasons
            scr = _sanctions_check(outcome.extracted) if not v.is_test else None
            if scr:
                extra.append(scr)
                if scr["status"] == REVIEW and status == "approved":
                    status = "review"
                if scr["status"] == REVIEW:
                    reasons.append(scr["code"])
            v.result = {"checks": outcome.checks + extra, "extracted": outcome.extracted, "scores": outcome.scores,
                        "engine_version": _engine_version()}
            v.doc_number_hash = doc_hash
            if outcome.selfie_embedding is not None:
                v.face_embedding = outcome.selfie_embedding.astype(np.float32).tobytes()
        v.decided_by = "auto"
        v.completed_at = utcnow()
        audit(db, "system", f"verification.{v.status}", v.id, ",".join(v.reasons or []))
        db.flush()
        webhooks.notify(v, v.client)


def housekeeping() -> None:
    """Expire abandoned sessions, enforce retention, refresh the sanctions list."""
    s = get_settings()
    try:
        if sanctions.refresh():
            hits = rescreen_all()
            log.info("sanctions list updated; %d verification(s) sent to review", hits)
    except Exception:  # noqa: BLE001
        log.exception("sanctions refresh failed")
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
            # never before the improvement set's days are over
            keep = max(s.retention_days, s.improvement_days)
            old = select(Verification).where(Verification.media_purged.is_(False),
                                             Verification.status.in_(("approved", "rejected")),
                                             Verification.completed_at < now - timedelta(days=keep))
            for v in db.scalars(old):
                purge_media(db, v)
                audit(db, "system", "media.purged", v.id)


def resume_interrupted() -> list[str]:
    """Verifications left in 'processing' by a restart."""
    with session_scope() as db:
        return list(db.scalars(select(Verification.id).where(Verification.status == "processing")))
