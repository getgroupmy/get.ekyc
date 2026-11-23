from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

from .db import Verification
from .reasons import describe

DocumentType = Literal["auto", "passport", "national_id", "driving_licence"]


class Claimed(BaseModel):
    """What the user typed into your app; cross-checked against the document."""
    full_name: str | None = Field(default=None, max_length=200)
    document_number: str | None = Field(default=None, max_length=60)
    date_of_birth: date | None = None
    address: str | None = Field(default=None, max_length=300, description="Checked against the MyKad address")


class CreateVerification(BaseModel):
    document_type: DocumentType = Field(default="auto", description="auto = detect from the image")
    country: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$", description="ISO 3166-1 alpha-3, e.g. MYS")
    external_ref: str | None = Field(default=None, max_length=200, description="Your user id")
    claimed: Claimed | None = None
    require_liveness: bool | None = None
    test: bool = Field(default=False, description="A test run: never used for duplicates, deleted after 48 h")


class CreateClient(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    webhook_url: HttpUrl | None = None


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=1000)


def _ts(d) -> str | None:  # noqa: ANN001
    return d.isoformat() + "Z" if d else None


def verification_out(v: Verification, required: list[str], *, admin: bool = False) -> dict:
    out = {
        "id": v.id,
        "status": v.status,
        "external_ref": v.external_ref,
        "test": bool(v.is_test),
        "document_type": v.document_type,
        "country": v.country,
        "require_liveness": v.require_liveness,
        "liveness_challenge": v.liveness_challenge if v.require_liveness else None,
        "required_media": required,
        "uploaded_media": sorted(m.kind for m in v.media),
        "reasons": v.reasons or [],
        "reason_details": describe(v.reasons),
        "result": v.result,
        "decided_by": v.decided_by,
        "created_at": _ts(v.created_at),
        "submitted_at": _ts(v.submitted_at),
        "completed_at": _ts(v.completed_at),
    }
    if admin:
        out["client"] = v.client.name
        out["claimed"] = v.claimed
        out["review_note"] = v.review_note
        out["media"] = [{"id": m.id, "kind": m.kind} for m in sorted(v.media, key=lambda m: m.kind)]
    return out
