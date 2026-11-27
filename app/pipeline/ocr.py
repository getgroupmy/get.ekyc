"""OCR. General text goes through Tesseract. The MRZ is read by fastmrz first and
falls back to Tesseract with the OCR-B specific 'mrz' model."""
from __future__ import annotations

import os
import re
import shutil
import threading

import cv2
import numpy as np
import pytesseract

from ..config import get_settings


# Background services (nohup over ssh, launchd) don't get the login shell's
# PATH, so Homebrew's tesseract is not found by name: look in the usual places.
_TESSERACT_PLACES = ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract")


def _tesseract() -> str | None:
    found = shutil.which("tesseract") or next((p for p in _TESSERACT_PLACES if os.path.exists(p)), None)
    if found:
        pytesseract.pytesseract.tesseract_cmd = found
    return found


def available() -> bool:
    return _tesseract() is not None


def _prep(img: np.ndarray, min_width: int = 1400) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    if gray.shape[1] < min_width:
        f = min_width / gray.shape[1]
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)


def ink(img: np.ndarray, threshold: float = 0.55, width: int = 1600) -> np.ndarray:
    """Black print only, on white. ID cards print black text over coloured
    security patterns (the blue guilloche on a MyKad); black ink is dark in every
    colour channel while the pattern is bright in at least one, so the brightest
    channel separates them. Dividing by the local background brightness makes it
    work under shadows and uneven phone lighting."""
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    f = width / img.shape[1]
    img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
    img = cv2.medianBlur(img, 3)
    brightest = np.max(img, axis=2).astype(np.float32)
    paper = cv2.GaussianBlur(cv2.dilate(brightest, np.ones((21, 21), np.uint8)), (0, 0), 31) + 1
    return np.where(brightest / paper < threshold, 0, 255).astype(np.uint8)


# (threshold, thicken strokes, page mode): the combinations that read the
# number on a real MyKad front under scan, shadow, dark JPEG and small-photo
# conditions. Several readings let the caller vote out single misreads.
_CARD_PASSES = ((0.55, False, 6), (0.55, True, 6), (0.55, True, 11), (0.65, False, 6))


# --- PP-OCR (PaddleOCR models through RapidOCR) ---------------------------------------
# A second, stronger reader next to Tesseract: reads the raw photo (no cleanup
# needed), handles the security pattern well, and its readings join the votes.
# Engine: onnxruntime (Mac mini, x86 with AVX) or openvino (Celeron NAS without
# AVX, as get-ride's doc-reader), set with EKYC_OCR_ENGINE; "off" disables it.
_rapid = None
_rapid_failed = False
_rapid_lock = threading.Lock()
# MyKad micro-print in the background (KERAJAANMALAYSIA...) read as text boxes.
_MICROPRINT = re.compile(r"KERAJAAN|RAJAAN|AJAANMA|SIAKERA|MALAYSIAKER")


def _rapid_engine():
    global _rapid, _rapid_failed
    if _rapid is None and not _rapid_failed:
        engine = os.environ.get("EKYC_OCR_ENGINE", "onnxruntime").lower()
        if engine == "off":
            _rapid_failed = True
            return None
        try:
            import logging

            from rapidocr import EngineType, RapidOCR
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            e = EngineType.OPENVINO if engine == "openvino" else EngineType.ONNXRUNTIME
            params = {"Det.engine_type": e, "Cls.engine_type": e, "Rec.engine_type": e, "Global.log_level": "warning"}
            # EKYC_OCR_THREADS: cap ONNX Runtime's threads (several workers on one machine);
            # unset = all cores, right for the server, which reads one document at a time.
            if (threads := os.environ.get("EKYC_OCR_THREADS", "")).isdigit() and int(threads) > 0:
                params["EngineConfig.onnxruntime.intra_op_num_threads"] = int(threads)
                params["EngineConfig.onnxruntime.inter_op_num_threads"] = 1
            _rapid = RapidOCR(params=params)
        except Exception:  # noqa: BLE001 - not installed / no model: Tesseract alone
            _rapid_failed = True
    return _rapid


def rapid_available() -> bool:
    return _rapid_engine() is not None


def _spaces(line: str) -> str:
    """PP-OCR sometimes drops a space: after A/L, A/P, BIN(TI) and a postcode."""
    line = re.sub(r"\bA/([LP])(?=[A-Z])", r"A/\1 ", line)
    line = re.sub(r"(?<!\d)(\d{5})(?=[A-Z]{3})", r"\1 ", line)
    return line


def read_rapid(img: np.ndarray, min_score: float = 0.6) -> str:
    """One PP-OCR reading as text lines, top to bottom (boxes on the same row
    joined left to right). '' when the engine is not available."""
    eng = _rapid_engine()
    if eng is None:
        return ""
    with _rapid_lock:
        try:
            r = eng(img)
        except Exception:  # noqa: BLE001
            return ""
    if not r or r.txts is None:
        return ""
    items = []
    for box, txt, score in zip(r.boxes, r.txts, r.scores):
        if score < min_score or _MICROPRINT.search(txt.upper()):
            continue
        ys = [p[1] for p in box]
        items.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(p[0] for p in box), txt))
    items.sort()
    lines: list[list] = []
    for cy, hgt, x, txt in items:
        if lines and abs(cy - lines[-1][0]) < 0.5 * max(hgt, lines[-1][1]):
            lines[-1][2].append((x, txt))
        else:
            lines.append([cy, hgt, [(x, txt)]])
    return "\n".join(_spaces(" ".join(t for _, t in sorted(parts))) for _, _, parts in lines)


# One Tesseract pass on a busy photo (a room, a fruit, a crowded screen) can run
# for minutes on a loaded machine. A pass that takes longer is dropped: the other
# readings still count, and the check goes on.
TESSERACT_TIMEOUT_S = float(os.environ.get("EKYC_TESSERACT_TIMEOUT", "30") or 30)


def _tess(img: np.ndarray, **kw) -> str:  # noqa: ANN003
    try:
        return pytesseract.image_to_string(img, timeout=TESSERACT_TIMEOUT_S, **kw)
    except RuntimeError as exc:  # pytesseract's "Tesseract process timeout"
        if "timeout" not in str(exc).lower():
            raise
        return ""


def read_texts(img: np.ndarray) -> list[str]:
    """Several OCR readings of a card or document page: PP-OCR on the photo,
    then Tesseract once per cleanup pass."""
    out = [t for t in (read_rapid(img),) if t]
    cache: dict[float, np.ndarray] = {}
    for threshold, thicken, psm in _CARD_PASSES:
        m = cache.setdefault(threshold, ink(img, threshold))
        if thicken:
            m = cv2.erode(m, np.ones((3, 3), np.uint8))
        out.append(_tess(m, lang="eng", config=f"--psm {psm}"))
    return out


def read_fine_print(img: np.ndarray) -> list[str]:
    """Two high-resolution readings for tiny print (a card serial): enlarged
    ink at two sizes, read as sparse text."""
    return [_tess(ink(img, 0.55, w), lang="eng", config="--psm 11") for w in (2400, 3200)]


def read_text(img: np.ndarray) -> str:
    """General text on the document (names, numbers), all passes joined."""
    return "\n".join(read_texts(img))


_fast = None
_fast_failed = False


def read_mrz_fast(img: np.ndarray) -> str:
    """MRZ lines via fastmrz (OpenCV line finder + small CTC network). Handles
    rotation and perspective, and is the primary reader. '' when nothing is found
    or the package is not installed."""
    global _fast, _fast_failed
    if _fast_failed:
        return ""
    try:
        if _fast is None:
            from fastmrz import FastMRZ
            _fast = FastMRZ()
        return _fast.get_details(img, input_type="numpy", ignore_parse=True) or ""
    except ImportError:
        _fast_failed = True
        return ""
    except Exception:  # noqa: BLE001 - fall back to Tesseract
        return ""


def read_mrz_text(img: np.ndarray) -> str:
    s = get_settings()
    has_mrz_model = (s.models_dir / "mrz.traineddata").exists()
    cfg = "--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
    if has_mrz_model:
        cfg += f' --tessdata-dir "{s.models_dir}"'
    lang = "mrz" if has_mrz_model else "eng"
    h = img.shape[0]
    texts = []
    # The MRZ sits at the bottom of the page; try tighter crops first.
    for top in (0.55, 0.0):
        g = _prep(img[int(h * top):], min_width=1600)
        texts.append(_tess(g, lang=lang, config=cfg))
    return "\n\n".join(texts)
