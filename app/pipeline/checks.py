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
