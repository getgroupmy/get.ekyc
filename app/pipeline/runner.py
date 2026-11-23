"""Runs every check for one verification and produces the decision."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import get_settings


def compat_scale(cosine: float, threshold: float) -> float:
    """0..1 on face-server's scale: the threshold shows as 0.80."""
    if cosine >= threshold:
        return 0.8 + (cosine - threshold) / (1 - threshold) * 0.2
    return max(0.0, cosine / threshold * 0.8)
from . import catalog, document, images, integrity, liveness
from . import face as facelib
from .checks import FAIL, PASS, REVIEW, SKIPPED, check, overall


@dataclass
class Outcome:
    status: str
    reasons: list[str]
    checks: list[dict]
    extracted: dict
    scores: dict
    selfie_embedding: np.ndarray | None
    document_number: str | None


def run(media: dict[str, bytes], document_type: str, country: str | None, claimed: dict | None,
        challenge: list[str] | None) -> Outcome:
    s = get_settings()
    checks: list[dict] = []
    scores: dict = {}

    def img(kind: str) -> np.ndarray | None:
        return images.decode(media[kind]) if kind in media else None

    front, back, selfie = img("document_front"), img("document_back"), img("selfie")
    assert front is not None

    doc = document.analyze(front, back, document_type, country, claimed)
    checks += doc.checks
    meta = catalog.TYPES.get(doc.extracted.get("document_type", document_type))
    needs_selfie = bool(meta and meta.needs_selfie)

    # liveness frames (decoded once, used three ways) ---------------------------
    frames = [images.decode(media[f"liveness_{i + 1}"]) for i in range(len(challenge or []))
              if f"liveness_{i + 1}" in media]
    center_frame = None
    if challenge and len(frames) == len(challenge) and "center" in challenge:
        img_c = frames[challenge.index("center")]
        fc = facelib.detect(img_c)
        center_frame = (img_c, fc[0]) if fc else None

    # selfie ------------------------------------------------------------------
    selfie_emb = None
    faces = facelib.detect(selfie) if selfie is not None else []
    if selfie is None:
        if needs_selfie:
            checks.append(check("selfie_quality", REVIEW, "selfie_missing"))
    elif not faces:
        checks.append(check("selfie_quality", FAIL, "selfie_no_face"))
    else:
        main = faces[0]
        selfie_emb = facelib.embed(selfie, main)
        fq = facelib.quality(selfie, main)
        scores["selfie_quality"] = fq
        detail = {"face_area_ratio": round(main.area_ratio, 3), **{k: v for k, v in fq.items() if k != "issues"}}
        if main.area_ratio < 0.03:
            checks.append(check("selfie_quality", FAIL, "selfie_face_too_small", **detail))
        elif len(faces) > 1 and faces[1].area_ratio > 0.3 * main.area_ratio:
            checks.append(check("selfie_quality", REVIEW, "selfie_multiple_faces", **detail))
        elif fq["issues"]:
            checks.append(check("selfie_quality", REVIEW, "selfie_quality_low", issues=fq["issues"], **detail))
        else:
            checks.append(check("selfie_quality", PASS, **detail))
        passive = liveness.passive([(selfie, main)] + ([center_frame] if center_frame else []))
        if passive:
            if "margin" in passive["detail"]:
                scores["liveness_margin"] = passive["detail"]["margin"]
            checks.append(passive)

    # selfie vs document portrait -----------------------------------------------
    if selfie_emb is not None and doc.face_embedding is not None:
        sim = facelib.similarity(selfie_emb, doc.face_embedding)
        source = "selfie"
        if center_frame is not None:  # the straight-on liveness frame is a second, live sample
            sim_c = facelib.similarity(facelib.embed(*center_frame), doc.face_embedding)
            if sim_c > sim:
                sim, source = sim_c, "liveness_frame"
        scores["face_match"] = round(sim, 4)
        scores["face_score"] = round(100 * compat_scale(sim, s.face_match_threshold))
        detail = {"similarity": round(sim, 4), "threshold": s.face_match_threshold, "best_of": source,
                  "id_photo_px": doc.portrait_px}
        copy = integrity.selfie_copy_check(facelib.similarity(selfie_emb, doc.face_embedding))
        if copy:
            checks.append(copy)
        if sim >= s.face_match_threshold:
            checks.append(check("face_match", PASS, **detail))
        elif sim >= s.face_review_threshold:
            checks.append(check("face_match", REVIEW, "face_match_uncertain", **detail))
        else:
            checks.append(check("face_match", FAIL, "face_mismatch", **detail))
    elif needs_selfie or selfie is not None:
        checks.append(check("face_match", SKIPPED, "face_missing"))

    # active liveness --------------------------------------------------------------
    if challenge:
        checks.append(liveness.active(frames, challenge, selfie_emb))

    checks.append(integrity.edited_check(media))
    status, reasons = overall(checks, s.auto_approve)
    return Outcome(status, reasons, checks, doc.extracted, scores, selfie_emb, doc.document_number)
