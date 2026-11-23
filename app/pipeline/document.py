"""Identity document analysis: capture quality, portrait, data extraction,
expiry / age rules and cross-checking against what the user typed in the app."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from rapidfuzz import fuzz

from ..config import get_settings
from . import face as facelib
from . import images, mrz, mykad, ocr
from .checks import FAIL, PASS, REVIEW, SKIPPED, check

MIN_SHORT_SIDE = 480
MIN_SHARPNESS = 12.0


@dataclass
class DocumentResult:
    checks: list[dict] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)
    face_embedding: np.ndarray | None = None
    document_number: str | None = None


def _norm_id(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _age(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _find_portrait(front: np.ndarray):
    """Try all four orientations; return (upright image, face) for the first with a face."""
    for turns in (0, 1, 3, 2):
        img = images.rotate(front, turns)
        faces = facelib.detect(img)
        if faces:
            return img, faces[0]
    return front, None


def _read_mrz(imgs: list[np.ndarray]) -> dict | None:
    best = None

    def consider(text: str) -> bool:
        nonlocal best
        found = mrz.find_mrz(text) if text else None
        if found and (best is None or found["valid_count"] > best["valid_count"]):
            best = found
        return bool(best and best["valid"])

    for img in imgs:  # primary reader, orientation independent
        if consider(ocr.read_mrz_fast(img)):
            return best
    for img in imgs:  # fallback
        for turns in (0, 2):
            if consider(ocr.read_mrz_text(images.rotate(img, turns))):
                return best
    return best


def analyze(front: np.ndarray, back: np.ndarray | None, document_type: str, country: str | None,
            claimed: dict | None, today: date | None = None) -> DocumentResult:
    s = get_settings()
    today = today or date.today()
    claimed = claimed or {}
    res = DocumentResult()
    country = (country or "").upper() or None

    # 1. capture quality -----------------------------------------------------
    q = images.quality(front)
    if min(q["width"], q["height"]) < MIN_SHORT_SIDE:
        res.checks.append(check("document_quality", FAIL, "document_resolution_too_low", **q))
    elif q["sharpness"] < MIN_SHARPNESS:
        res.checks.append(check("document_quality", FAIL, "document_blurry", **q))
    else:
        res.checks.append(check("document_quality", PASS, **q))

    # 2. portrait on the document --------------------------------------------
    front, doc_face = _find_portrait(front)
    if doc_face is None:
        res.checks.append(check("document_portrait", FAIL, "document_face_not_found"))
    else:
        res.face_embedding = facelib.embed(front, doc_face)
        res.checks.append(check("document_portrait", PASS, face_width_px=doc_face.box[2],
                                detector_score=round(doc_face.score, 3)))

    # 3. data extraction ------------------------------------------------------
    sides = [front] + ([back] if back is not None else [])
    text = ""
    definitive = False  # extracted data backed by check digits / a validated ID number
    extracted: dict = {}
    if not ocr.available():
        res.checks.append(check("document_data", REVIEW, "ocr_unavailable"))
    else:
        text = "\n".join(ocr.read_text(img) for img in sides)
        found_mrz = _read_mrz(sides[::-1]) if document_type in ("passport", "national_id") else None
        ic = mykad.find_in_text(text) if country == "MYS" and document_type != "passport" else None

        if found_mrz:
            extracted = {k: found_mrz.get(k) for k in (
                "document_number", "surname", "given_names", "date_of_birth", "expiry_date", "sex",
                "nationality", "issuing_country", "personal_number")}
            extracted["full_name"] = " ".join(x for x in (found_mrz["given_names"], found_mrz["surname"]) if x)
            extracted["source"] = "mrz"
            detail = {"mrz_format": found_mrz["format"], "check_digits": found_mrz["checks"]}
            if not found_mrz["valid"]:
                res.checks.append(check("document_data", REVIEW, "mrz_checksum_failed", **detail))
            elif document_type == "passport" and not found_mrz["document_code"].startswith("P"):
                res.checks.append(check("document_data", REVIEW, "document_type_mismatch", **detail))
            elif country and found_mrz["issuing_country"] != country:
                res.checks.append(check("document_data", REVIEW, "issuing_country_mismatch",
                                        issuing_country=found_mrz["issuing_country"], **detail))
            else:
                definitive = True
                res.checks.append(check("document_data", PASS, **detail))
        elif document_type == "passport":
            res.checks.append(check("document_data", FAIL, "mrz_not_found"))
        elif ic:
            extracted = {**ic, "source": "mykad_number"}
            definitive = True
            res.checks.append(check("document_data", PASS, id_format="MyKad"))
        elif country == "MYS" and document_type == "national_id":
            res.checks.append(check("document_data", FAIL, "id_number_not_found"))
        elif claimed.get("document_number") or claimed.get("full_name"):
            extracted = {"source": "ocr_text"}
            res.checks.append(check("document_data", PASS, note="verified against claimed data only"))
        else:
            res.checks.append(check("document_data", REVIEW, "no_machine_readable_data"))
    res.extracted = extracted
    res.document_number = extracted.get("document_number") or claimed.get("document_number")

    # 4. expiry ---------------------------------------------------------------
    exp = extracted.get("expiry_date")
    if exp:
        expired = date.fromisoformat(exp) < today
        res.checks.append(check("document_expiry", FAIL if expired else PASS, "document_expired", expiry_date=exp))
    else:
        res.checks.append(check("document_expiry", SKIPPED, "expiry_not_readable"))

    # 5. age ------------------------------------------------------------------
    dob = extracted.get("date_of_birth")
    if dob and s.min_age > 0:
        age = _age(date.fromisoformat(dob), today)
        res.checks.append(check("age", PASS if age >= s.min_age else FAIL, "underage", minimum=s.min_age))
    else:
        res.checks.append(check("age", SKIPPED, "date_of_birth_not_readable"))

    # 6. claimed data vs document ---------------------------------------------
    if claimed and text:
        res.checks.append(_match_claimed(claimed, extracted, text, definitive, s.name_match_threshold))
    return res


def _match_claimed(claimed: dict, extracted: dict, text: str, definitive: bool, name_thr: int) -> dict:
    flat = " ".join(text.upper().split())
    compared: dict = {}
    worst = PASS
    code = None

    def flag(status: str, c: str) -> None:
        nonlocal worst, code
        if status == FAIL or worst == PASS:
            worst, code = status, c

    name = (claimed.get("full_name") or "").upper().strip()
    if name:
        if extracted.get("full_name"):
            score = fuzz.token_set_ratio(name, extracted["full_name"].upper())
        else:
            score = fuzz.partial_ratio(name, flat)
        compared["full_name_score"] = round(score)
        if score < name_thr:
            flag(REVIEW, "name_mismatch")

    number = _norm_id(claimed.get("document_number") or "")
    if number:
        ex = _norm_id(extracted.get("document_number") or "")
        ok = number == ex if ex else number in _norm_id(flat)
        compared["document_number_match"] = ok
        if not ok:
            flag(FAIL if (definitive and ex) else REVIEW, "document_number_mismatch")

    dob = claimed.get("date_of_birth")
    if dob and extracted.get("date_of_birth"):
        ok = dob == extracted["date_of_birth"]
        compared["date_of_birth_match"] = ok
        if not ok:
            flag(FAIL if definitive else REVIEW, "date_of_birth_mismatch")

    return check("claimed_data", worst, code, **compared)
