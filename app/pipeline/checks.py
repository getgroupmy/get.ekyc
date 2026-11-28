from __future__ import annotations

PASS, FAIL, REVIEW, SKIPPED = "pass", "fail", "review", "skipped"


def check(name: str, status: str, code: str | None = None, **detail) -> dict:
    return {"name": name, "status": status, "code": None if status == PASS else code, "detail": detail}


def overall(checks: list[dict], auto_approve: bool) -> tuple[str, list[str]]:
    """any fail -> rejected; else any review -> review; else approved."""
    statuses = {c["status"] for c in checks}
    reasons = [c["code"] for c in checks if c["status"] in (FAIL, REVIEW) and c["code"]]
    if FAIL in statuses:
        return "rejected", [c["code"] for c in checks if c["status"] == FAIL and c["code"]]
    if REVIEW in statuses:
        return "review", reasons
    return ("approved" if auto_approve else "review"), ([] if auto_approve else ["manual_approval_required"])


# Documents whose details can be confirmed by the machine alone: the MyKad
# number (date of birth, sex and state built in, front and back agreeing) and
# the check digits of a passport or of an identity card / residence permit that
# carries the same machine-readable lines.
CONFIRMABLE = {"mykad": ("national_id",), "mrz": ("passport", "national_id", "residence_permit")}


def auto_approval(checks: list[dict], extracted: dict) -> dict:
    """What an approval without a person needs: the document's details
    confirmed, the selfie matching its photo, and the head-turn liveness passed.
    Anything else goes to a person, however clean it looks."""
    by = {c["name"]: c for c in checks}
    source, kind = extracted.get("source"), extracted.get("document_type")
    missing = []
    if not (by.get("document_data", {}).get("status") == PASS and kind in CONFIRMABLE.get(source, ())):
        missing.append("document_confirmed")
    if by.get("face_match", {}).get("status") != PASS:
        missing.append("face_match")
    if by.get("liveness", {}).get("status") != PASS:
        missing.append("liveness")
    if missing:
        return check("auto_approval", REVIEW, "manual_approval_required", missing=missing)
    return check("auto_approval", PASS, confirmed_by=source)
