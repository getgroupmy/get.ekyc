"""Malaysian MyKad (NRIC) number parsing: YYMMDD-PB-###G."""
from __future__ import annotations

import re
from datetime import date

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


def find_in_text(text: str) -> dict | None:
    for m in _PATTERN.finditer(text):
        parsed = parse_number("".join(m.groups()))
        if parsed:
            return parsed
    return None
