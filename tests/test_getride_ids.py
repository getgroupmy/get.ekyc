"""MyKad and passport the way GET-Ride sends them: document_type "auto", no
claimed details, a selfie, and the photos fetched back by the client (as
ekyc-webhook does). What comes out must be enough for GET-Ride's checks."""
import json

import pytest

from tests import synth
from tests.conftest import ADMIN

BASE = "/v1/verifications"
NAME = ["ANNA BINTI ERIK"]
ADDRESS = ["12 JALAN MAWAR 3", "TAMAN MAWAR", "43000 KAJANG", "SELANGOR"]


@pytest.fixture(autouse=True)
def _needs(need_models):
    pass


def _run(client, key, files, ref):
    v = client.post(BASE, json={"document_type": "auto", "require_liveness": False, "test": True,
                                "external_ref": ref}, headers=key).json()
    for kind, data in files.items():
        r = client.put(f"{BASE}/{v['id']}/media/{kind}", files={"file": ("x.jpg", data, "image/jpeg")}, headers=key)
        assert r.status_code == 200, r.text
    assert client.post(f"{BASE}/{v['id']}/submit", headers=key).status_code == 202
    out = client.get(f"{BASE}/{v['id']}", headers=key).json()
    for kind in files:  # ekyc-webhook copies these into GET-Ride's storage
        assert client.get(f"{BASE}/{v['id']}/media/{kind}", headers=key).content == files[kind]
    return out


def _checks(v):
    return {c["name"]: c for c in v["result"]["checks"]}


def _done(client, v):
    client.delete(f"/admin/api/verifications/{v['id']}", headers=ADMIN)


def test_mykad_auto(client, api_key):
    face = synth.face_bgr()
    v = _run(client, api_key, {
        "document_front": synth.mykad_front("900514-10-5232", NAME, ADDRESS, "PEREMPUAN", face),
        "document_back": synth.mykad_back("900514-10-5232"),
        "selfie": synth.jpeg(synth.selfie()),
    }, "getride-mykad")
    try:
        ex = v["result"]["extracted"]
        assert ex["document_type"] == "national_id" and ex["country"] == "MYS", ex
        assert ex["id_number"] == "900514-10-5232" and ex["date_of_birth"] == "1990-05-14"
        assert ex["full_name"] == "ANNA BINTI ERIK" and ex["sex"] == "F" and ex["citizenship"] == "citizen"
        assert ex["postcode"] == "43000"
        checks = _checks(v)
        assert checks["document_data"]["status"] == "pass", checks["document_data"]
        assert checks["face_match"]["status"] in ("pass", "review")  # synthetic faces
        assert isinstance(v["result"]["scores"].get("face_score"), (int, float))
        print("MYKAD", json.dumps({"status": v["status"], "reasons": v["reasons"], "extracted": ex,
                                   "checks": {k: c["status"] for k, c in checks.items()},
                                   "scores": v["result"]["scores"]}))
    finally:
        _done(client, v)


def test_passport_auto(client, api_key):
    doc = synth.passport_image(synth.td3_lines(), synth.face_bgr())
    v = _run(client, api_key, {"document_front": doc, "selfie": synth.jpeg(synth.selfie())}, "getride-passport")
    try:
        ex = v["result"]["extracted"]
        assert ex["document_type"] == "passport", ex
        assert ex["document_number"] == "L898902C3"
        assert ex["date_of_birth"] == "1974-08-12" and ex["sex"] == "F"
        assert ex.get("expiry_date") and ex.get("nationality") == "UTO"
        assert "ANNA MARIA" in ex["full_name"] and "ERIKSSON" in ex["full_name"]
        checks = _checks(v)
        assert checks["document_data"]["status"] == "pass", checks["document_data"]
        print("PASSPORT", json.dumps({"status": v["status"], "reasons": v["reasons"], "extracted": ex,
                                      "checks": {k: c["status"] for k, c in checks.items()},
                                      "scores": v["result"]["scores"]}))
    finally:
        _done(client, v)


# --- the other identity documents GET-Ride riders and staff may show -----------------
from datetime import date  # noqa: E402

from app.pipeline import document, fields, images  # noqa: E402

OTHER_CARDS = {
    "MyPR": (["KAD PENGENALAN", "PEMASTAUTIN TETAP MALAYSIA", "900514-71-5233", "CHEN WEI MING", "12 JALAN MAWAR 3",
              "43000 KAJANG", "SELANGOR", "PEMASTAUTIN TETAP   LELAKI"],
             {"document_type": "national_id", "document_subtype": "MyPR", "full_name": "CHEN WEI MING",
              "id_number": "900514-71-5233", "date_of_birth": "1990-05-14", "sex": "M"}),
    "MyKAS": (["KAD PENGENALAN", "PEMASTAUTIN SEMENTARA", "900514-71-5234", "SITI AMINAH", "TARIKH LUPUT 12-05-2030",
               "PEREMPUAN"],
              {"document_subtype": "MyKAS", "full_name": "SITI AMINAH", "id_number": "900514-71-5234",
               "sex": "F", "expiry_date": "2030-05-12"}),
    "MyTentera": (["ANGKATAN TENTERA MALAYSIA", "KAD PENGENALAN TENTERA", "900514-10-5235", "T 1234567",
                   "AHMAD BIN ALI", "LELAKI"],
                  {"document_subtype": "MyTentera", "full_name": "AHMAD BIN ALI", "id_number": "900514-10-5235"}),
    "i-Kad": (["i-KAD  PEKERJA ASING", "NAMA / NAME", "RAHUL KUMAR", "NO. PASPORT / PASSPORT NO", "N1234567",
               "WARGANEGARA  INDIA", "TARIKH LUPUT 12/05/2028"],
              {"document_type": "foreign_worker_card", "full_name": "RAHUL KUMAR", "document_number": "N1234567",
               "nationality": "IND", "expiry_date": "2028-05-12"}),
    "UNHCR": (["UNHCR", "NAME  MOHAMMAD ALI", "UNHCR NO  123-21C12345", "DATE OF BIRTH 01/01/1990", "ORIGIN  MYANMAR",
               "VALID UNTIL 31/12/2027"],
              {"document_type": "refugee_card", "full_name": "MOHAMMAD ALI", "document_number": "123-21C12345",
               "date_of_birth": "1990-01-01", "nationality": "MMR", "expiry_date": "2027-12-31"}),
    "Singapore NRIC": (["REPUBLIC OF SINGAPORE", "IDENTITY CARD NO. S1234567D", "Name", "TAN AH KOW",
                        "Date of birth 12-05-1990", "Sex M"],
                       {"country": "SGP", "full_name": "TAN AH KOW", "id_number": "S1234567D",
                        "date_of_birth": "1990-05-12", "sex": "M"}),
    "Indonesia KTP": (["PROVINSI DKI JAKARTA", "NIK : 3171051205900001", "Nama : BUDI SANTOSO",
                       "Tempat/Tgl Lahir : JAKARTA, 12-05-1990", "Jenis Kelamin : LAKI-LAKI", "Kewarganegaraan : WNI"],
                      {"country": "IDN", "full_name": "BUDI SANTOSO", "id_number": "3171051205900001",
                       "date_of_birth": "1990-05-12", "sex": "M", "nationality": "IDN"}),
    "Malaysian driving licence": (["LESEN MEMANDU", "MALAYSIA", "NAMA / NAME", "AHMAD BIN ALI",
                                   "NO. PENGENALAN / IDENTITY NO", "900514-10-5231", "KELAS / CLASS  D  B2",
                                   "TEMPOH 12/05/2024 - 11/05/2029"],
                                  {"document_type": "driving_licence", "full_name": "AHMAD BIN ALI",
                                   "id_number": "900514-10-5231", "date_of_birth": "1990-05-14", "sex": "M",
                                   "expiry_date": "2029-05-11"}),
}


@pytest.mark.parametrize("card", OTHER_CARDS)
def test_other_identity_documents_auto(card):
    lines, want = OTHER_CARDS[card]
    res = document.analyze(images.decode(synth.card_image(synth.face_bgr(), lines)), None, "auto", None, {},
                           today=date(2026, 11, 26))
    got = {k: res.extracted.get(k) for k in want}
    assert got == want, (card, res.extracted)
    data = next(c for c in res.checks if c["name"] == "document_data")
    assert data["status"] == "pass", (card, data)


def test_holder_fields_from_labels():
    texts = ["NAMA / NAME\nRAHUL KUMAR\nTARIKH LAHIR / DATE OF BIRTH 02/03/1988\nJANTINA / SEX: LELAKI\n"
             "WARGANEGARA / NATIONALITY: BANGLADESH",
             "NAMA / NAME ;\nif oy\nTARIKH LAHIR 02/03/1988"]  # a noisy second reading must not win
    assert fields.holder(texts, date(2026, 11, 26)) == {"full_name": "RAHUL KUMAR", "date_of_birth": "1988-03-02",
                                                        "sex": "M", "nationality": "BGD"}
    assert fields.holder(["Nama : SITI BINTI ABU\nJenis Kelamin : PEREMPUAN"])["sex"] == "F"
    assert fields.holder(["NAME\nNO. 12345"]) == {}  # a label with no name after it


# --- wording as OCR reads it on real driver and vehicle documents (made-up details) --------
from app.pipeline import catalog  # noqa: E402


@pytest.mark.parametrize("lines,want", [
    (["365", "LESENKENDERAANMOTOR AKTA PENGANGKUTAN JALAN 1987", "Sila baca maklumat di muka sebelah sebelum",
      "melekatkan LKM ini di cermin kenderaan anda", "ABC1234 11 JAN"], "road_tax"),
    (["LESEN KENDERAAN MOTO", "maklumat di muka sebelah sebelum", "an LKM ini di cermin kenderaan anda 2023"], "road_tax"),
    (["LESEN VOKASIONAL MALAYSIA", "MALAYSIAN VOCATIONAL LICENCE", "Name / Name", "ALI BIN ABU",
      "No.KP./ I.D.:900514105231", "Tempoh / Valid Until", "28/06/2023-26/07/2024", "PSV D:TEKSI/KERETA SEWA/E-HAILING"],
     "driving_licence"),
    (["MOTOR POLICY SCHEDULE/JADUAL POLISI MOTOR", "Policy No./No. Polisi : 235-070-22-000000",
      "The insured must hold a valid driving licence"], "vehicle_insurance"),
    (["SOME INSURANCE BHD", "THE SCHEDULE", "Class of Policy PRIVATE CAR", "Type of Cover COMPREHENSIVE"], "vehicle_insurance"),
    (["Zurich General Takaful Malaysia Berhad", "CONFIRMATION OF PURCHASE OF TAKAFUL", "Vehicle Registration No. ABC123"],
     "vehicle_insurance"),
    (["Notifications >", "Help & Support >", "Logout >"], None),  # an app screenshot is not a document
])
def test_real_world_wording(lines, want):
    assert catalog.by_wording(["\n".join(lines)]) == want


def test_vocational_licence_reads_as_such():
    text = ["LESEN VOKASIONAL MALAYSIA\nMALAYSIAN VOCATIONAL LICENCE\nName / Name\nALI BIN ABU\n"
            "No.KP./ I.D.:900514105231\nTempoh / Valid Until\n28/06/2023-26/07/2024\n"
            "Jenis / Type : Kelas / Class:\nPSV D:TEKSI/TEKSI MEWAH/\nKERETA SEWA/E-HAILING\nGDL D:BTM <= 3500 KG"]
    out = catalog.read("driving_licence", text, date(2024, 1, 1))
    assert out["document_subtype"] == "Lesen Vokasional (PSV/GDL)"
    assert out["psv"] and out["gdl"] and out["ehailing"] and out["vocational"]
    assert out["expiry_date"] == "2024-07-26" and out["id_number"] == "900514105231"


# --- the Malaysian licence and road tax as printed (made-up details) -------------------------
def test_licence_name_classes_and_address_under_the_heading():
    text = ["0105011\nJenis / Type : Kelas / Class:\nLESEN MEMANDU MALAYSIA\nDRIVING LICENCE\nALI BIN ABU\n"
            "Warganegara / Nationality No. Pengenalan / Identity No.\nMALAYSIA 900514105231\nKelas / Class\nB2 D\n"
            "Tempoh / Validity\n12/05/2022-03/05/2027\nAlamat / Address\n12 JALAN MAWAR 3\n43000 KAJANG\nSELANGOR"]
    out = catalog.read("driving_licence", text, date(2026, 1, 1))
    assert out["full_name"] == "ALI BIN ABU"
    assert out["licence_classes"] == ["B2", "D"]
    assert out["expiry_date"] == "2027-05-03" and out["id_number"] == "900514105231"
    assert out["address"].startswith("12 JALAN MAWAR 3") and "43000 KAJANG" in out["address"]


def test_road_tax_dates_and_holder_layout():
    text = "2024\nABC1234 25 JUL 132128747\n0110021 Vqxyngr 21072023 26072023 25072024\n" \
           "VEL02010 008664 XXXXX 1332SP AB RM70.00\nSEMENANJUNG PERSENDIRIAN"
    assert catalog.by_wording([text]) == "road_tax"  # no title: recognised by its layout
    out = catalog.read("road_tax", [text], date(2024, 1, 1))
    assert out["start_date"] == "2023-07-26" and out["expiry_date"] == "2024-07-25"
    stuck = "LESEN KENDERAAN MOTO\n0110011 ZZYqwbK 22112022 2611202225052023\nSEMENANJUNG PERSENDIRIAN"
    assert catalog.read("road_tax", [stuck], date(2023, 1, 1))["expiry_date"] == "2023-05-25"


def test_a_sideways_licence_is_turned_upright():
    import cv2
    img = images.decode(synth.card_image(synth.face_bgr(), [
        "LESEN VOKASIONAL MALAYSIA", "MALAYSIAN VOCATIONAL LICENCE", "Name / Name", "ALI BIN ABU",
        "No.KP./ I.D.:900514105231", "Tempoh / Valid Until", "28/06/2023-26/07/2027"]))
    res = document.analyze(cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE), None, "auto", None, {}, today=date(2026, 1, 1))
    assert res.extracted["document_type"] == "driving_licence", res.extracted
    assert res.extracted.get("document_subtype") == "Lesen Vokasional (PSV/GDL)"


def test_malaysian_ic_is_not_taken_for_a_foreign_number():
    """A licence or vocational card's IC number must not become an Indian Aadhaar
    or Japanese My Number just because it passes their check digit."""
    from app.pipeline import idnumbers, mykad
    seen = {}
    for spec in idnumbers.SPECS:
        if spec.country not in ("IND", "JPN"):
            continue
        for dob in range(900101, 900131):
            for tail in range(1000, 1400):
                n = f"{dob}10{tail}"
                if mykad.parse_number(n) and idnumbers._hits(spec, [n]) and idnumbers._hits(spec, [n])[0]["valid_checksum"]:
                    seen[spec.country] = n
                    break
            if spec.country in seen:
                break
    assert seen, "expected a collision to test with"
    for n in seen.values():
        assert idnumbers.read([f"No. Pengenalan / Identity No.\nMALAYSIA {n}"], None) is None
        assert idnumbers.read([n], "IND" if n == seen.get("IND") else "JPN") is not None  # with the country, still read


def test_short_keywords_are_not_found_across_words():
    assert catalog.by_wording(["KAD PENGENALAN\nPEMASTAUTIN TETAP MALAYSIA\nKADIR BIN ALI"]) is None
    assert catalog.by_wording(["SELANGOR LKS PERMIT"]) is None


@pytest.mark.parametrize("readings,start,expiry", [
    # the good reading first, then a garbled one that adds digit runs
    (["2023\nABC4043\n0114011 PjwVIKe 10112022 1011202209112023\nVEL05003 000000 998SP AB RM20.00",
      "0114011 10112022 1011202209112023 77777777 31129999"], "2022-11-10", "2023-11-09"),
    (["2024\nTB84 25 JUL 132128747\n0110021 Vqxyngr 21072023 26072023 25072024"], "2023-07-26", "2024-07-25"),
    # only the issue date as digits: the big "04 MEI" and the issue year
    (["SEMENANJUNG VEL02502 5008611 206082 nzIjwDE 05052023 atkan LKM ini di cermin kenderaan anda\n"
      "PERSENDIRIAN 1498 SP AB RM90.00 04 MEI 202"], "2023-05-05", "2024-05-04"),
    # garbled digits; the big "25 DIS" with the year above it
    (["2023\nVCN1405 25 DIS\n0114011 UAYm1 20122052"], None, "2023-12-25"),
])
def test_road_tax_expiry_from_real_readings(readings, start, expiry):
    s, e = fields.road_tax_dates(readings)
    assert (s.isoformat() if s else None, e.isoformat() if e else None) == (start, expiry)
