"""Which country issued the document, from the image itself.

Strongest evidence first:
1. the MRZ issuing state (passports, ID cards with an MRZ);
2. a confidently read MyKad number (Malaysia);
3. wording printed on the card ("KAD PENGENALAN MALAYSIA", "REPUBLIC OF
   SINGAPORE", "REPUBLIK INDONESIA" ...). A country's own phrase counts 2,
   its bare name 1; it needs 2 to be named, and a clear lead over the next.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

# ISO 3166-1 alpha-3 -> (specific phrases, weak words)
_WORDING: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "MYS": (("KAD PENGENALAN", "MYKAD", "WARGANEGARA", "LESEN MEMANDU", "PEMASTAUTIN TETAP", "MYPR"),
            ("MALAYSIA",)),
    "SGP": (("REPUBLIC OF SINGAPORE", "IDENTITY CARD NO", "SINGAPORE CITIZEN"), ("SINGAPORE",)),
    "IDN": (("REPUBLIK INDONESIA", "KEWARGANEGARAAN", "PROVINSI", "KARTU TANDA PENDUDUK"), ("INDONESIA",)),
    "THA": (("KINGDOM OF THAILAND", "THAI NATIONAL ID"), ("THAILAND",)),
    "PHL": (("REPUBLIC OF THE PHILIPPINES", "REPUBLIKA NG PILIPINAS", "PILIPINAS"), ("PHILIPPINES",)),
    "BRN": (("NEGARA BRUNEI DARUSSALAM", "BRUNEI DARUSSALAM"), ("BRUNEI",)),
    "IND": (("GOVERNMENT OF INDIA", "REPUBLIC OF INDIA", "AADHAAR"), ("INDIA",)),
    "BGD": (("PEOPLE'S REPUBLIC OF BANGLADESH", "NATIONAL ID CARD BANGLADESH"), ("BANGLADESH",)),
    "NPL": (("GOVERNMENT OF NEPAL",), ("NEPAL",)),
    "MMR": (("REPUBLIC OF THE UNION OF MYANMAR",), ("MYANMAR",)),
    "VNM": (("SOCIALIST REPUBLIC OF VIET NAM", "CAN CUOC CONG DAN"), ("VIET NAM", "VIETNAM")),
    "CHN": (("PEOPLE'S REPUBLIC OF CHINA",), ("CHINA",)),
    "PAK": (("ISLAMIC REPUBLIC OF PAKISTAN",), ("PAKISTAN",)),
}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^A-Z' ]", " ", text.upper()).split())


def _found(phrase: str, lines: list[str], flat: str) -> bool:
    if phrase in flat:
        return True
    if len(phrase) < 8:  # short words must be read exactly
        return False
    return any(fuzz.partial_ratio(phrase, ln) >= 90 for ln in lines if len(ln) >= len(phrase) - 2)


def from_wording(texts: list[str]) -> tuple[str | None, dict]:
    lines = list(dict.fromkeys(_norm(ln) for t in texts for ln in t.splitlines() if ln.strip()))
    flat = " ".join(lines)
    scores = {}
    for code, (strong, weak) in _WORDING.items():
        score = 2 * sum(_found(p, lines, flat) for p in strong) + sum(_found(w, lines, flat) for w in weak)
        if score:
            scores[code] = score
    if not scores:
        return None, {}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, score = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0
    if score >= 2 and score >= runner + 2:
        return top, scores
    return None, scores


def detect(texts: list[str], mrz_country: str | None = None, mykad_confident: bool = False) -> dict:
    """{"country", "source", "wording_scores"}; country is None when unsure."""
    wording, scores = from_wording(texts)
    if mrz_country:
        return {"country": mrz_country, "source": "mrz", "wording_scores": scores}
    if mykad_confident:
        return {"country": "MYS", "source": "mykad_number", "wording_scores": scores}
    return {"country": wording, "source": "wording" if wording else None, "wording_scores": scores}
