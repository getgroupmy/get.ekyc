from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from . import service
from .config import get_settings
from .db import init_db
from .pipeline import face, liveness, ocr
from .routers import admin, verifications

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


@app.get("/health", tags=["system"])
def health():
    ready = face.models_available() and ocr.available()
    return {"status": "ok" if ready else "degraded", "face_models": face.models_available(),
            "ocr": ocr.available(), "passive_liveness": liveness.passive_available()}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/admin")


@app.get("/admin", include_in_schema=False)
def admin_ui():
    return FileResponse(STATIC / "admin.html", headers={"Cache-Control": "no-store"})
