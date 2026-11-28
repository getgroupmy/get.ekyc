from datetime import date

import pytest

from app.pipeline import mrz, mykad
from app.pipeline.checks import overall, check, FAIL, PASS, REVIEW
from tests.synth import td3_lines


def test_check_digit_icao_examples():
    assert mrz.check_digit("L898902C3") == "6"
    assert mrz.check_digit("740812") == "2"
    assert mrz.check_digit("ZE184226B<<<<<") == "1"


def test_td3_specimen():
    text = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204159ZE184226B<<<<<10"
    r = mrz.find_mrz(text)
    assert r["valid"] and r["format"] == "TD3"
    assert (r["surname"], r["given_names"]) == ("ERIKSSON", "ANNA MARIA")
    assert r["document_number"] == "L898902C3"
    assert r["date_of_birth"] == "1974-08-12" and r["expiry_date"] == "2012-04-15"
    assert r["sex"] == "F" and r["nationality"] == "UTO"


def test_td3_tolerates_ocr_noise_in_numeric_fields():
    l1, l2 = td3_lines()
    noisy = l2[:13] + l2[13:19].replace("0", "O") + l2[19:]
    r = mrz.find_mrz(f"junk line\n{l1}\n{noisy}\n")
    assert r["valid"] and r["date_of_birth"] == "1974-08-12"


def test_td3_detects_tampering():
    l1, l2 = td3_lines()
    forged = l2[:13] + "900101" + l2[19:]  # change the date of birth, keep old check digit
    r = mrz.find_mrz(f"{l1}\n{forged}")
    assert not r["valid"] and not r["checks"]["date_of_birth"]


def test_td1_specimen():
    text = "I<UTOD231458907<<<<<<<<<<<<<<<\n7408122F1204159UTO<<<<<<<<<<<6\nERIKSSON<<ANNA<MARIA<<<<<<<<<<"
    r = mrz.find_mrz(text)
    assert r["valid"] and r["format"] == "TD1"
    assert r["document_number"] == "D23145890" and r["surname"] == "ERIKSSON"


def test_td2_specimen():
    text = "I<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<\nD231458907UTO7408122F1204159<<<<<<<6"
    r = mrz.find_mrz(text)
    assert r["valid"] and r["format"] == "TD2" and r["given_names"] == "ANNA MARIA"


def test_no_mrz():
    assert mrz.find_mrz("hello world\nnothing here") is None


def test_mykad():
    r = mykad.parse_number("900514-10-5231", today=date(2026, 1, 1))
    assert r == {"document_number": "900514-10-5231", "date_of_birth": "1990-05-14", "sex": "M",
                 "place_of_birth": "Selangor"}
    assert mykad.parse_number("050229-14-5232", today=date(2026, 1, 1)) is None  # 29 Feb 2005 does not exist
    assert mykad.parse_number("900514-00-5231") is None  # invalid place code
    assert mykad.parse_number("12345") is None
    assert mykad.find_in_text("No. KP 900514 10 5232 WARGANEGARA")["sex"] == "F"


def test_overall_decision():
    assert overall([check("a", PASS)], True) == ("approved", [])
    assert overall([check("a", PASS)], False) == ("review", ["manual_approval_required"])
    assert overall([check("a", PASS), check("b", REVIEW, "x")], True) == ("review", ["x"])
    assert overall([check("a", REVIEW, "x"), check("b", FAIL, "y")], True) == ("rejected", ["y"])


def _c(name, status="pass", code=None):
    return {"name": name, "status": status, "code": code, "detail": {}}


@pytest.mark.parametrize("source,kind,face,live,missing", [
    ("mykad", "national_id", "pass", "pass", []),
    ("mrz", "passport", "pass", "pass", []),
    ("mrz", "residence_permit", "pass", "pass", []),
    ("mrz", "visa", "pass", "pass", ["document_confirmed"]),
    ("id_number", "national_id", "pass", "pass", ["document_confirmed"]),  # a foreign number, format only
    (None, "driving_licence", "pass", "pass", ["document_confirmed"]),
    (None, "refugee_card", "pass", "pass", ["document_confirmed"]),
    ("mykad", "national_id", "skipped", "pass", ["face_match"]),
    ("mykad", "national_id", "pass", None, ["liveness"]),
    ("mrz", "passport", "review", "fail", ["face_match", "liveness"]),
])
def test_auto_approval_needs_confirmed_document_face_and_liveness(source, kind, face, live, missing):
    from app.pipeline.checks import auto_approval
    checks = [_c("document_data"), _c("face_match", face)] + ([_c("liveness", live)] if live else [])
    gate = auto_approval(checks, {"source": source, "document_type": kind})
    assert gate["detail"].get("missing", []) == missing
    assert (gate["status"] == "pass") == (not missing)
    # the document's own data check must have passed too
    checks[0] = _c("document_data", "review", "id_number_uncertain")
    assert "document_confirmed" in auto_approval(checks, {"source": source, "document_type": kind})["detail"]["missing"]
