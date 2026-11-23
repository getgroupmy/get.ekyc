from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from . import service
from .config import get_settings
from .db import get_db, init_db
from .pipeline import face, liveness, ocr
from .routers import admin, compat, session, verifications

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ekyc")
STATIC = Path(__file__).parent / "static"
_stop = threading.Event()


def _background() -> None:
    for vid in service.resume_interrupted():
        service.process(vid)
    while not _stop.is_set():
        try:
            service.housekeeping()
        except Exception:  # noqa: BLE001
            log.exception("housekeeping failed")
        _stop.wait(600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings()  # fail fast on bad configuration
    init_db()
    if not face.models_available():
        log.error("face models missing in %s - run scripts/download_models.py", get_settings().models_dir)
    _stop.clear()
    threading.Thread(target=_background, daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="GET eKYC", version="1.0.0", lifespan=lifespan,
              description="Self-hosted identity verification: document, selfie match and liveness.")

_origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])

app.include_router(verifications.router)
app.include_router(admin.router)
app.include_router(compat.router)
app.include_router(session.router)


@app.get("/health", tags=["system"])
def health(request: Request, db=Depends(get_db)):  # noqa: ANN001
    denied = compat.health_key_check(request, db)
    if denied:
        return denied
    ready = face.models_available() and ocr.available()
    body = {"status": "ok" if ready else "degraded", "model": compat.MODEL, "face_models": face.models_available(),
            "ocr": ocr.available(), "ocr_ppocr": ocr.rapid_available(), "passive_liveness": liveness.passive_available()}
    if request.headers.get("x-api-key") is not None:
        body["key"] = "ok"  # same as face-server: Admin > API Keys > Test reads this
    return body


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/admin")


@app.get("/guide", include_in_schema=False)
def guide_page():
    return FileResponse(STATIC / "guide.html", headers={"Cache-Control": "no-cache"})


@app.get("/guide/meta", include_in_schema=False)
def guide_meta():
    """Live values the integration guide shows (no secrets)."""
    from .pipeline.catalog import TYPES
    from .reasons import MESSAGES
    s = get_settings()
    return {
        "document_types": [{"id": t.id, "name": t.name, "category": t.category, "needs_selfie": t.needs_selfie}
                           for t in TYPES.values()],
        "reasons": MESSAGES,
        "max_upload_mb": s.max_upload_mb,
        "session_ttl_minutes": s.session_ttl_minutes,
        "test_retention_hours": s.test_retention_hours,
        "liveness_default": s.liveness_default,
        "face_match_threshold": s.face_match_threshold,
    }


@app.get("/admin", include_in_schema=False)
def admin_ui():
    return FileResponse(STATIC / "admin.html", headers={"Cache-Control": "no-store"})
