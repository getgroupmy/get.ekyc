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
from . import catalog, fields, idnumbers
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
    portrait_px: int | None = None  # size of the face on the document


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


def _looks_like_mrz(texts: list[str]) -> bool:
    """A machine readable zone shows up in ordinary OCR as long runs of capitals,
    digits and '<' fillers. Without that, the slow Tesseract MRZ pass is skipped."""
    # '<' fillers are the tell: a MyKad's security print (MALAYSIAKERAJAAN...) is
    # just as long, but has none.
    return any(ln.count("<") >= 4 or re.search(r"[A-Z0-9]<<", ln) for t in texts for ln in t.upper().splitlines())


def _read_mrz(imgs: list[np.ndarray], texts: list[str] | None = None) -> dict | None:
    """fastmrz first (any orientation, ~0.5 s); Tesseract with the OCR-B model
    only when fastmrz found nothing valid and the page shows MRZ-like lines."""
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
    if texts is not None and not best and not _looks_like_mrz(texts):
        return None
    for img in imgs:  # fallback
        for turns in (0, 2):
            if consider(ocr.read_mrz_text(images.rotate(img, turns))):
                return best
    return best


def _detect_type(sides: list[np.ndarray], texts: list[str]) -> tuple[str, str, dict | None]:
    """(document type, how it was decided, the MRZ if one was read).
    An MRZ names the type outright; otherwise the printed wording of driver and
    vehicle documents; otherwise an identity card is assumed."""
    found = _read_mrz(sides[::-1], texts)
    if found:
        return catalog.by_mrz(found["document_code"]), "mrz", found
    by_words = catalog.by_wording(texts)
    if by_words:
        return by_words, "wording", None
    return "national_id", "default", None


def _mrz_fields(found: dict) -> dict:
    out = {k: found.get(k) for k in ("document_number", "surname", "given_names", "date_of_birth", "expiry_date",
                                     "sex", "nationality", "issuing_country", "personal_number")}
    out["full_name"] = " ".join(x for x in (found["given_names"], found["surname"]) if x)
    out["source"] = "mrz"
    # A Malaysian passport carries the holder's NRIC in the personal-number field.
    if found.get("issuing_country") == "MYS" and found.get("personal_number"):
        nric = mykad.parse_number(found["personal_number"])
        if nric:
            out["id_number"] = nric["document_number"]
    return out


_NRIC_ANY = re.compile(r"(?<!\d)\d{6}-?\d{2}-?\d{4}(?!\d)")


def _recognisable(texts: list[str]) -> bool:
    """Something the readers can work with: known wording, an IC number, a
    MyKad or passport heading, or a machine-readable zone."""
    flat = " ".join(texts).upper()
    return bool(catalog.by_wording(texts) or _NRIC_ANY.search(flat) or "KAD PENGENALAN" in flat
                or "PASSPORT" in flat or "PASPORT" in flat or "<<" in flat)


def _turn_upright(img: np.ndarray) -> np.ndarray | None:
    """The image turned a quarter (either way) when that makes it recognisable."""
    import cv2
    for k in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        turned = cv2.rotate(img, k)
        t = ocr.read_rapid(turned)
        if t and _recognisable([t]):
            return turned
    return None


def analyze(front: np.ndarray, back: np.ndarray | None, document_type: str, country: str | None,
            claimed: dict | None, today: date | None = None) -> DocumentResult:
    s = get_settings()
    today = today or date.today()
    claimed = claimed or {}
    res = DocumentResult()
    country = (country or "").upper() or None
    requested_country, requested_type = country, document_type

    # 1. capture quality -----------------------------------------------------
    q = images.quality(front)
    if min(q["width"], q["height"]) < MIN_SHORT_SIDE:
        res.checks.append(check("document_quality", FAIL, "document_resolution_too_low", **q))
    elif q["sharpness"] < MIN_SHARPNESS:
        res.checks.append(check("document_quality", FAIL, "document_blurry", **q))
    else:
        res.checks.append(check("document_quality", PASS, **q))

    # 2. find the portrait (also turns the page upright) ----------------------
    front, doc_face = _find_portrait(front)
    sides = [front] + ([back] if back is not None else [])

    if not ocr.available():
        res.checks.append(check("document_data", REVIEW, "ocr_unavailable"))
        _portrait_check(res, front, doc_face, required=True)
        res.extracted = {}
        res.document_number = claimed.get("document_number")
        _common_checks(res, {}, claimed, "", False, today, s)
        return res

    side_texts = [ocr.read_texts(img) for img in sides]
    # A card photographed sideways: no face found to turn it upright and nothing
    # recognisable read. Try it turned a quarter each way.
    if document_type == "auto" and doc_face is None and not _recognisable(side_texts[0]):
        turned = _turn_upright(front)
        if turned is not None:
            front, doc_face = _find_portrait(turned)
            sides[0] = front
            side_texts[0] = ocr.read_texts(front)
    all_texts = [t for texts in side_texts for t in texts]
    text = "\n".join(all_texts)

    # 3. what is it -------------------------------------------------------------
    found_mrz = None
    if document_type == "auto":
        document_type, type_source, found_mrz = _detect_type(sides, all_texts)
    else:
        type_source = "requested"
        detected = catalog.by_wording(all_texts)
        if detected and catalog.TYPES[detected].category != catalog.TYPES.get(document_type, catalog.TYPES["national_id"]).category:
            res.checks.append(check("document_type", REVIEW, "document_type_mismatch",
                                    requested=document_type, detected=detected))
    meta = catalog.TYPES.get(document_type, catalog.TYPES["national_id"])
    extracted: dict = {"document_type": document_type, "document_type_source": type_source,
                       "document_category": meta.category}
    definitive = False
    _portrait_check(res, front, doc_face, required=meta.has_portrait and document_type != "visa")

    # 4. read it ----------------------------------------------------------------
    if meta.category in ("driver", "vehicle"):
        read = catalog.read(document_type, all_texts, today)
        extracted.update(read)
        missing = [f for f in catalog.REQUIRED.get(document_type, ()) if f not in read]
        detail = {"read": sorted(read)}
        if missing:
            res.checks.append(check("document_data", REVIEW, "document_fields_missing", missing=missing, **detail))
        else:
            res.checks.append(check("document_data", PASS, **detail))
        _vehicle_rules(res, document_type, read)
        if read.get("id_number"):
            nric = mykad.parse_number(read["id_number"])
            if nric:
                extracted["id_number"] = nric["document_number"]
                extracted.setdefault("date_of_birth", nric["date_of_birth"])
                if nric.get("sex"):
                    extracted.setdefault("sex", nric["sex"])
                definitive = True
    elif document_type in ("refugee_card", "foreign_worker_card"):
        pattern = r"(?<!\d)\d{3}-\d{2}[A-Z]?-?\d{5,7}(?!\d)" if document_type == "refugee_card" else r"\b[A-Z]{1,2}\d{6,9}\b"
        m = re.search(pattern, text.upper())
        if m:
            extracted["document_number"] = m.group()
            res.checks.append(check("document_data", PASS, id_format=meta.name))
        else:
            res.checks.append(check("document_data", REVIEW, "id_number_not_found"))
        extracted.update(_dates_on_card(text, today))
    else:
        definitive = _read_identity(res, extracted, sides, side_texts, all_texts, text, document_type, country,
                                    found_mrz, front)
    # The holder's name and details from labelled fields, where the document's
    # own reader (MyKad layout, MRZ) left them empty.
    if meta.category in ("identity", "driver") and document_type not in ("visa", "medical_report"):
        for k, v in fields.holder(all_texts, today).items():
            extracted.setdefault(k, v)
    country = extracted.get("country") or country

    # 5. which country -----------------------------------------------------------
    found = countrylib.detect(all_texts, mrz_country=extracted.get("issuing_country") if extracted.get("source") == "mrz" else None,
                              mykad_confident=extracted.get("source") == "mykad" and definitive)
    if not found["country"] and extracted.get("country_source") == "id_number":
        found = {"country": extracted["country"], "source": "id_number"}
    if found["country"]:
        extracted["country"] = found["country"]
        extracted["country_source"] = found["source"]
        if requested_country and requested_country != found["country"]:
            res.checks.append(check("document_country", REVIEW, "issuing_country_mismatch",
                                    requested=requested_country, detected=found["country"], source=found["source"]))
        else:
            res.checks.append(check("document_country", PASS, detected=found["country"], source=found["source"]))
    else:
        res.checks.append(check("document_country", SKIPPED, "country_not_identified", requested=requested_country))

    res.extracted = extracted
    # the number duplicates are keyed on: the person's ID number first
    res.document_number = extracted.get("id_number") or extracted.get("document_number") or claimed.get("document_number")
    _common_checks(res, extracted, claimed, text, definitive, today, s)
    return res


def _portrait_check(res: DocumentResult, front: np.ndarray, doc_face, required: bool) -> None:  # noqa: ANN001
    if doc_face is not None:
        res.face_embedding = facelib.embed(front, doc_face)
        res.portrait_px = int(min(doc_face.box[2], doc_face.box[3]))
        res.checks.append(check("document_portrait", PASS, face_width_px=doc_face.box[2],
                                detector_score=round(doc_face.score, 3)))
    elif required:
        res.checks.append(check("document_portrait", FAIL, "document_face_not_found"))


def _card_serial(texts: list[str]) -> str | None:
    """MyKad chip serial ("SN: D2009A76CE"): hexadecimal, tiny print. Fix the usual
    OCR swaps for letters that cannot be in it, vote across the readings, and
    give it only when at least two readings agree."""
    from collections import Counter
    votes: Counter = Counter()
    for t in texts:
        for m in re.finditer(r"\bS\s?N\s*[:.;]?\s*([A-Z0-9]{8,12})\b", t.upper()):
            cand = m.group(1).translate(str.maketrans("OQILZSG", "0011256"))
            if re.fullmatch(r"[0-9A-F]{8,12}", cand):
                votes[cand] += 1
    if not votes:
        return None
    best, n = votes.most_common(1)[0]
    return best if n >= 2 else None


def _issue_date(text: str, extracted: dict) -> str | None:
    """Date of issue on a passport data page: the latest printed date that is
    after the date of birth and not after today (the MRZ itself has none)."""
    dob = extracted.get("date_of_birth")
    exp = extracted.get("expiry_date")
    ds = [d for d in fields.dates(text) if d <= date.today() and (not dob or d.isoformat() > dob)
          and (not exp or d.isoformat() != exp)]
    return max(ds).isoformat() if ds else None


def _dates_on_card(text: str, today: date) -> dict:
    return fields.validity(text, today)


def _vehicle_rules(res: DocumentResult, document_type: str, read: dict) -> None:
    """Rules from APAD / PUSPAKOM practice that hold for every service."""
    if document_type == "vehicle_inspection" and read.get("result") == "fail":
        res.checks.append(check("inspection_result", FAIL, "inspection_failed"))
    elif document_type == "vehicle_inspection" and read.get("result") == "pass":
        res.checks.append(check("inspection_result", PASS))
    if document_type == "vehicle_registration" and read.get("vehicle_age_years") is not None:
        age = read["vehicle_age_years"]
        res.checks.append(check("vehicle_age", REVIEW if age > 15 else PASS, "vehicle_over_15_years",
                                age_years=age, limit=15))


def _read_identity(res: DocumentResult, extracted: dict, sides, side_texts, all_texts, text, document_type,  # noqa: ANN001, PLR0913
                   country, found_mrz, front) -> bool:
    """Passports, visas, residence permits and national identity cards. Returns
    whether the data is backed by check digits or a validated number."""
    definitive = False
    if country is None and document_type != "passport" and not found_mrz:
        country = countrylib.from_wording(all_texts)[0]
        if country is None:
            probe = mykad.read_card(side_texts[0], side_texts[1] if len(side_texts) > 1 else None)
            if probe and probe["confident"]:
                country = "MYS"
    is_mykad = country == "MYS" and document_type == "national_id"
    if found_mrz is None and not is_mykad and document_type in ("passport", "national_id", "visa", "residence_permit"):
        found_mrz = _read_mrz(sides[::-1], None if document_type == "passport" else all_texts)
    card = (mykad.read_card(side_texts[0], side_texts[1] if len(side_texts) > 1 else None)
            if country == "MYS" and document_type == "national_id" else None)
    foreign = (idnumbers.read(all_texts, country) if document_type == "national_id" and not card and not found_mrz
               and country != "MYS" else None)

    if card:
        extracted.update({k: card[k] for k in ("full_name", "date_of_birth", "sex", "place_of_birth", "citizenship",
                                               "address", "postcode", "city", "state") if card.get(k)})
        extracted["id_number"] = card["document_number"]
        # The serial is tiny print: only read it from a sharp, large enough photo of the back.
        back_ok = len(sides) > 1 and min(sides[1].shape[:2]) >= 700 and images.quality(sides[1])["sharpness"] >= 100
        serial = _card_serial(all_texts + ocr.read_fine_print(sides[1])) if back_ok else None
        if serial:
            extracted["document_number"] = serial  # the card's own serial, printed on the back
        family = mykad.family_by_wording(all_texts) or mykad.family_by_colour(front) or "MyKad"
        extracted["document_subtype"] = family
        extracted["holder"] = mykad.FAMILY[family]["holder"]
        extracted["source"] = "mykad"
        detail = {"id_format": family, "votes": card["votes"]}
        definitive = card["confident"]
        if card["front_number"] and card["back_number"] and card["front_number"] != card["back_number"]:
            res.checks.append(check("document_data", REVIEW, "id_number_front_back_mismatch", **detail))
        elif not card["confident"]:
            res.checks.append(check("document_data", REVIEW, "id_number_uncertain", **detail))
        elif card["printed_sex"] and card["printed_sex"] != card["sex"]:
            res.checks.append(check("document_data", REVIEW, "gender_mismatch", printed=card["printed_sex"],
                                    from_number=card["sex"], **detail))
        else:
            res.checks.append(check("document_data", PASS, **detail))
        if family == "MyKAS":
            extracted.update(_dates_on_card(text, date.today()))
        consistent = mykad.postcode_matches_state(card.get("postcode"), card.get("state"))
        if consistent is False:
            res.checks.append(check("address_consistency", REVIEW, "postcode_state_mismatch",
                                    postcode=card["postcode"], state=card["state"]))
        elif consistent:
            res.checks.append(check("address_consistency", PASS))
        res.candidates = card["candidates"]
        res.name_readings = card["name_readings"]
    elif is_mykad:
        res.checks.append(check("document_data", FAIL, "id_number_not_found"))
    elif found_mrz:
        extracted.update(_mrz_fields(found_mrz))
        issued = _issue_date(text, extracted)
        if issued:
            extracted["start_date"] = issued
        detail = {"mrz_format": found_mrz["format"], "document_code": found_mrz["document_code"],
                  "check_digits": found_mrz["checks"]}
        if not found_mrz["valid"]:
            res.checks.append(check("document_data", REVIEW, "mrz_checksum_failed", **detail))
        elif document_type == "passport" and not found_mrz["document_code"].startswith("P"):
            res.checks.append(check("document_data", REVIEW, "document_type_mismatch", **detail))
        else:
            definitive = True
            res.checks.append(check("document_data", PASS, **detail))
    elif foreign:
        extracted.update({k: v for k, v in foreign.items()
                          if k in ("date_of_birth", "sex", "holder", "birth_year")})
        extracted["id_number"] = foreign["number"]
        extracted["document_subtype"] = foreign["id_format"]
        extracted["source"] = "id_number"
        if not country:
            extracted["country"], extracted["country_source"] = foreign["country"], "id_number"
        detail = {"id_format": foreign["id_format"], "checksum": foreign["checksum"],
                  "valid_checksum": foreign["valid_checksum"], "votes": foreign["votes"]}
        res.candidates = foreign["candidates"]
        if foreign["checksum"] == "hard" and not foreign["valid_checksum"]:
            res.checks.append(check("document_data", REVIEW, "id_number_checksum_failed", **detail))
        elif foreign["checksum"] == "none" and not foreign["confident"]:
            res.checks.append(check("document_data", REVIEW, "id_number_uncertain", **detail))
        else:
            definitive = foreign["valid_checksum"] or foreign["confident"]
            res.checks.append(check("document_data", PASS, **detail))
    elif document_type == "passport":
        res.checks.append(check("document_data", FAIL, "mrz_not_found"))
    else:
        res.checks.append(check("document_data", REVIEW, "no_machine_readable_data"))
    return definitive


def _common_checks(res: DocumentResult, extracted: dict, claimed: dict, text: str, definitive: bool,
                   today: date, s) -> None:  # noqa: ANN001
    # expiry
    exp = extracted.get("expiry_date")
    if exp:
        expired = date.fromisoformat(exp) < today
        res.checks.append(check("document_expiry", FAIL if expired else PASS, "document_expired", expiry_date=exp))
    elif extracted.get("source") == "mykad" and extracted.get("document_subtype") != "MyKAS":
        res.checks.append(check("document_expiry", SKIPPED, "no_expiry_on_document"))
    else:
        res.checks.append(check("document_expiry", SKIPPED, "expiry_not_readable"))
    # age (people only)
    if extracted.get("document_category", "identity") != "vehicle":
        dob = extracted.get("date_of_birth")
        if dob and s.min_age > 0:
            age = _age(date.fromisoformat(dob), today)
            res.checks.append(check("age", PASS if age >= s.min_age else FAIL, "underage", minimum=s.min_age))
        else:
            res.checks.append(check("age", SKIPPED, "date_of_birth_not_readable"))
    # what the user typed
    if claimed and text:
        res.checks.append(_match_claimed(claimed, extracted, text, definitive, s.name_match_threshold,
                                         res.candidates, res.name_readings))


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
        # The user may type either the person's ID number or the document's own number.
        read_numbers = {_norm_id(extracted.get(k) or "") for k in ("id_number", "document_number", "owner_id_number")} - {""}
        ex = next(iter(read_numbers), "")
        ok = number in read_numbers if read_numbers else number in _norm_id(flat)
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

    typed_plate = re.sub(r"\s", "", (claimed.get("plate") or "").upper())
    if typed_plate:
        read_plate = extracted.get("plate")
        ok = read_plate == typed_plate if read_plate else typed_plate in re.sub(r"\s", "", flat)
        compared["plate_match"] = ok
        if not ok:
            flag(REVIEW, "plate_mismatch")

    dob = claimed.get("date_of_birth")
    if dob and extracted.get("date_of_birth"):
        ok = dob == extracted["date_of_birth"]
        compared["date_of_birth_match"] = ok
        if not ok:
            flag(FAIL if definitive else REVIEW, "date_of_birth_mismatch")

    return check("claimed_data", worst, code, **compared)
