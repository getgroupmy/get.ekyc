"""Malaysian MyKad (NRIC): number parsing and reading the card.

The 12-digit number is YYMMDD-PB-###G: date of birth, place-of-birth code, and
a serial whose last digit is odd for a man and even for a woman. The front also
prints LELAKI (male) / PEREMPUAN (female) and WARGANEGARA (citizen); the back
repeats the number followed by the card's issue code.

The address under the name is read as lines plus postcode, city and state, so
it can be checked against what the user typed. Deliberately NOT read: the
religion some cards print (ISLAM) - sensitive personal data under the PDPA,
and not needed for KYC.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date

from rapidfuzz import fuzz

# Place-of-birth codes issued by JPN (states + foreign countries).
_STATE_CODES = {
    "01": "Johor", "21": "Johor", "22": "Johor", "23": "Johor", "24": "Johor",
    "02": "Kedah", "25": "Kedah", "26": "Kedah", "27": "Kedah",
    "03": "Kelantan", "28": "Kelantan", "29": "Kelantan",
    "04": "Melaka", "30": "Melaka",
    "05": "Negeri Sembilan", "31": "Negeri Sembilan", "59": "Negeri Sembilan",
    "06": "Pahang", "32": "Pahang", "33": "Pahang",
    "07": "Pulau Pinang", "34": "Pulau Pinang", "35": "Pulau Pinang",
    "08": "Perak", "36": "Perak", "37": "Perak", "38": "Perak", "39": "Perak",
    "09": "Perlis", "40": "Perlis",
    "10": "Selangor", "41": "Selangor", "42": "Selangor", "43": "Selangor", "44": "Selangor",
    "11": "Terengganu", "45": "Terengganu", "46": "Terengganu",
    "12": "Sabah", "47": "Sabah", "48": "Sabah", "49": "Sabah",
    "13": "Sarawak", "50": "Sarawak", "51": "Sarawak", "52": "Sarawak", "53": "Sarawak",
    "14": "Kuala Lumpur", "54": "Kuala Lumpur", "55": "Kuala Lumpur", "56": "Kuala Lumpur", "57": "Kuala Lumpur",
    "15": "Labuan", "58": "Labuan",
    "16": "Putrajaya",
    "82": "Unknown state",
}
_FOREIGN = {str(n) for n in range(60, 80)} | {"83", "84", "85", "86", "87", "88", "89", "90", "91", "92", "93", "98", "99"}

_PATTERN = re.compile(r"(?<!\d)(\d{6})\s?[-–—]?\s?(\d{2})\s?[-–—]?\s?(\d{4})(?!\d)")


def parse_number(number: str, today: date | None = None) -> dict | None:
    """Validate a 12 digit NRIC number and derive DOB / gender / place of birth."""
    today = today or date.today()
    digits = re.sub(r"\D", "", number)
    if len(digits) != 12:
        return None
    yy, mm, dd, pb = int(digits[:2]), int(digits[2:4]), int(digits[4:6]), digits[6:8]
    year = 2000 + yy if 2000 + yy <= today.year else 1900 + yy
    try:
        dob = date(year, mm, dd)
    except ValueError:
        return None
    if dob > today:
        return None
    if pb in _STATE_CODES:
        place = _STATE_CODES[pb]
    elif pb in _FOREIGN:
        place = "Born outside Malaysia"
    else:
        return None
    return {
        "document_number": f"{digits[:6]}-{digits[6:8]}-{digits[8:]}",
        "date_of_birth": dob.isoformat(),
        "sex": "M" if int(digits[-1]) % 2 else "F",
        "place_of_birth": place,
    }


def _join_digit_runs(text: str) -> str:
    """OCR often puts a dot or space inside a printed number (8.30825)."""
    return re.sub(r"(?<=\d)[ .,'’`](?=\d)", "", text)


def numbers_in(text: str) -> list[str]:
    """Every valid NRIC number in the text, normalised to 12 digits."""
    out = []
    for m in _PATTERN.finditer(_join_digit_runs(text)):
        digits = "".join(m.groups())
        if parse_number(digits):
            out.append(digits)
    return out


def find_in_text(text: str) -> dict | None:
    for digits in numbers_in(text):
        return parse_number(digits)
    return None


_CONNECTORS = ("BIN", "BINTI", "BT", "B", "A/L", "A/P", "AL", "AP", "@", "S/O", "D/O")
# Printed in the right-hand column; OCR often appends bits of them to a line.
_RIGHT_COLUMN = ("WARGANEGARA", "LELAKI", "PEREMPUAN", "ISLAM", "PEMASTAUTIN", "TETAP")
_HEADER = {"KAD", "PENGENALAN", "MALAYSIA", "IDENTITY", "CARD", "MYKAD"}
STATES = ("SELANGOR", "JOHOR", "KEDAH", "KELANTAN", "MELAKA", "NEGERI SEMBILAN", "PAHANG", "PULAU PINANG",
          "PENANG", "PERAK", "PERLIS", "SABAH", "SARAWAK", "TERENGGANU", "KUALA LUMPUR", "LABUAN", "PUTRAJAYA")


def _is_right_column(token: str) -> bool:
    t = token.strip(".,:;")
    return len(t) >= 4 and any(fuzz.partial_ratio(t, w) >= 90 for w in _RIGHT_COLUMN)


def _printed_run(line: str, digits: bool) -> list[str]:
    """The leading run of tokens that look printed on the card: capitals (and
    digits / unit separators for addresses). Everything from the first token
    that isn't - lowercase OCR noise from the photo or pattern, stray symbols,
    the right-hand column - is dropped."""
    allowed = r"[A-Z0-9][A-Z0-9/'@.,&-]*" if digits else r"[A-Z][A-Z/'@.-]*|@"
    out: list[str] = []
    for tok in line.split():
        if not re.fullmatch(allowed, tok) or _is_right_column(tok):
            break
        if not digits and len(tok) == 1 and tok not in ("@", "B"):
            break
        out.append(tok.strip(","))
    if not digits:  # OCR reads A/L as AL. or A/L. - write the connectors one way
        fix = {"AL": "A/L", "AP": "A/P", "A/L": "A/L", "A/P": "A/P", "BT": "BT", "S/O": "S/O", "D/O": "D/O"}
        out = [fix.get(t.rstrip(".").replace(".", ""), t.rstrip(".") if t.endswith(".") and len(t) <= 4 else t)
               for t in out]
    return [t for t in out if t]


def _lines_after_number(text: str, number: str) -> list[str] | None:
    lines = [ln.strip() for ln in _join_digit_runs(text).splitlines() if ln.strip()]
    start = next((i for i, ln in enumerate(lines) if number in re.sub(r"\D", "", ln)), None)
    return None if start is None else lines[start + 1:]


def _name_and_rest(lines: list[str]) -> tuple[str | None, list[str]]:
    """The name is the first printed line under the number (OCR noise lines
    between them are skipped). A second line is kept only when it continues the
    name (BIN, BINTI, A/L, A/P, @ ...), so an address line is not taken for one."""
    parts: list[str] = []
    i = 0
    while i < len(lines) and i < 4:
        words = _printed_run(lines[i], digits=False)
        if parts and re.match(r"\S*\d", lines[i]):  # the address starts (C-1-6 ..., 12 JALAN ...)
            break
        if len(" ".join(words)) < 3 or set(words) & _HEADER:
            i += 1
            if parts:
                break
            continue
        if parts and not (words[0] in _CONNECTORS or parts[-1].split()[-1] in _CONNECTORS):
            break
        parts.append(" ".join(words))
        i += 1
        if len(parts) == 2:
            break
    return (" ".join(parts) or None), lines[i:]


def _address_from(lines: list[str]) -> dict | None:
    """Address lines up to and including the one naming the state; the postcode
    is the 5-digit number on the line before it (43000 KAJANG)."""
    out: list[str] = []
    state = None
    for ln in lines[:7]:
        words = _printed_run(ln, digits=True)
        if not words:
            continue
        joined = " ".join(words)
        out.append(joined)
        state = next((st for st in STATES if st in joined), None)
        if state:
            break
    if not out or not state:
        return None
    if not any(re.match(r"\d{5}\s", ln) for ln in out):
        return None  # every MyKad address has a postcode line; without one it is noise
    postcode, city = None, None
    for ln in reversed(out):
        m = re.match(r"(\d{5})\s+(.+)", ln)
        if m:
            postcode, city = m.group(1), m.group(2)
            break
    return {"address": ", ".join(out), "postcode": postcode, "city": city, "state": state}


def _has_word(texts: list[str], word: str) -> bool:
    words = {w for t in texts for w in re.findall(r"[A-Z]{4,}", t.upper())}
    return any(fuzz.ratio(word, w) >= 85 for w in words)


def read_card(front_texts: list[str], back_texts: list[str] | None = None) -> dict | None:
    """Vote over every OCR reading of both sides. Returns None when no valid
    number was read at all."""
    back_texts = back_texts or []
    front_votes = Counter(n for t in front_texts for n in numbers_in(t))
    back_votes = Counter(n for t in back_texts for n in numbers_in(t))
    votes = front_votes + back_votes
    if not votes:
        return None
    (number, top), *rest = votes.most_common()
    runner_up = rest[0][1] if rest else 0
    parsed = parse_number(number)
    assert parsed is not None
    printed_sex = ("M" if _has_word(front_texts, "LELAKI") else
                   "F" if _has_word(front_texts, "PEREMPUAN") else None)
    citizenship = ("citizen" if _has_word(front_texts, "WARGANEGARA") else
                   "permanent_resident" if _has_word(front_texts, "PEMASTAUTIN") else None)
    name, address = None, None
    readings: list[str] = []
    for t in front_texts:  # the most complete reading wins
        lines = _lines_after_number(t, number)
        if lines is None:
            continue
        n, rest = _name_and_rest(lines)
        if n:
            readings.append(n)
        a = _address_from(rest) if n else None
        if n and (name is None or (a and not address) or (a and address and len(a["address"]) > len(address["address"]))):
            name, address = n, a or address
    name = name and re.sub(r"\s+", " ", name)
    side = lambda c: c.most_common(1)[0][0] if c else None  # noqa: E731
    return {
        **parsed,
        "full_name": name,
        "name_readings": readings,
        **(address or {}),
        "printed_sex": printed_sex,
        "citizenship": citizenship,
        "front_number": side(front_votes),
        "back_number": side(back_votes) if back_texts else None,
        "votes": top,
        "confident": top >= 2 and top >= 2 * runner_up,
        "candidates": [n for n, _ in votes.most_common(4)],
    }
