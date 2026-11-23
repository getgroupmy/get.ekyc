"""ICAO 9303 machine readable zone parsing with check-digit validation.

Supports TD3 (passports, 2x44), TD2 (2x36) and TD1 (ID cards, 3x30).
"""
from __future__ import annotations

import re
from datetime import date

_WEIGHTS = (7, 3, 1)
_TO_DIGIT = str.maketrans("OQDIZSBG", "00012586")
_TO_ALPHA = str.maketrans("012586", "OIZSBG")


def char_value(c: str) -> int:
    if c.isdigit():
        return int(c)
    if "A" <= c <= "Z":
        return ord(c) - 55
    return 0  # '<'


def check_digit(s: str) -> str:
    return str(sum(char_value(c) * _WEIGHTS[i % 3] for i, c in enumerate(s)) % 10)


def _digits(s: str) -> str:
    return s.translate(_TO_DIGIT)


def _alpha(s: str) -> str:
    return s.translate(_TO_ALPHA)


def _date(yymmdd: str, *, expiry: bool, today: date | None = None) -> str | None:
    today = today or date.today()
    if not re.fullmatch(r"\d{6}", yymmdd):
        return None
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    if expiry:
        year = 2000 + yy
    else:
        year = 2000 + yy if yy <= today.year % 100 else 1900 + yy
    try:
        return date(year, mm, dd).isoformat()
    except ValueError:
        return None


def _names(field: str) -> tuple[str, str]:
    field = _alpha(field)
    surname, _, given = field.partition("<<")
    clean = lambda s: " ".join(s.replace("<", " ").split())  # noqa: E731
    return clean(surname), clean(given)


def _fit(line: str, n: int) -> str | None:
    if abs(len(line) - n) > 2:
        return None
    return line[:n].ljust(n, "<")


def _result(fmt: str, checks: dict[str, bool], **fields) -> dict:
    return {
        "format": fmt,
        **fields,
        "checks": checks,
        "valid": all(checks.values()),
        "valid_count": sum(checks.values()),
    }


def _fix(line: str, *spans: tuple[int, int]) -> str:
    """Normalise look-alike letters to digits inside purely numeric spans."""
    for a, b in spans:
        line = line[:a] + _digits(line[a:b]) + line[b:]
    return line


def parse_td3(l1: str, l2: str) -> dict:
    l2 = _fix(l2, (9, 10), (13, 20), (21, 28), (43, 44))
    surname, given = _names(l1[5:44])
    number, dob, exp = l2[0:9], _digits(l2[13:19]), _digits(l2[21:27])
    personal = l2[28:42]
    composite = l2[0:10] + l2[13:20] + l2[21:43]
    pcd = _digits(l2[42])
    checks = {
        "document_number": check_digit(number) == _digits(l2[9]),
        "date_of_birth": check_digit(dob) == _digits(l2[19]),
        "expiry_date": check_digit(exp) == _digits(l2[27]),
        "personal_number": check_digit(personal) == pcd or (pcd in "<0" and set(personal) <= {"<"}),
        "composite": check_digit(composite) == _digits(l2[43]),
    }
    return _result(
        "TD3", checks,
        document_code=l1[0:2].replace("<", ""), issuing_country=_alpha(l1[2:5]).replace("<", ""),
        surname=surname, given_names=given, document_number=number.replace("<", ""),
        nationality=_alpha(l2[10:13]).replace("<", ""), date_of_birth=_date(dob, expiry=False),
        sex=l2[20] if l2[20] in "MF" else None, expiry_date=_date(exp, expiry=True),
        personal_number=personal.replace("<", "") or None,
    )


def parse_td2(l1: str, l2: str) -> dict:
    l2 = _fix(l2, (9, 10), (13, 20), (21, 28), (35, 36))
    surname, given = _names(l1[5:36])
    number, dob, exp = l2[0:9], _digits(l2[13:19]), _digits(l2[21:27])
    composite = l2[0:10] + l2[13:20] + l2[21:35]
    checks = {
        "document_number": check_digit(number) == _digits(l2[9]),
        "date_of_birth": check_digit(dob) == _digits(l2[19]),
        "expiry_date": check_digit(exp) == _digits(l2[27]),
        "composite": check_digit(composite) == _digits(l2[35]),
    }
    return _result(
        "TD2", checks,
        document_code=l1[0:2].replace("<", ""), issuing_country=_alpha(l1[2:5]).replace("<", ""),
        surname=surname, given_names=given, document_number=number.replace("<", ""),
        nationality=_alpha(l2[10:13]).replace("<", ""), date_of_birth=_date(dob, expiry=False),
        sex=l2[20] if l2[20] in "MF" else None, expiry_date=_date(exp, expiry=True),
        personal_number=l2[28:35].replace("<", "") or None,
    )


def parse_td1(l1: str, l2: str, l3: str) -> dict:
    l1 = _fix(l1, (14, 15))
    l2 = _fix(l2, (0, 7), (8, 15), (29, 30))
    surname, given = _names(l3)
    number, dob, exp = l1[5:14], _digits(l2[0:6]), _digits(l2[8:14])
    composite = l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29]
    checks = {
        "document_number": check_digit(number) == _digits(l1[14]),
        "date_of_birth": check_digit(dob) == _digits(l2[6]),
        "expiry_date": check_digit(exp) == _digits(l2[14]),
        "composite": check_digit(composite) == _digits(l2[29]),
    }
    return _result(
        "TD1", checks,
        document_code=l1[0:2].replace("<", ""), issuing_country=_alpha(l1[2:5]).replace("<", ""),
        surname=surname, given_names=given, document_number=number.replace("<", ""),
        nationality=_alpha(l2[15:18]).replace("<", ""), date_of_birth=_date(dob, expiry=False),
        sex=l2[7] if l2[7] in "MF" else None, expiry_date=_date(exp, expiry=True),
        personal_number=l1[15:30].replace("<", "") or None,
    )


def find_mrz(text: str) -> dict | None:
    """Find the best MRZ candidate in raw OCR text (tolerant of OCR noise)."""
    lines = []
    for raw in text.upper().splitlines():
        line = re.sub(r"[^A-Z0-9<]", "", raw.replace("«", "<<").replace(" ", ""))
        if len(line) >= 28 and (line.count("<") >= 2 or sum(c.isdigit() for c in line) >= 8):
            lines.append(line)
    best: dict | None = None

    def consider(candidate: dict | None) -> None:
        nonlocal best
        if candidate and (best is None or candidate["valid_count"] / len(candidate["checks"])
                          > best["valid_count"] / len(best["checks"])):
            best = candidate

    for i in range(len(lines)):
        if i + 1 < len(lines):
            a, b = lines[i], lines[i + 1]
            for n, fn in ((44, parse_td3), (36, parse_td2)):
                fa, fb = _fit(a, n), _fit(b, n)
                if fa and fb and "<" in fa:
                    consider(fn(fa, fb))
        if i + 2 < len(lines):
            f = [_fit(x, 30) for x in lines[i:i + 3]]
            if all(f):
                consider(parse_td1(*f))  # type: ignore[arg-type]
    if best and best["valid_count"] == 0:
        return None
    return best
