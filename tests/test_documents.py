"""Auto-detection and reading of driver, vehicle and foreign identity documents
(made-up documents, typical wording)."""
from datetime import date

import pytest

from app.pipeline import catalog, document, fields, idnumbers, images, mykad
from tests import synth

TODAY = date(2026, 11, 23)


@pytest.fixture(autouse=True)
def _needs_ocr(need_models):
    pass


def _doc(lines, face=False, requested="auto", claimed=None):
    img = images.decode(synth.card_image(synth.face_bgr() if face else None, lines))
    return document.analyze(img, None, requested, None, claimed or {}, today=TODAY)


def _check(res, name):
    return next((c for c in res.checks if c["name"] == name), None)


def test_vehicle_ownership_certificate():
    res = _doc(["SIJIL PEMILIKAN KENDERAAN", "No. Pendaftaran: WXY 1234", "No. Casis: PM2M600GHKA012345",
                "No. Enjin: 3SZB123456", "Tahun Dibuat: 2015", "Keupayaan Enjin: 1495 cc"])
    ex = res.extracted
    assert ex["document_type"] == "vehicle_registration" and ex["document_category"] == "vehicle"
    assert ex["plate"] == "WXY1234" and ex["chassis_number"] == "PM2M600GHKA012345"
    assert ex["year_made"] == 2015 and ex["vehicle_age_years"] == 11 and ex["engine_cc"] == 1495
    assert _check(res, "document_portrait") is None  # no portrait expected on a VOC
    assert _check(res, "vehicle_age")["status"] == "pass"


def test_old_vehicle_goes_to_review():
    res = _doc(["VEHICLE OWNERSHIP CERTIFICATE", "No. Pendaftaran: BKA 88", "Tahun Dibuat: 2008"])
    assert _check(res, "vehicle_age")["code"] == "vehicle_over_15_years"


def test_puspakom_fail_rejects():
    res = _doc(["PUSPAKOM", "LAPORAN PEMERIKSAAN KENDERAAN E-HAILING", "No. Pendaftaran: VAB 2233",
                "Tarikh: 20/11/2026", "Keputusan: GAGAL"])
    assert res.extracted["document_type"] == "vehicle_inspection" and res.extracted["result"] == "fail"
    assert _check(res, "inspection_result")["status"] == "fail"


def test_road_tax_and_insurance():
    tax = _doc(["LESEN KENDERAAN MOTOR", "No. Pendaftaran: JQT 5566", "Tempoh Sah: 01/12/2026 - 30/11/2027", "AH"])
    assert tax.extracted["document_type"] == "road_tax" and tax.extracted["expiry_date"] == "2027-11-30"
    assert tax.extracted["usage_code"] == "AH" and _check(tax, "document_expiry")["status"] == "pass"
    ins = _doc(["CERTIFICATE OF INSURANCE", "ETIQA GENERAL", "Policy No: VPC123456789",
                "Vehicle No: WA 1234 B", "Period: 01-01-2026 to 31-12-2026", "E-HAILING EXTENSION"])
    ex = ins.extracted
    assert ex["document_type"] == "vehicle_insurance" and ex["insurer"] == "ETIQA" and ex["ehailing_cover"]
    assert ex["plate"] == "WA1234B" and ex["expiry_date"] == "2026-12-31"


def test_expired_insurance_rejects():
    ins = _doc(["COVER NOTE", "No. Pendaftaran: PKB 77", "Tempoh: 01/01/2025 - 31/12/2025"])
    assert _check(ins, "document_expiry")["status"] == "fail"


def test_driving_licence_reads_classes_validity_and_psv():
    res = _doc(["LESEN MEMANDU MALAYSIA", "900514-10-5231", "KELAS: D DA B2", "TEMPOH: 15/03/2024 - 14/03/2029",
                "PSV E-HAILING"], face=True)
    ex = res.extracted
    assert ex["document_type"] == "driving_licence" and ex["licence_classes"] == ["D", "DA", "B2"]
    assert ex["expiry_date"] == "2029-03-14" and ex["psv"] and ex["ehailing"]
    assert ex["id_number"] == "900514-10-5231" and ex["date_of_birth"] == "1990-05-14"
    assert _check(res, "document_portrait")["status"] == "pass"


def test_plate_typed_by_the_user_is_checked():
    ok = _doc(["SIJIL PEMILIKAN KENDERAAN", "No. Pendaftaran: WXY 1234"], claimed={"plate": "wxy1234"})
    assert _check(ok, "claimed_data")["status"] == "pass"
    bad = _doc(["SIJIL PEMILIKAN KENDERAAN", "No. Pendaftaran: WXY 1234"], claimed={"plate": "WXY 1243"})
    assert _check(bad, "claimed_data")["code"] == "plate_mismatch"


def test_requested_type_disagreeing_with_the_document():
    res = _doc(["SIJIL PEMILIKAN KENDERAAN", "No. Pendaftaran: WXY 1234"], requested="national_id")
    assert _check(res, "document_type")["code"] == "document_type_mismatch"


def test_singapore_nric_found_and_country_named_by_its_check_letter():
    res = _doc(["REPUBLIC OF SINGAPORE", "IDENTITY CARD NO. S1234567D", "TAN AH KOW"], face=True)
    ex = res.extracted
    assert ex["country"] == "SGP" and ex["id_number"] == "S1234567D"
    assert _check(res, "document_data")["status"] == "pass"


# --- pure rules ---------------------------------------------------------------------

def test_detection_order_vehicle_before_identity():
    # a VOC carries the owner's IC number, a licence too: neither is an identity card
    assert catalog.by_wording(["SIJIL PEMILIKAN KENDERAAN\nNo. KP 900514-10-5231"]) == "vehicle_registration"
    assert catalog.by_wording(["LESEN MEMANDU\n900514-10-5231"]) == "driving_licence"
    assert catalog.by_wording(["KAD PENGENALAN\nMALAYSIA"]) is None
    assert catalog.by_mrz("P<") == "passport" and catalog.by_mrz("V<") == "visa" and catalog.by_mrz("IR") == "residence_permit"


def test_dates_and_plates():
    assert [d.isoformat() for d in fields.dates("Tempoh 01 MAC 2026 hingga 28-02-2027")] == ["2026-03-01", "2027-02-28"]
    assert fields.plate("No. Pendaftaran : SAB 1234 A") == "SAB1234A"
    assert fields.plates("QAA 123, HWA 45, LIMO 12 B, ZB 123") == ["QAA123", "HWA45", "LIMO12B", "ZB123"]
    assert fields.plates("IO 1234") == []  # I and O are never used


@pytest.mark.parametrize("country,number,valid", [
    ("SGP", "S1234567D", True), ("SGP", "S1234567A", False),
    ("HKG", "A123456(3)", True), ("HKG", "A123456(4)", False),
    ("TWN", "A123456789", True), ("TWN", "A123456788", False),
    ("CHN", "11010519491231002X", True), ("CHN", "110105194912310021", False),
    ("IND", "234123412346", True), ("IND", "234123412345", False),
])
def test_checksums(country, number, valid):
    info = idnumbers.BY_COUNTRY[country].read(number)
    assert info and info["valid_checksum"] is valid


def test_encoded_details():
    nik = idnumbers.BY_COUNTRY["IDN"].read("3171074512900001")  # day 45 = 5th, female
    assert nik["date_of_birth"] == "1990-12-05" and nik["sex"] == "F"
    cnic = idnumbers.BY_COUNTRY["PAK"].read("35202-1234567-3")
    assert cnic and cnic["sex"] == "M"
    vn = idnumbers.BY_COUNTRY["VNM"].read("001301012345")  # Ha Noi, 2000s female, 2001
    assert vn["sex"] == "F" and vn["birth_year"] == 2001
    assert idnumbers.read(["random 123456789012 text"], None) is None  # 12 digits alone name no country


def test_mykad_family_and_codes():
    assert mykad.family_by_wording(["KAD PENGENALAN PEMASTAUTIN TETAP"]) == "MyPR"
    assert mykad.family_by_wording(["MyKAS"]) == "MyKAS"
    assert mykad.parse_number("900514-69-5231") is None  # 69 is not allocated
    assert mykad.parse_number("900514-74-5231")["place_of_birth"] == "China"


def test_start_and_expiry_dates_and_both_numbers():
    ins = _doc(["CERTIFICATE OF INSURANCE", "Policy No: VPC123456789", "Vehicle No: WA 1234 B",
                "Period: 01/01/2026 - 31/12/2026"])
    assert ins.extracted["start_date"] == "2026-01-01" and ins.extracted["expiry_date"] == "2026-12-31"
    assert ins.extracted["document_number"] == "VPC123456789"
    lic = _doc(["LESEN MEMANDU MALAYSIA", "900514-10-5231", "NO. LESEN: 12345678", "TEMPOH: 15/03/2024 - 14/03/2029"],
               face=True, claimed={"document_number": "12345678"})
    assert lic.extracted["id_number"] == "900514-10-5231" and lic.extracted["document_number"] == "12345678"
    assert lic.extracted["start_date"] == "2024-03-15"
    # the user may type either number
    assert _check(lic, "claimed_data")["status"] == "pass"
    lic2 = _doc(["LESEN MEMANDU MALAYSIA", "900514-10-5231", "NO. LESEN: 12345678", "TEMPOH: 15/03/2024 - 14/03/2029"],
                face=True, claimed={"document_number": "900514105231"})
    assert _check(lic2, "claimed_data")["status"] == "pass"


def test_mykad_card_serial_is_the_document_number():
    front = images.decode(synth.mykad_front("900514-10-5232", ["ANNA BINTI ERIK"],
                                            ["12 JALAN MAWAR 3", "43000 KAJANG", "SELANGOR"], "PEREMPUAN", synth.face_bgr()))
    back = images.decode(synth.card_image(None, ["900514-10-5232-06-01", "SN: D2009A76CE"]))
    res = document.analyze(front, back, "auto", None, {}, today=TODAY)
    assert res.extracted["id_number"] == "900514-10-5232" and res.extracted["document_number"] == "D2009A76CE"
    assert res.extracted["document_subtype"] == "MyKad" and res.extracted["holder"] == "citizen"
