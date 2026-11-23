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
from . import country as countrylib
from . import images, mrz, mykad, names, ocr
from .checks import FAIL, PASS, REVIEW, SKIPPED, check

MIN_SHORT_SIDE = 480
MIN_SHARPNESS = 12.0


@dataclass
class DocumentResult:
    checks: list[dict] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)
    face_embedding: np.ndarray | None = None
    document_number: str | None = None
    candidates: list[str] = field(default_factory=list)  # other numbers OCR read, most likely first
    name_readings: list[str] = field(default_factory=list)  # every name OCR read on the card


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
        side_texts = [ocr.read_texts(img) for img in sides]
        text = "\n".join(t for texts in side_texts for t in texts)
        requested = country
        all_texts = [t for texts in side_texts for t in texts]
        if country is None and document_type != "passport":
            # No country given: printed wording, or a confidently read NRIC
            # number, decides which reader to use.
            country = countrylib.from_wording(all_texts)[0]
            if country is None and document_type == "national_id":
                probe = mykad.read_card(side_texts[0], side_texts[1] if len(side_texts) > 1 else None)
                if probe and probe["confident"]:
                    country = "MYS"
        is_mykad = country == "MYS" and document_type == "national_id"
        # A MyKad has no machine readable zone: never look for one, or OCR noise
        # on its patterned background can pass for a broken MRZ.
        found_mrz = _read_mrz(sides[::-1]) if document_type in ("passport", "national_id") and not is_mykad else None
        card = (mykad.read_card(side_texts[0], side_texts[1] if len(side_texts) > 1 else None)
                if country == "MYS" and document_type != "passport" else None)

        if card:
            extracted = {k: card[k] for k in ("document_number", "full_name", "date_of_birth", "sex",
                                              "place_of_birth", "citizenship", "address", "postcode",
                                              "city", "state") if card.get(k)}
            extracted["source"] = "mykad"
            detail = {"id_format": "MyKad", "votes": card["votes"]}
            definitive = card["confident"]
            if (card["front_number"] and card["back_number"]
                    and card["front_number"] != card["back_number"]):
                res.checks.append(check("document_data", REVIEW, "id_number_front_back_mismatch", **detail))
            elif not card["confident"]:
                res.checks.append(check("document_data", REVIEW, "id_number_uncertain", **detail))
            elif card["printed_sex"] and card["printed_sex"] != card["sex"]:
                res.checks.append(check("document_data", REVIEW, "gender_mismatch", printed=card["printed_sex"],
                                        from_number=card["sex"], **detail))
            else:
                res.checks.append(check("document_data", PASS, **detail))
            res.candidates = card["candidates"]
            res.name_readings = card["name_readings"]
        elif is_mykad:
            res.checks.append(check("document_data", FAIL, "id_number_not_found"))
        elif found_mrz:
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
            else:
                definitive = True
                res.checks.append(check("document_data", PASS, **detail))
        elif document_type == "passport":
            res.checks.append(check("document_data", FAIL, "mrz_not_found"))
        elif claimed.get("document_number") or claimed.get("full_name"):
            extracted = {"source": "ocr_text"}
            res.checks.append(check("document_data", PASS, note="verified against claimed data only"))
        else:
            res.checks.append(check("document_data", REVIEW, "no_machine_readable_data"))
        found = countrylib.detect(all_texts, mrz_country=found_mrz["issuing_country"] if found_mrz and found_mrz["valid"] else None,
                                  mykad_confident=bool(card and card["confident"]))
        if found["country"]:
            extracted["country"] = found["country"]
            extracted["country_source"] = found["source"]
            if requested and requested != found["country"]:
                res.checks.append(check("document_country", REVIEW, "issuing_country_mismatch",
                                        requested=requested, detected=found["country"], source=found["source"]))
            else:
                res.checks.append(check("document_country", PASS, detected=found["country"], source=found["source"]))
        else:
            res.checks.append(check("document_country", SKIPPED, "country_not_identified",
                                    requested=requested))
    res.extracted = extracted
    res.document_number = extracted.get("document_number") or claimed.get("document_number")

    # 4. expiry ---------------------------------------------------------------
    exp = extracted.get("expiry_date")
    if exp:
        expired = date.fromisoformat(exp) < today
        res.checks.append(check("document_expiry", FAIL if expired else PASS, "document_expired", expiry_date=exp))
    elif extracted.get("source") == "mykad":
        res.checks.append(check("document_expiry", SKIPPED, "no_expiry_on_document"))
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
        res.checks.append(_match_claimed(claimed, extracted, text, definitive, s.name_match_threshold,
                                         res.candidates, res.name_readings))
    return res


def _match_claimed(claimed: dict, extracted: dict, text: str, definitive: bool, name_thr: int,
                   candidates: list[str] | None = None, name_readings: list[str] | None = None) -> dict:
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
        readings = list(name_readings or []) + ([extracted["full_name"]] if extracted.get("full_name") else [])
        if readings:
            ok, score = names.match(name, readings)
        else:
            score = round(fuzz.partial_ratio(name, flat))
            ok = score >= name_thr
        compared["full_name_score"] = score
        if not ok:
            flag(REVIEW, "name_mismatch")

    number = _norm_id(claimed.get("document_number") or "")
    if number:
        ex = _norm_id(extracted.get("document_number") or "")
        ok = number == ex if ex else number in _norm_id(flat)
        compared["document_number_match"] = ok
        if not ok:
            # Fail only when the reading was certain and the typed number was not
            # even among the other readings; otherwise let a person look.
            seen = number in {_norm_id(c) for c in candidates or []}
            flag(FAIL if (definitive and ex and not seen) else REVIEW, "document_number_mismatch")

    typed_address = (claimed.get("address") or "").upper().strip()
    if typed_address and extracted.get("address"):
        # Review, never reject: many people move without updating their MyKad.
        score = fuzz.token_set_ratio(typed_address, extracted["address"].upper())
        compared["address_score"] = round(score)
        typed_postcode = re.search(r"(?<!\d)\d{5}(?!\d)", typed_address)
        if typed_postcode and extracted.get("postcode"):
            compared["postcode_match"] = typed_postcode.group() == extracted["postcode"]
        if score < 70 or compared.get("postcode_match") is False:
            flag(REVIEW, "address_mismatch")

    dob = claimed.get("date_of_birth")
    if dob and extracted.get("date_of_birth"):
        ok = dob == extracted["date_of_birth"]
        compared["date_of_birth_match"] = ok
        if not ok:
            flag(FAIL if definitive else REVIEW, "date_of_birth_mismatch")

    return check("claimed_data", worst, code, **compared)
