"""What get.ekyc learns from its reviewers.

Every case a person decided (approved or rejected) is a labelled example. This
module turns those decisions into evidence and *suggestions*: which automatic
checks keep sending genuine people to review, where the face-match threshold
should sit, how often the engine agrees with the reviewers.

Nothing changes by itself: thresholds are applied by an admin (and, under BNM's
e-KYC policy, a material change to the matching engine needs re-validation).
"""
from __future__ import annotations

from collections import Counter

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import AuditLog, Verification


def _engine_verdict(db: Session, v: Verification) -> str | None:
    """What the engine decided before a person stepped in (from the audit trail)."""
    row = db.scalar(select(AuditLog.action).where(AuditLog.verification_id == v.id, AuditLog.actor == "system",
                                                  AuditLog.action.in_(("verification.approved", "verification.review",
                                                                       "verification.rejected")))
                    .order_by(AuditLog.id).limit(1))
    return row.split(".")[1] if row else None


def report(db: Session, min_cases: int = 20) -> dict:
    s = get_settings()
    decided = list(db.scalars(select(Verification).where(Verification.decided_by == "admin",
                                                         Verification.status.in_(("approved", "rejected")),
                                                         Verification.is_test.is_(False))))
    genuine = [v for v in decided if v.status == "approved"]
    fraud = [v for v in decided if v.status == "rejected"]
    out: dict = {"reviewed_cases": len(decided), "approved_by_reviewer": len(genuine),
                 "rejected_by_reviewer": len(fraud), "enough_data": len(decided) >= min_cases, "suggestions": []}

    # 1. checks that sent genuine people to review (cost of manual work)
    costly: Counter = Counter()
    for v in genuine:
        for c in (v.result or {}).get("checks", []):
            if c.get("status") in ("review", "fail") and c.get("code"):
                costly[c["code"]] += 1
    out["reasons_on_genuine_people"] = costly.most_common(10)

    # 2. agreement: engine verdict vs reviewer
    agree = Counter()
    for v in decided:
        e = _engine_verdict(db, v)
        if e:
            agree[(e, v.status)] += 1
    out["engine_vs_reviewer"] = {f"engine {e} -> reviewer {r}": n for (e, r), n in agree.items()}

    # 3. face-match threshold from scored, reviewer-labelled cases
    g = np.array([(v.result or {}).get("scores", {}).get("face_match") for v in genuine], dtype=float)
    f = np.array([(v.result or {}).get("scores", {}).get("face_match") for v in fraud
                  if "face_mismatch" in (v.reasons or []) or "face_match_uncertain" in (v.reasons or [])], dtype=float)
    g, f = g[~np.isnan(g)], f[~np.isnan(f)]
    face = {"current_threshold": s.face_match_threshold, "genuine_scored": int(g.size), "impostor_scored": int(f.size)}
    if g.size:
        face["genuine_p01"] = round(float(np.percentile(g, 1)), 3)
        face["genuine_p05"] = round(float(np.percentile(g, 5)), 3)
        face["genuine_below_threshold"] = int((g < s.face_match_threshold).sum())
    if f.size:
        face["impostor_max"] = round(float(f.max()), 3)
    out["face_match"] = face

    if not out["enough_data"]:
        out["suggestions"].append(f"Keep reviewing: suggestions start after {min_cases} decided cases "
                                  f"(BNM audits use at least 400 a month).")
        return out
    if g.size >= min_cases and face["genuine_below_threshold"] / g.size > 0.05:
        safe_floor = max(face.get("impostor_max", 0) + 0.05, 0.30)
        if safe_floor < s.face_match_threshold:
            out["suggestions"].append(
                f"{face['genuine_below_threshold']} of {g.size} genuine people scored below the face threshold "
                f"{s.face_match_threshold}. Lowering it to {safe_floor:.2f} keeps a 0.05 margin over the highest "
                f"impostor seen ({face.get('impostor_max', 'none')}). Measure with scripts/evaluate.py before changing.")
    if f.size and face["impostor_max"] >= s.face_match_threshold:
        out["suggestions"].append(f"An impostor scored {face['impostor_max']} — above the face threshold "
                                  f"{s.face_match_threshold}. Consider raising EKYC_FACE_MATCH_THRESHOLD.")
    for code, n in costly.most_common(3):
        if n / max(1, len(genuine)) >= 0.2:
            out["suggestions"].append(f"'{code}' sent {n} of {len(genuine)} genuine people to review: look at "
                                      f"those cases; the check or its photo guidance may need adjusting.")
    wrong_auto = agree.get(("approved", "rejected"), 0)
    if wrong_auto:
        out["suggestions"].append(f"The engine approved {wrong_auto} case(s) a reviewer then rejected: these are "
                                  f"false accepts — study them first.")
    return out
