"""Every document type get.ekyc knows, how it is recognised, and what is read
from the driver and vehicle documents.

Recognition order (most specific first): an MRZ names passports, visas and
MRZ cards; then the wording of vehicle and driver documents, which come before
identity cards because a VOC or a licence also carries an IC number; then the
identity-card family.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from . import fields


@dataclass(frozen=True)
class DocType:
    id: str
    name: str
    category: str  # identity | driver | vehicle
    needs_selfie: bool
    has_portrait: bool
    words: tuple[str, ...] = ()  # any one of these names the document


TYPES: dict[str, DocType] = {t.id: t for t in (
    # identity
    DocType("passport", "Passport", "identity", True, True),
    DocType("visa", "Visa", "identity", False, True),
    DocType("national_id", "National identity card", "identity", True, True),
    DocType("residence_permit", "Residence permit", "identity", True, True),
    DocType("refugee_card", "UNHCR refugee card", "identity", True, True, ("UNHCR", "UNITED NATIONS HIGH COMMISSIONER")),
    DocType("foreign_worker_card", "i-Kad (foreign worker / expatriate)", "identity", True, True,
            ("I-KAD", "IKAD", "KAD PEKERJA ASING", "PLKS", "FOREIGN WORKER")),
    # driver
    DocType("driving_licence", "Driving licence", "driver", True, True,
            ("LESEN MEMANDU", "DRIVING LICENCE", "DRIVING LICENSE", "DRIVER LICENSE", "DRIVER'S LICENSE",
             "SURAT IZIN MENGEMUDI",
             # PSV / GDL: the vocational licence taxi and e-hailing drivers carry
             "LESEN VOKASIONAL", "VOCATIONAL LICENCE", "VOCATIONAL LICENSE")),
    DocType("international_driving_permit", "International driving permit", "driver", True, True,
            ("INTERNATIONAL DRIVING PERMIT", "CONVENTION ON ROAD TRAFFIC", "PERMIS INTERNATIONAL")),
    DocType("medical_report", "Medical report for PSV / GDL (JPJL8A)", "driver", False, False,
            ("JPJL8A", "JPJ L8A", "PEMERIKSAAN KESIHATAN", "LAPORAN PEMERIKSAAN PERUBATAN")),
    # vehicle
    DocType("vehicle_inspection", "PUSPAKOM inspection report", "vehicle", False, False,
            ("PUSPAKOM", "VR1", "LAPORAN PEMERIKSAAN KENDERAAN")),
    DocType("vehicle_registration", "Vehicle ownership certificate (VOC / geran)", "vehicle", False, False,
            ("SIJIL PEMILIKAN KENDERAAN", "VEHICLE OWNERSHIP CERTIFICATE", "KAD PENDAFTARAN KENDERAAN",
             "PEMUNYA BERDAFTAR")),
    DocType("road_tax", "Road tax (LKM)", "vehicle", False, False,
            ("LESEN KENDERAAN MOTO", "E-LKM", "MOTOR VEHICLE LICENCE", "MOTOR VEHICLE LICENSE",
             # the sticker's own line, when the big title is cut off or run together
             "LKM INI DI CERMIN", "DI CERMIN KENDERAAN")),
    DocType("vehicle_insurance", "Motor insurance certificate / cover note", "vehicle", False, False,
            ("CERTIFICATE OF INSURANCE", "SIJIL INSURANS", "COVER NOTE", "NOTA PERLINDUNGAN",
             "ROAD TRANSPORT ACT 1987", "THIRD PARTY RISKS", "SIJIL TAKAFUL", "CERTIFICATE OF TAKAFUL",
             # policy schedules and e-cover notes as insurers send them
             "POLICY SCHEDULE", "JADUAL POLISI", "POLISI MOTOR", "TYPE OF COVER", "PURCHASE OF TAKAFUL",
             "PRIVATE CAR POLICY", "SUM INSURED", "JUMLAH DIINSURANSKAN")),
    DocType("ehailing_vehicle_permit", "E-hailing vehicle permit (EVP)", "vehicle", False, False,
            ("E-HAILING VEHICLE PERMIT", "PERMIT KENDERAAN E-HAILING", "AGENSI PENGANGKUTAN AWAM DARAT",
             "LEMBAGA PELESENAN KENDERAAN PERDAGANGAN")),
    DocType("taxi_permit", "Taxi permit", "vehicle", False, False, ("PERMIT TEKSI", "TAXI PERMIT")),
)}

# Wording checked in this order: the most specific documents first.
# Insurance before licences: policies mention "a valid driving licence" in their small print.
_ORDER = ("vehicle_inspection", "vehicle_registration", "road_tax", "medical_report", "international_driving_permit",
          "vehicle_insurance", "driving_licence", "ehailing_vehicle_permit", "taxi_permit", "refugee_card",
          "foreign_worker_card")

IDENTITY_TYPES = tuple(t for t, d in TYPES.items() if d.category == "identity")


def _flat(texts: list[str]) -> str:
    return " ".join(" ".join(t.upper().split()) for t in texts)


def by_wording(texts: list[str]) -> str | None:
    flat = _flat(texts)
    tight = flat.replace(" ", "")  # OCR often runs big titles together: LESENKENDERAANMOTOR
    for tid in _ORDER:
        # Run-together matching only for long multi-word titles: short words like
        # IKAD would otherwise turn up across word boundaries ("...MALAYSIA KAD...").
        if any(w in flat or (" " in w and len(w) >= 12 and w.replace(" ", "") in tight) for w in TYPES[tid].words):
            return tid
    # Road tax in its holder: the title hidden, but the region and class line and
    # the issued/start/expiry digits always show.
    if re.search(r"\b(SEMENANJUNG|SABAH|SARAWAK|LABUAN)\b", flat) and \
            re.search(r"\b(PERSENDIRIAN|PERDAGANGAN|KERAJAAN|PERSENDIRAN)\b", flat) and \
            len(fields.compact_dates(flat)) >= 2:
        return "road_tax"
    return None


def by_mrz(code: str) -> str:
    """ICAO document code: P passport, V visa, I/A/C cards (IR, AR, CR = residence permits)."""
    if code.startswith("P"):
        return "passport"
    if code.startswith("V"):
        return "visa"
    if code[:2] in ("IR", "AR", "CR", "RP"):
        return "residence_permit"
    return "national_id"


# --- reading driver and vehicle documents ----------------------------------------------
def read(doc_type: str, texts: list[str], today: date | None = None) -> dict:
    """Fields of a driver or vehicle document. Identity documents are read elsewhere."""
    text = "\n".join(texts)
    today = today or date.today()
    out: dict = {}
    if doc_type in ("vehicle_registration", "road_tax", "vehicle_insurance", "vehicle_inspection",
                    "ehailing_vehicle_permit", "taxi_permit"):
        if p := fields.plate(text):
            out["plate"] = p
    if doc_type in ("vehicle_registration", "vehicle_inspection", "vehicle_insurance"):
        if c := fields.chassis(text):
            out["chassis_number"] = c
        if e := fields.engine_number(text):
            out["engine_number"] = e
    if doc_type == "vehicle_registration":
        if y := fields.year_made(text):
            out["year_made"] = y
            out["vehicle_age_years"] = today.year - y
        if cc := fields.engine_cc(text):
            out["engine_cc"] = cc
        owner_ic = fields.nric_in(text)
        if owner_ic:
            out["owner_id_number"] = owner_ic
        if n := fields.reference_number(text, ("NO. SIJIL", "NO SIJIL", "NO. SIRI", "CERTIFICATE NO", "VOC NO")):
            out["document_number"] = n
        if dates_ := fields.dates(text):
            reg = [d for d in dates_ if d <= today]
            if reg:
                out["registration_date"] = max(reg).isoformat()
        elif co := fields.company_number(text):
            out["owner_company_number"] = co
        up = text.upper()
        out["financed"] = bool(re.search(r"HAK\s?MILIK\s?TUNTUT|TUNTUTAN\s?PEMUNYA|OWNERSHIP CLAIM", up)) and \
            bool(re.search(r"BANK|BERHAD|BHD|FINANCE|KREDIT", up))
        m = re.search(r"\b(AB|AD|AH|AR|TB)\b", up)
        if m and re.search(r"KEGUNAAN|USAGE", up):
            out["usage_code"] = m.group(1)
    if doc_type == "road_tax":
        out.update(fields.validity(text, today))
        # The sticker's own line: issued, start, expiry as DDMMYYYY runs
        ds = fields.compact_dates(text)
        if len(ds) >= 2 and ds[-1] > ds[-2]:
            out.setdefault("start_date", ds[-2].isoformat())
            out["expiry_date"] = ds[-1].isoformat()
        m = re.search(r"\b(AB|AD|AH)\b", text.upper())
        if m:
            out["usage_code"] = m.group(1)
    if doc_type == "vehicle_insurance":
        out.update(fields.validity(text, today))
        if ins := fields.insurer(text):
            out["insurer"] = ins
        if n := fields.reference_number(text, ("COVER NOTE NO", "NO. NOTA", "POLICY NO", "NO. POLISI",
                                               "CERTIFICATE NO", "NO. SIJIL", "POLICY/CERTIFICATE NO")):
            out["document_number"] = n
        out["cover_note"] = bool(re.search(r"COVER NOTE|NOTA PERLINDUNGAN", text.upper()))
        out["ehailing_cover"] = bool(re.search(r"E[\s-]?HAIL", text.upper()))
    if doc_type == "vehicle_inspection":
        up = text.upper()
        out["result"] = "pass" if "LULUS" in up or re.search(r"\bPASS(ED)?\b", up) else \
            "fail" if "GAGAL" in up or re.search(r"\bFAIL(ED)?\b", up) else None
        out["ehailing_inspection"] = bool(re.search(r"E[\s-]?HAILING", up))
        ds = fields.dates(text)
        if ds:
            out["inspection_date"] = max(d for d in ds if d <= today).isoformat() if any(d <= today for d in ds) else None
        if n := fields.reference_number(text, ("NO. LAPORAN", "REPORT NO", "NO LAPORAN", "NO. RUJUKAN")):
            out["document_number"] = n
    if doc_type in ("ehailing_vehicle_permit", "taxi_permit"):
        out.update(fields.validity(text, today))
        if n := fields.reference_number(text, ("NO. PERMIT", "PERMIT NO", "NO PERMIT", "PERMIT NUMBER")):
            out["document_number"] = n
    if doc_type == "driving_licence":
        if re.search(r"VOKASIONAL|VOCATIONAL", text.upper()):
            out["document_subtype"] = "Lesen Vokasional (PSV/GDL)"
        out.update(fields.validity(text, today))
        cls = fields.licence_classes(text)
        if cls:
            out["licence_classes"] = cls
        out.update(fields.licence_flags(text))
        if ic := fields.nric_in(text):
            out["id_number"] = ic  # JPJ keys a Malaysian licence by the holder's IC
        if n := fields.reference_number(text, ("NO. LESEN", "LICENCE NO", "LICENSE NO", "NO LESEN")):
            out["document_number"] = n
        if name := fields.name_after_heading(texts):
            out["full_name"] = name
        if addr := fields.address_after_label(texts):
            out["address"] = addr
    if doc_type in ("international_driving_permit", "medical_report"):
        out.update(fields.validity(text, today))
        if ic := fields.nric_in(text):
            out["id_number"] = ic
        if n := fields.reference_number(text, ("PERMIT NO", "NO. PERMIT", "SERIAL NO", "NO. SIRI", "FORM NO")):
            out["document_number"] = n
    return {k: v for k, v in out.items() if v is not None}


# What each type must yield to count as read.
REQUIRED = {
    "vehicle_registration": ("plate",),
    "road_tax": ("plate", "expiry_date"),
    "vehicle_insurance": ("plate", "expiry_date"),
    "vehicle_inspection": ("plate", "result"),
    "ehailing_vehicle_permit": ("expiry_date",),
    "taxi_permit": ("expiry_date",),
    "driving_licence": ("expiry_date",),
    "international_driving_permit": (),
    "medical_report": (),
}
