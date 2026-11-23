"""National identity numbers other than Malaysia's (see mykad.py): find them in
OCR text, validate them, and read what they encode (date of birth, sex).

Checksums: "hard" means a wrong check digit means a misread or a fake number;
"soft" means valid numbers can fail it (Singapore M-series FIN, Korean numbers
issued after Oct 2020); "none" means the number has no check digit, so it can
only be trusted together with the card's own wording.

Where python-stdnum (LGPL) has a tested validator it is used; the others follow
the published algorithms noted next to them.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Callable

from stdnum.cn import ric as cn_ric
from stdnum.id import nik as id_nik
from stdnum.in_ import aadhaar as in_aadhaar
from stdnum.jp import in_ as jp_in
from stdnum.kr import rrn as kr_rrn
from stdnum.pk import cnic as pk_cnic
from stdnum.th import pin as th_pin


@dataclass(frozen=True)
class Spec:
    country: str
    name: str
    find: re.Pattern
    check: str  # hard | soft | none
    read: Callable[[str], dict | None]  # number -> {"valid_checksum": bool, ...} or None if not one


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _ok(fn, number: str) -> bool:
    try:
        return bool(fn(number))
    except Exception:  # noqa: BLE001
        return False


# --- Singapore NRIC / FIN: weights 2765432, +4 for T/G, +3 for M ------------------
def _sgp(raw: str) -> dict | None:
    n = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if not re.fullmatch(r"[STFGM]\d{7}[A-Z]", n):
        return None
    s = sum(int(d) * w for d, w in zip(n[1:8], (2, 7, 6, 5, 4, 3, 2)))
    s += 4 if n[0] in "TG" else 3 if n[0] == "M" else 0
    table = {"S": "JZIHGFEDCBA", "T": "JZIHGFEDCBA", "F": "XWUTRQPNMLK", "G": "XWUTRQPNMLK", "M": "XWUTRQPNJLK"}[n[0]]
    kind = "citizen_or_pr" if n[0] in "ST" else "foreigner"
    return {"number": n, "valid_checksum": table[s % 11] == n[8], "holder": kind}


# --- Hong Kong ID: weights 9..2, single-letter prefix counts as 36 ---------------------
def _hkg(raw: str) -> dict | None:
    n = re.sub(r"[^A-Z0-9]", "", raw.upper())
    m = re.fullmatch(r"([A-Z]{1,2})(\d{6})([0-9A])", n)
    if not m:
        return None
    letters, digits, check = m.groups()
    vals = ([36] if len(letters) == 1 else []) + [ord(c) - 55 for c in letters] + [int(d) for d in digits]
    r = (11 - sum(v * w for v, w in zip(vals, range(9, 1, -1))) % 11) % 11
    return {"number": f"{letters}{digits}({check})", "valid_checksum": ("A" if r == 10 else str(r)) == check}


# --- Taiwan national ID / new UI number ------------------------------------------------
_TW = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
               (10, 11, 12, 13, 14, 15, 16, 17, 34, 18, 19, 20, 21, 22, 35, 23, 24, 25, 26, 27, 28, 29, 32, 30, 31, 33)))


def _twn(raw: str) -> dict | None:
    n = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if not re.fullmatch(r"[A-Z][12389]\d{8}", n):
        return None
    code = _TW[n[0]]
    s = code // 10 + (code % 10) * 9 + sum(int(d) * w for d, w in zip(n[1:9], range(8, 0, -1))) + int(n[9])
    sex = {"1": "M", "2": "F", "8": "M", "9": "F"}.get(n[1])
    return {"number": n, "valid_checksum": s % 10 == 0, "sex": sex,
            "holder": "citizen" if n[1] in "12" else "foreigner"}


# --- library-backed ------------------------------------------------------------------
def _chn(raw: str) -> dict | None:
    n = re.sub(r"[^0-9X]", "", raw.upper())
    if not re.fullmatch(r"\d{17}[\dX]", n):
        return None
    ok = _ok(cn_ric.validate, n)
    out = {"number": n, "valid_checksum": ok, "sex": "M" if int(n[16]) % 2 else "F"}
    if ok:
        out["date_of_birth"] = cn_ric.get_birth_date(n).isoformat()
    return out


def _idn(raw: str) -> dict | None:
    n = _digits(raw)
    if len(n) != 16:
        return None
    try:
        dob = id_nik.get_birth_date(n)
    except Exception:  # noqa: BLE001 - no valid date encoded: not a NIK
        return None
    return {"number": n, "valid_checksum": False, "date_of_birth": dob.isoformat(),
            "sex": "F" if int(n[6:8]) > 40 else "M"}


def _tha(raw: str) -> dict | None:
    n = _digits(raw)
    if len(n) != 13:
        return None
    return {"number": n, "valid_checksum": _ok(th_pin.validate, n)}


def _ind(raw: str) -> dict | None:
    n = _digits(raw)
    if not re.fullmatch(r"[2-9]\d{11}", n):
        return None
    return {"number": n, "valid_checksum": _ok(in_aadhaar.validate, n)}


def _jpn(raw: str) -> dict | None:
    n = _digits(raw)
    if len(n) != 12:
        return None
    return {"number": n, "valid_checksum": _ok(jp_in.validate, n)}


def _kor(raw: str) -> dict | None:
    n = _digits(raw)
    if not re.fullmatch(r"\d{6}[1-8]\d{6}", n):
        return None
    try:
        dob = kr_rrn.get_birth_date(n)
    except Exception:  # noqa: BLE001
        return None
    return {"number": f"{n[:6]}-{n[6:]}", "valid_checksum": _ok(kr_rrn.validate, n), "date_of_birth": dob.isoformat(),
            "sex": "M" if int(n[6]) % 2 else "F", "holder": "citizen" if n[6] in "1234" else "foreigner"}


def _pak(raw: str) -> dict | None:
    n = _digits(raw)
    if len(n) != 13:
        return None
    if not _ok(pk_cnic.validate, n):
        return None
    return {"number": f"{n[:5]}-{n[5:12]}-{n[12]}", "valid_checksum": False, "sex": "M" if int(n[12]) % 2 else "F"}


# --- format only (no public check digit) ---------------------------------------------
def _vnm(raw: str) -> dict | None:
    n = _digits(raw)
    if len(n) != 12 or not (1 <= int(n[:3]) <= 96):
        return None
    century_sex = int(n[3])  # 0/1 = 1900s M/F, 2/3 = 2000s M/F, ...
    year = 1900 + 100 * (century_sex // 2) + int(n[4:6])
    if year > date.today().year:
        return None
    return {"number": n, "valid_checksum": False, "sex": "F" if century_sex % 2 else "M", "birth_year": year}


def _phl(raw: str) -> dict | None:
    n = _digits(raw)
    return {"number": f"{n[:4]}-{n[4:8]}-{n[8:12]}-{n[12:]}", "valid_checksum": False} if len(n) == 16 else None


def _bgd(raw: str) -> dict | None:
    n = _digits(raw)
    return {"number": n, "valid_checksum": False} if len(n) in (10, 13, 17) and n[0] != "0" else None


def _mmr(raw: str) -> dict | None:
    m = re.fullmatch(r"(1[0-4]|[1-9])/([A-Z]{3,9})\((N|E|P|NAING|AE|PYU)\)(\d{6})",
                     re.sub(r"\s", "", raw.upper()))
    if not m:
        return None
    status = {"N": "citizen", "NAING": "citizen", "E": "associate", "AE": "associate", "P": "naturalised",
              "PYU": "naturalised"}[m.group(3)]
    return {"number": f"{m.group(1)}/{m.group(2)}({m.group(3)}){m.group(4)}", "valid_checksum": False, "holder": status}


def _brn(raw: str) -> dict | None:
    m = re.fullmatch(r"(\d{2})-?(\d{6})", re.sub(r"\s", "", raw))
    return {"number": f"{m.group(1)}-{m.group(2)}", "valid_checksum": False} if m else None


_SEP = r"[\s.-]?"
SPECS: tuple[Spec, ...] = (
    Spec("SGP", "NRIC/FIN", re.compile(r"\b[STFGM]\s?\d{7}\s?[A-Z]\b"), "hard", _sgp),
    Spec("HKG", "HKID", re.compile(r"\b[A-Z]{1,2}\d{6}\s?\(?\s?[0-9A]\s?\)?"), "hard", _hkg),
    Spec("TWN", "National ID / UI No.", re.compile(r"\b[A-Z][12389]\d{8}\b"), "hard", _twn),
    Spec("CHN", "Resident ID", re.compile(r"(?<![\dX])\d{17}[\dX](?![\dX])"), "hard", _chn),
    Spec("THA", "National ID", re.compile(rf"(?<!\d)\d{_SEP}\d{{4}}{_SEP}\d{{5}}{_SEP}\d{{2}}{_SEP}\d(?!\d)"), "hard", _tha),
    Spec("IND", "Aadhaar", re.compile(rf"(?<!\d)[2-9]\d{{3}}{_SEP}\d{{4}}{_SEP}\d{{4}}(?!\d)"), "hard", _ind),
    Spec("JPN", "My Number", re.compile(rf"(?<!\d)\d{{4}}{_SEP}\d{{4}}{_SEP}\d{{4}}(?!\d)"), "hard", _jpn),
    Spec("KOR", "Resident Registration No.", re.compile(r"(?<!\d)\d{6}\s?-?\s?[1-8]\d{6}(?!\d)"), "soft", _kor),
    Spec("IDN", "NIK", re.compile(r"(?<!\d)\d{16}(?!\d)"), "none", _idn),
    Spec("PHL", "PhilSys PCN", re.compile(rf"(?<!\d)\d{{4}}{_SEP}\d{{4}}{_SEP}\d{{4}}{_SEP}\d{{4}}(?!\d)"), "none", _phl),
    Spec("VNM", "CCCD", re.compile(r"(?<!\d)\d{12}(?!\d)"), "none", _vnm),
    Spec("PAK", "CNIC", re.compile(r"(?<!\d)\d{5}-?\d{7}-?\d(?!\d)"), "none", _pak),
    Spec("BGD", "NID", re.compile(r"(?<!\d)(?:\d{17}|\d{13}|\d{10})(?!\d)"), "none", _bgd),
    Spec("MMR", "NRC", re.compile(r"\b(?:1[0-4]|[1-9])\s?/\s?[A-Z]{3,9}\s?\(\s?[A-Z]{1,5}\s?\)\s?\d{6}\b"), "none", _mmr),
    Spec("BRN", "IC", re.compile(r"(?<!\d)\d{2}-\d{6}(?!\d)"), "none", _brn),
)
BY_COUNTRY = {s.country: s for s in SPECS}


def _hits(spec: Spec, texts: list[str]) -> list[dict]:
    out = []
    for t in texts:
        for m in spec.find.finditer(t.upper()):
            info = spec.read(m.group())
            if info:
                out.append(info)
    return out


def read(texts: list[str], country: str | None) -> dict | None:
    """The national ID number on the card. With a known country, that country's
    format; without one, only formats whose check digit passes can name the
    country (a bare 12- or 16-digit number fits several countries)."""
    specs = [BY_COUNTRY[country]] if country in BY_COUNTRY else [s for s in SPECS if s.check == "hard"]
    best = None
    for spec in specs:
        hits = _hits(spec, texts)
        if not country:
            hits = [h for h in hits if h["valid_checksum"]]
        if not hits:
            continue
        votes = Counter(h["number"] for h in hits)
        (number, top), *rest = votes.most_common()
        info = next(h for h in hits if h["number"] == number)
        result = {**info, "country": spec.country, "id_format": spec.name, "checksum": spec.check, "votes": top,
                  "confident": top >= 2 and top >= 2 * (rest[0][1] if rest else 0),
                  "candidates": [n for n, _ in votes.most_common(4)]}
        if best is None or (result["valid_checksum"], top) > (best["valid_checksum"], best["votes"]):
            best = result
    return best
