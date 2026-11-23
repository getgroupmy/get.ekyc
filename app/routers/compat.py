"""Drop-in replacement for get-ride's deploy/face-server API.

get-ride's `face-compare` edge function (supabase/functions/_shared/face_server.ts)
calls `POST /compare` with header `x-api-key` and JSON `{image1, image2}` (base64).
Pointing its "Face match server" key at this service ("https://<host>:<ek_ key>")
makes it use this engine with no app change. Photos are compared in memory only.
"""
from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import require_client
from ..config import get_settings
from ..db import ApiClient, audit, get_db
from ..pipeline import face as facelib
from ..pipeline import images

router = APIRouter(tags=["face-server compatibility"])
MODEL = "SFace"


class CompareBody(BaseModel):
    image1: str
    image2: str


def display_score(cosine: float, threshold: float) -> float:
    """Same 0..1 scale as deploy/face-server: the match threshold shows as 0.80,
    a perfect match as 1.00, and anything below zero as 0."""
    if cosine >= threshold:
        return 0.8 + (cosine - threshold) / (1 - threshold) * 0.2
    return max(0.0, cosine / threshold * 0.8)


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _decode(b64: str) -> bytes:
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    return base64.b64decode(b64, validate=False)


@router.post("/compare")
def compare(body: CompareBody, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    s = get_settings()
    limit = s.max_upload_mb * 1024 * 1024
    if max(len(body.image1), len(body.image2)) * 3 // 4 > limit:
        return _error(413, "too_large")
    try:
        img1, img2 = images.decode(_decode(body.image1)), images.decode(_decode(body.image2))
    except (images.ImageError, binascii.Error, ValueError):
        return _error(400, "bad_image")
    faces1, faces2 = facelib.detect(img1), facelib.detect(img2)
    if not faces1 or not faces2:
        audit(db, f"client:{client.id}", "compare.no_face")
        return _error(422, "no_face")
    # Largest face on each image (on a MyKad that skips the small ghost photo).
    cosine = facelib.similarity(facelib.embed(img1, faces1[0]), facelib.embed(img2, faces2[0]))
    verified = cosine >= s.face_match_threshold
    audit(db, f"client:{client.id}", "compare." + ("match" if verified else "no_match"), detail=f"{cosine:.3f}")
    return {"similarity": round(display_score(cosine, s.face_match_threshold), 4), "model": MODEL,
            "verified": verified, "cosine": round(cosine, 4)}


def health_key_check(request: Request, db: Session) -> JSONResponse | None:
    """face-server's /health also validates x-api-key when one is sent."""
    key = request.headers.get("x-api-key")
    if key is None:
        return None
    try:
        require_client(key, db)
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None
