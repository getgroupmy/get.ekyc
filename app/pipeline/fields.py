"""Field readers shared by driver and vehicle documents (Malaysia first).

Formats from JPJ / APAD / PUSPAKOM practice, collected Nov 2026; label wording
on the newest cards and app screens is not published, so readers look for the
value patterns first and use labels only to pick between candidates.
"""
from __future__ import annotations

import re
from datetime import date

# --- dates ------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_MONTHS.update({"MAC": 3, "MEI": 5, "OGO": 8, "OGOS": 8, "OKT": 10, "DIS": 12, "JUNE": 6, "JULY": 7, "SEPT": 9})
_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)")
_DATE_TXT = re.compile(r"(?<!\d)(\d{1,2})[\s-]+([A-Z]{3,5})[\s-]+(\d{4})(?!\d)")
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def dates(text: str) -> list[date]:
    """Every plausible date in the text, in reading order (DD/MM/YYYY, DD MMM YYYY, ISO)."""
    out: list[tuple[int, date]] = []
    t = text.upper()
    for m in _DATE_NUM.finditer(t):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        _add(out, m.start(), y, mo, d)
    for m in _DATE_TXT.finditer(t):
        mo = _MONTHS.get(m.group(2))
        if mo:
            _add(out, m.start(), int(m.group(3)), mo, int(m.group(1)))
    for m in _DATE_ISO.finditer(t):
        _add(out, m.start(), int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return [d for _, d in sorted(out)]


def _add(out: list, pos: int, y: int, mo: int, d: int) -> None:
    if 1900 <= y <= 2100:
        try:
            out.append((pos, date(y, mo, d)))
        except ValueError:
            pass


def validity(text: str, today: date | None = None) -> dict:
    """Start and expiry of a validity period: the latest date is the expiry, the
    one before it the start. Dates far in the past (birth dates) are ignored."""
    today = today or date.today()
    ds = sorted({d for d in dates(text) if d.year >= today.year - 15})
    if not ds:
        return {}
    out = {"expiry_date": ds[-1].isoformat()}
    if len(ds) > 1:
        out["start_date"] = ds[-2].isoformat()
    return out


# --- Malaysian plates ------------------------------------------------------------
# Letters I and O are never used; Z is military. Peninsular state prefixes, Sabah
# S*, Sarawak Q*, taxis H*, KLIA LIMO, and vanity series as a loose fallback.
_PLATE = re.compile(
    r"\b("
    r"(?:A|B|C|D|F|J|K|KV|M|N|P|R|T|V|W|L)[A-HJ-NP-Y]{0,2}\s?[1-9]\d{0,3}\s?[A-HJ-NP-Y]?"
    r"|S[A-HJ-NP-Z]{1,2}\s?[1-9]\d{0,3}\s?[A-HJ-NP-RT-Y]?"
    r"|Q[A-HJ-NP-Y]{1,2}\s?[1-9]\d{0,3}\s?[A-HJ-NP-Y]?"
    r"|H[A-DJ-NP-TW][A-HJ-NP-Y]?\s?[1-9]\d{0,3}"
    r"|LIMO\s?[1-9]\d{0,3}\s?[BW]"
    r"|Z[A-HJ-NP-Z]\s?[1-9]\d{0,3}"
    r"|(?:PUTRAJAYA|MALAYSIA|PATRIOT|PROTON|PERODUA|G1M|VIP|US|UP|GTR|BAMBEE|IM4U|SAM|NBOS|TIADA)\s?[1-9]\d{0,3}\s?[A-Z]?"
    r")\b")
_PLATE_LABELS = ("NO. PENDAFTARAN", "NO PENDAFTARAN", "NO. KENDERAAN", "REGISTRATION", "NOMBOR PENDAFTARAN",
                 "VEHICLE NO", "NO KENDERAAN", "REG. NO", "REG NO")


def plates(text: str) -> list[str]:
    t = text.upper()
    found = []
    for m in _PLATE.finditer(t):
        p = re.sub(r"\s", "", m.group(1))
        if len(p) >= 2 and any(c.isdigit() for c in p) and any(c.isalpha() for c in p):
            found.append(p)
    return found


def plate(text: str) -> str | None:
    """The registration number: one next to a plate label first, else the most frequent."""
    t = text.upper()
    for line in t.splitlines():
        if any(lbl in line for lbl in _PLATE_LABELS):
            near = plates(line.split(":", 1)[-1])
            if near:
                return near[0]
    ps = plates(t)
    return max(set(ps), key=ps.count) if ps else None


# --- vehicle identifiers ------------------------------------------------------------
_VIN = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
_FRAME = re.compile(r"\b([A-Z0-9]{2,8}-\d{5,8})\b")  # imported Japanese frame numbers, NCP93-1234567


def chassis(text: str) -> str | None:
    t = text.upper()
    for line in t.splitlines():
        if re.search(r"CASIS|CHASSIS|VIN", line):
            m = _VIN.search(line) or _FRAME.search(line)
            if m:
                return m.group(1)
    m = next((m for m in _VIN.finditer(t) if re.search(r"\d", m.group(1)) and re.search(r"[A-Z]", m.group(1))), None)
    return m.group(1) if m else None


def engine_number(text: str) -> str | None:
    for line in text.upper().splitlines():
        m = re.search(r"(?:NO\.?\s*)?(?:ENJIN|ENGINE|MOTOR)\s*(?:NO\.?)?\s*[:.]?\s*([A-Z0-9-]{5,20})", line)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1)
    return None


def year_made(text: str) -> int | None:
    for line in text.upper().splitlines():
        if re.search(r"TAHUN\s*(DIBUAT|BUATAN)|YEAR\s*(OF\s*)?(MANUFACTURE|MADE)", line):
            m = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", line)
            if m:
                return int(m.group(1))
    return None


def engine_cc(text: str) -> int | None:
    for line in text.upper().splitlines():
        if re.search(r"KEUPAYAAN|CAPACITY|\bCC\b|SUKATAN", line):
            m = re.search(r"\b(\d{2,5})\s*(?:CC)?\b", line.split(":", 1)[-1])
            if m and 49 <= int(m.group(1)) <= 20000:
                return int(m.group(1))
    return None


def company_number(text: str) -> str | None:
    t = text.upper()
    m = re.search(r"(?<!\d)(\d{4}0[1-6]\d{6})(?!\d)", t) or re.search(r"(?<![\dA-Z])(\d{1,7}-[A-Z])(?![A-Z\d])", t)
    return m.group(1) if m else None


# --- driving licence ------------------------------------------------------------------
_CLASSES = ("A", "A1", "B", "B1", "B2", "C", "D", "DA", "E", "E1", "E2", "F", "G", "H", "I", "M")
_RARE = {"A", "F", "G", "H", "I", "M"}


def licence_classes(text: str) -> list[str]:
    """Classes printed after KELAS / CLASS (e.g. "D DA B2")."""
    found: list[str] = []
    for line in text.upper().splitlines():
        m = re.search(r"\b(?:KELAS|CLASS)\b\s*[:.]?\s*(.+)", line)
        if not m:
            continue
        for tok in re.split(r"[\s,/]+", m.group(1)):
            tok = tok.strip(".")
            # Rare classes (A, F, G, H, I, M) after common ones are usually OCR
            # noise from the photo next to the line.
            if found and tok in _RARE and any(c not in _RARE for c in found):
                break
            if tok in _CLASSES and tok not in found:
                found.append(tok)
            elif tok and tok not in _CLASSES and found:
                break
    return found


def licence_flags(text: str) -> dict:
    t = text.upper()
    out = {}
    kind = ("LDL" if re.search(r"\bLDL\b|LESEN BELAJAR|LEARNER", t) else
            "PDL" if re.search(r"\bPDL\b|PERCUBAAN|PROBATION", t) else
            "CDL" if re.search(r"\bCDL\b|KOMPETEN|COMPETENT", t) else None)
    if kind:
        out["licence_type"] = kind
    out["psv"] = bool(re.search(r"\bPSV\b|PERKHIDMATAN AWAM", t))
    out["gdl"] = bool(re.search(r"\bGDL\b|BARANG", t))
    out["ehailing"] = bool(re.search(r"E[\s-]?HAILING", t))
    out["vocational"] = out["psv"] or out["gdl"] or "VOKASIONAL" in t
    return out


# --- insurance ---------------------------------------------------------------------
INSURERS = ("ALLIANZ", "ETIQA", "TAKAFUL IKHLAS", "ZURICH", "AMGENERAL", "LIBERTY", "TOKIO MARINE", "RHB",
            "BERJAYA SOMPO", "PACIFIC & ORIENT", "P&O", "LONPAC", "MSIG", "AIG", "CHUBB", "GENERALI", "AXA",
            "SYARIKAT TAKAFUL", "TAKAFUL MALAYSIA", "QBE", "TUNE PROTECT", "KURNIA", "BERJAYA", "DAGANG NET")


def insurer(text: str) -> str | None:
    t = text.upper()
    return next((n for n in INSURERS if n in t), None)


def reference_number(text: str, labels: tuple[str, ...]) -> str | None:
    """A document number printed after one of the labels (policy no., permit no., report no.)."""
    for line in text.upper().splitlines():
        for lbl in labels:
            i = line.find(lbl)
            if i >= 0:
                m = re.search(r"([A-Z0-9][A-Z0-9/-]{5,24})", line[i + len(lbl):].lstrip(" :.#"))
                if m and re.search(r"\d", m.group(1)):
                    return m.group(1)
    return None


_NRIC = re.compile(r"(?<!\d)(\d{6}-?\d{2}-?\d{4})(?!\d)")


def nric_in(text: str) -> str | None:
    m = _NRIC.search(text)
    return m.group(1) if m else None
