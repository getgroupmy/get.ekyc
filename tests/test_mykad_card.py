"""MyKad reading on cards with a security-pattern background (made-up people)."""
from datetime import date

import pytest

from app.pipeline import document, images, mykad, ocr
from tests import synth

NAME = ["ANNA BINTI ERIK"]
ADDRESS = ["12 JALAN MAWAR 3", "TAMAN MAWAR", "43000 KAJANG", "SELANGOR"]


def _read(number="900514-10-5232", sex="PEREMPUAN", back_number=None, claimed=None):
    front = images.decode(synth.mykad_front(number, NAME, ADDRESS, sex, synth.face_bgr()))
    back = images.decode(synth.mykad_back(back_number or number))
    return document.analyze(front, back, "national_id", "MYS", claimed or {}, today=date(2026, 11, 23))


def _check(res, name):
    return next(c for c in res.checks if c["name"] == name)


@pytest.fixture(autouse=True)
def _needs_ocr(need_models):
    pass


def test_reads_number_name_address_and_cross_checks():
    res = _read(claimed={"full_name": "Anna binti Erik", "document_number": "900514105232",
                         "address": "12 Jalan Mawar 3, Taman Mawar, 43000 Kajang, Selangor"})
    ex = res.extracted
    assert ex["document_number"] == "900514-10-5232" and ex["date_of_birth"] == "1990-05-14"
    assert ex["sex"] == "F" and ex["place_of_birth"] == "Selangor" and ex["citizenship"] == "citizen"
    assert ex["full_name"] == "ANNA BINTI ERIK"
    assert ex["postcode"] == "43000" and ex["state"] == "SELANGOR" and ex["city"] == "KAJANG"
    assert _check(res, "document_data")["status"] == "pass"
    claimed = _check(res, "claimed_data")
    assert claimed["status"] == "pass", claimed
    assert claimed["detail"]["document_number_match"] and claimed["detail"]["postcode_match"]


def test_typed_number_with_or_without_dashes():
    for typed in ("900514-10-5232", "900514105232", "900514 10 5232"):
        assert _check(_read(claimed={"document_number": typed}), "claimed_data")["status"] == "pass"


def test_wrong_typed_number_rejects():
    c = _check(_read(claimed={"document_number": "900514-10-5234"}), "claimed_data")
    assert c["status"] == "fail" and c["code"] == "document_number_mismatch"


def test_gender_word_disagreeing_with_number_goes_to_review():
    d = _check(_read(sex="LELAKI"), "document_data")  # number ends in 2 = female
    assert d["status"] == "review" and d["code"] == "gender_mismatch"


def test_front_and_back_numbers_differ_goes_to_review():
    d = _check(_read(back_number="880101-14-5677"), "document_data")
    assert d["status"] == "review" and d["code"] == "id_number_front_back_mismatch"


def test_moved_address_goes_to_review_not_reject():
    c = _check(_read(claimed={"address": "5 Jalan Ampang, 50450 Kuala Lumpur"}), "claimed_data")
    assert c["status"] == "review" and c["code"] == "address_mismatch" and c["detail"]["postcode_match"] is False


def test_never_looks_for_a_passport_mrz_on_a_mykad(monkeypatch):
    monkeypatch.setattr(document, "_read_mrz", lambda *a: pytest.fail("MRZ searched on a MyKad"))
    _read()


# --- pure text rules -------------------------------------------------------------

def test_name_and_address_from_ocr_lines():
    text = ("KAD PENGENALAN\n8.30825-08-6367 Si\nANNA BINTI ERIK oe |\n"
            "AL. MOHD ZAIN\n12 JALAN MAWAR 3 li cgi\nTAMAN MAWAR i WARGANEGARA :\n43000 KAJANG NEGAR.\nSELANGOR\n")
    card = mykad.read_card([text])
    assert card["document_number"] == "830825-08-6367"
    assert card["full_name"] == "ANNA BINTI ERIK A/L MOHD ZAIN"
    assert card["address"] == "12 JALAN MAWAR 3, TAMAN MAWAR, 43000 KAJANG, SELANGOR"


def test_votes_pick_the_most_read_number():
    texts = ["900514-10-5232"] * 3 + ["900514-10-5282"]
    card = mykad.read_card(texts)
    assert card["document_number"] == "900514-10-5232" and card["confident"]
    assert not mykad.read_card(["900514-10-5232", "900514-10-5282"])["confident"]


def test_ocr_ink_keeps_black_print_and_drops_the_pattern():
    img = images.decode(synth.mykad_back("900514-10-5232"))
    assert "900514105232" in [n for t in ocr.read_texts(img) for n in mykad.numbers_in(t)]


# --- name rules (made-up names, same shape as real ones) ----------------------------
from app.pipeline import names  # noqa: E402

CARD = ["ARJUN SINGH DHILLON A/L HARPAL SINGH"]


@pytest.mark.parametrize("typed", [
    "arjun singh dhillon a/l harpal singh",
    "arjun singh dhillon al harpal singh",
    "arjun singh dhillon harpal singh",
    "arjun singh dhillon",
    "arjun dhillon",
    "Dhillon Arjun",
])
def test_name_variants_that_should_pass(typed):
    assert names.match(typed, CARD)[0], typed


@pytest.mark.parametrize("typed", [
    "singh",                 # one common word
    "arjun",                 # one word of a multi-word name
    "harpal singh",          # father's name only
    "arjun kumar",           # a word not on the card
    "ahmad bin ali",
])
def test_name_variants_that_should_not_pass(typed):
    assert not names.match(typed, CARD)[0], typed


def test_name_tolerates_ocr_slips_and_uses_every_reading():
    assert names.match("arjun singh dhillon", ["ARJUN SIRGH DHILLON A/L HARPAL"])[0]
    # one reading dropped DHILLON, another has it
    assert names.match("arjun singh dhillon", ["ARJUN A/L HARPAL SINGH", "ARJUN SINGH DHILLON"])[0]
    assert names.match("siti aminah binti abu", ["SITI AMINAH BINTI ABU BAKAR"])[0]
    assert names.match("siti aminah", ["SITI AMINAH BINTI ABU BAKAR"])[0]
    assert not names.match("abu bakar", ["SITI AMINAH BINTI ABU BAKAR"])[0]


@pytest.mark.parametrize("card,typed,ok", [
    ("ALI BIN ABU", "ali bin abu", True),
    ("ALI BIN ABU", "ali b. abu", True),
    ("ALI BIN ABU", "ali abu", True),
    ("ALI B. ABU", "ali bin abu", True),
    ("RINA BINTI ABU", "rina binti abu", True),
    ("RINA BINTI ABU", "rina bt. abu", True),
    ("RINA BINTI ABU", "rina abu", True),
    ("RINA BT ABU", "rina binti abu", True),
    ("ALI BIN ABU", "ali", False),        # one word of a two-word name
    ("ALI BIN ABU", "abu", False),        # father's name only
    ("ALI BIN ABU", "rina binti abu", False),
    ("RINA BINTI ABU", "ali abu", False),
])
def test_malay_names(card, typed, ok):
    assert names.match(typed, [card])[0] is ok, (card, typed)


def test_father_name_alone_fails_even_when_ocr_garbles_the_shared_word():
    readings = ["ARJUN SIRGH DHILLON A/L HARPAL", "ARJUN SINGH DHILLON A/L HARPAL SINGH"]
    assert not names.match("harpal singh", readings)[0]
    assert names.match("arjun singh", readings)[0]


def test_order_free_and_single_word_goes_to_review():
    readings = ["ARJUN SINGH DHILLON A/L HARPAL SINGH", "ARJUN"]  # second reading cut off
    assert names.match("harpal singh arjun singh dhillon", readings)[0]
    assert not names.match("arjun", readings)[0]


def test_mykad_recognised_without_country():
    front = images.decode(synth.mykad_front("900514-10-5232", NAME, ADDRESS, "PEREMPUAN", synth.face_bgr()))
    back = images.decode(synth.mykad_back("900514-10-5232"))
    res = document.analyze(front, back, "national_id", None, {"document_number": "900514105232"},
                           today=date(2026, 11, 23))
    assert res.extracted["source"] == "mykad" and _check(res, "document_data")["status"] == "pass"


def test_mrz_noise_with_two_lucky_check_digits_is_ignored():
    from app.pipeline import mrz
    l1 = "I<OLZJZ<ISHS3ZZZZZ7Z7SES4S9IIBP<BNSO"
    number = "B0SYS06J<"
    body = number + mrz.check_digit(number) + "SZZ" + "123456" + "0" + "<" + "300101" + mrz.check_digit("300101") + "5X00C00"
    composite = mrz.check_digit(body[0:10] + body[13:20] + body[21:35])
    l2 = body + str((int(composite) + 1) % 10)  # as in the real case: number and expiry pass, the rest fail
    parsed = mrz.parse_td2(l1, l2)
    assert parsed["valid_count"] == 2 and not parsed["checks"]["composite"]
    assert mrz.find_mrz(f"{l1}\n{l2}") is None
