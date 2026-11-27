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
    lines = text.upper().splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"\b(?:KELAS|CLASS)\b\s*[:.]?\s*(.*)", line)
        if not m:
            continue
        rest = re.sub(r"^[\s/:.]*(?:KELAS|CLASS)\b", "", m.group(1)).strip()
        # "Kelas / Class" alone on its line: the classes are on the next one
        if not any(t.strip(".") in _CLASSES for t in re.split(r"[\s,/]+", rest)) and i + 1 < len(lines):
            rest = lines[i + 1]
        for tok in re.split(r"[\s,/]+", rest):
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


# --- the holder, read off labelled fields -----------------------------------------------
# For cards without a machine-readable zone or a known layout (i-Kad, UNHCR card,
# foreign ID cards, driving licences): "NAMA / NAME" then the name on the same
# line or the next, "TARIKH LAHIR / DATE OF BIRTH", "JANTINA / SEX",
# "WARGANEGARA / NATIONALITY". Several OCR readings vote.
_NAME_LABEL = re.compile(r"^\s*(?:NAMA\s*LENGKAP|FULL\s*NAME|NAMA|NAME|NOM)\b(?:\s*/\s*(?:NAMA|NAME))?\s*[:.\-]?\s*(.*)$")
_DOB_LABEL = re.compile(r"(?:DATE\s*OF\s*BIRTH|BIRTH\s*DATE|TARIKH\s*LAHIR|TGL\.?\s*LAHIR|TANGGAL\s*LAHIR|\bDOB\b|\bD\.O\.B\b)[^0-9A-Z]*(.*)$")
_SEX_LABEL = re.compile(r"(?:\bSEX\b|JANTINA|JENIS\s*KELAMIN|\bGENDER\b|\bKELAMIN\b)\s*[:./]*\s*(.*)$")
_NAT_LABEL = re.compile(r"(?:WARGANEGARA|KEWARGANEGARAAN|NATIONALITY|CITIZENSHIP|COUNTRY\s*OF\s*ORIGIN|\bORIGIN\b)\s*[:./]*\s*(.*)$")
_NOT_NAME = {"NAME", "NAMA", "NO", "NUMBER", "DATE", "BIRTH", "TARIKH", "LAHIR", "SEX", "JANTINA", "CLASS", "KELAS",
             "VALIDITY", "TEMPOH", "ADDRESS", "ALAMAT", "PASSPORT", "PASPORT", "IDENTITY", "PENGENALAN", "NATIONALITY",
             "WARGANEGARA", "MALAYSIA", "LICENCE", "LESEN", "CARD", "KAD", "EXPIRY", "LUPUT", "VALID", "UNTIL"}
NATIONALITIES = {
    "MALAYSIA": "MYS", "MALAYSIAN": "MYS", "INDONESIA": "IDN", "INDONESIAN": "IDN", "WNI": "IDN", "INDIA": "IND",
    "INDIAN": "IND", "BANGLADESH": "BGD", "BANGLADESHI": "BGD", "NEPAL": "NPL", "NEPALI": "NPL", "MYANMAR": "MMR",
    "BURMA": "MMR", "PAKISTAN": "PAK", "PAKISTANI": "PAK", "PHILIPPINES": "PHL", "FILIPINO": "PHL", "VIETNAM": "VNM",
    "VIET NAM": "VNM", "VIETNAMESE": "VNM", "THAILAND": "THA", "THAI": "THA", "CAMBODIA": "KHM", "SRI LANKA": "LKA",
    "CHINA": "CHN", "CHINESE": "CHN", "SINGAPORE": "SGP", "SINGAPOREAN": "SGP", "BRUNEI": "BRN", "LAOS": "LAO",
    "AFGHANISTAN": "AFG", "SOMALIA": "SOM", "YEMEN": "YEM", "SYRIA": "SYR", "IRAQ": "IRQ", "IRAN": "IRN",
    "SUDAN": "SDN", "UZBEKISTAN": "UZB", "TURKMENISTAN": "TKM", "KAZAKHSTAN": "KAZ",
}


_LABEL_WORDS = re.compile(r"^(?:[\s/:.,-]*(?:SEX|JANTINA|JENIS\s*KELAMIN|GENDER|KELAMIN|NATIONALITY|WARGANEGARA|"
                          r"KEWARGANEGARAAN|CITIZENSHIP|COUNTRY\s*OF\s*ORIGIN|ORIGIN))+[\s/:.,-]*")


def _value(s: str) -> str:
    """The value after a bilingual label: "JANTINA / SEX: LELAKI" -> "LELAKI"."""
    return _LABEL_WORDS.sub("", s).strip()


def _clean_name(s: str) -> str | None:
    s = re.sub(r"[^A-Z@'/.\- ]", " ", s.upper())
    words = [w for w in s.split() if w]
    while words and (words[0] in _NOT_NAME or len(words[0]) == 1 and words[0] not in ("@",)):
        words.pop(0)
    while words and (words[-1] in _NOT_NAME or len(words[-1]) == 1):
        words.pop()
    if not words or len(words) > 7 or any(w in _NOT_NAME for w in words):
        return None
    name = " ".join(words)
    # a real name has a word of three letters or more; "IF OY" is noise
    if not any(sum(c.isalpha() for c in w) >= 3 for w in words) or sum(c.isalpha() for c in name) < 4:
        return None
    return name


def _sex(s: str) -> str | None:
    s = s.upper()
    if re.match(r"^\s*(LAKI[\s-]*LAKI|LELAKI|MALE|PRIA|M\b|L\b)", s):
        return "M"
    if re.match(r"^\s*(PEREMPUAN|FEMALE|WANITA|F\b|P\b)", s):
        return "F"
    return None


def _nationality(s: str) -> str | None:
    s = " ".join(re.sub(r"[^A-Z ]", " ", s.upper()).split())
    for name in sorted(NATIONALITIES, key=len, reverse=True):
        if re.match(rf"{name}\b", s):
            return NATIONALITIES[name]
    return None


def holder(texts: list[str], today: date | None = None) -> dict:
    """Name, date of birth, sex and nationality printed after their labels."""
    today = today or date.today()
    votes: dict[str, list] = {"full_name": [], "date_of_birth": [], "sex": [], "nationality": []}
    for n, text in enumerate(texts):
        weight = 2 if n == 0 else 1  # the first reading is the cleanest (RapidOCR)
        lines = [ln for ln in text.upper().splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            after = lines[i + 1] if i + 1 < len(lines) else ""
            if m := _NAME_LABEL.match(line):
                name = _clean_name(m.group(1)) or _clean_name(after)
                if name:
                    votes["full_name"] += [name] * weight
            if m := _DOB_LABEL.search(line):
                ds = [d for d in dates(m.group(1) or after) if d < today and today.year - d.year <= 110]
                if ds:
                    votes["date_of_birth"] += [ds[0].isoformat()] * weight
            if m := _SEX_LABEL.search(line):
                v = _value(m.group(1))
                if sx := _sex(v) or (_sex(after) if not v else None):
                    votes["sex"] += [sx] * weight
            if m := _NAT_LABEL.search(line):
                v = _value(m.group(1))
                if nat := _nationality(v) or (_nationality(after) if not v else None):
                    votes["nationality"] += [nat] * weight
    return {k: max(set(v), key=v.count) for k, v in votes.items() if v}


# --- a licence's holder, printed under the heading without a label ------------------------------
_HEADINGS = ("LESEN MEMANDU", "DRIVING LICENCE", "DRIVING LICENSE", "LESEN VOKASIONAL", "VOCATIONAL LICENCE")


def name_after_heading(texts: list[str]) -> str | None:
    """The Malaysian licence prints the holder's name right under "LESEN MEMANDU
    MALAYSIA / DRIVING LICENCE", before "Warganegara / Nationality"."""
    votes: list[str] = []
    for n, text in enumerate(texts):
        lines = [ln.strip() for ln in text.upper().splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            if not any(h in line for h in _HEADINGS):
                continue
            for nxt in lines[i + 1:i + 4]:
                if any(h in nxt for h in _HEADINGS) or nxt in ("MALAYSIA",) or re.search(r"\d", nxt):
                    continue
                if _NAME_LABEL.match(nxt) or re.match(r"^(NAMA|NAME)\b", nxt.replace("/", " ")):
                    break  # a labelled name: holder() reads it
                if name := _clean_name(nxt):
                    votes += [name] * (2 if n == 0 else 1)
                break
    return max(set(votes), key=votes.count) if votes else None


def address_after_label(texts: list[str]) -> str | None:
    """Lines after "Alamat / Address", up to the next label or four lines."""
    for text in texts:
        lines = [ln.strip() for ln in text.upper().splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            if re.match(r"^(ALAMAT|ADDRESS)\b", line):
                parts = []
                for nxt in lines[i + 1:i + 5]:
                    if re.match(r"^(NO\.|KELAS|CLASS|TEMPOH|VALIDITY|JENIS|WARGANEGARA)", nxt):
                        break
                    parts.append(nxt)
                if parts and re.search(r"\d{5}", " ".join(parts)):
                    return ", ".join(parts)[:300]
    return None


def compact_dates(text: str) -> list[date]:
    """Dates printed as runs of digits, DDMMYYYY (road tax: issued, start, expiry),
    sometimes run together: "21072023 26072023 25072024" or "2611202225052023"."""
    out = []
    for run in re.findall(r"(?<!\d)(\d{8}(?:\d{8}){0,2})(?!\d)", text):
        for k in range(0, len(run), 8):
            d, mo, y = int(run[k:k + 2]), int(run[k + 2:k + 4]), int(run[k + 4:k + 8])
            if 1990 <= y <= 2100:
                try:
                    out.append(date(y, mo, d))
                except ValueError:
                    pass
    return out


_MY_MONTHS = {"JAN": 1, "FEB": 2, "MAC": 3, "MAR": 3, "APR": 4, "MEI": 5, "MAY": 5, "JUN": 6, "JUL": 7, "OGO": 8,
              "AUG": 8, "SEP": 9, "OKT": 10, "OCT": 10, "NOV": 11, "DIS": 12, "DEC": 12}


def road_tax_dates(texts: list[str]) -> tuple[date | None, date | None]:
    """Start and expiry on a road tax sticker (LKM). Each OCR reading on its own:
    the DDMMYYYY runs (issued, start, expiry) where expiry follows start by at
    most 13 months; else the big printed day and month ("25 DIS") with the year
    printed above it or taken from the issue date. Readings vote."""
    from collections import Counter
    votes: Counter = Counter()
    for n, text in enumerate(texts):
        w = 2 if n == 0 else 1
        # garbled runs make dates like 2052: only those a road tax can carry
        ds = [d for d in compact_dates(text) if 2015 <= d.year <= date.today().year + 2]
        pair = None
        for a, b in zip(ds, ds[1:]):
            if 25 <= (b - a).days <= 400:
                pair = (a, b)
        if pair:
            votes[pair] += w * 2
            continue
        up = text.upper()
        m = re.search(r"\b(\d{1,2})\s*(" + "|".join(_MY_MONTHS) + r")\b", up)
        if not m:
            continue
        day, mo = int(m.group(1)), _MY_MONTHS[m.group(2)]
        years = [int(y) for y in re.findall(r"(?<!\d)(20[2-4]\d)(?!\d)", up)]
        issued = ds[0] if ds else None
        year = years[0] if years else (issued.year if issued else None)
        if year is None:
            continue
        try:
            exp = date(year, mo, day)
        except ValueError:
            continue
        if issued and exp <= issued:
            try:
                exp = date(year + 1, mo, day)
            except ValueError:
                continue
        votes[(issued if issued and issued < exp else None, exp)] += w
    if not votes:
        return None, None
    return votes.most_common(1)[0][0]
