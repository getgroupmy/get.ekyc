"""OCR. General text goes through Tesseract. The MRZ is read by fastmrz first and
falls back to Tesseract with the OCR-B specific 'mrz' model."""
from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytesseract

from ..config import get_settings


def available() -> bool:
    return shutil.which("tesseract") is not None


def _prep(img: np.ndarray, min_width: int = 1400) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    if gray.shape[1] < min_width:
        f = min_width / gray.shape[1]
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)


def read_text(img: np.ndarray) -> str:
    """General text on the document (names, numbers)."""
    g = _prep(img)
    out = [pytesseract.image_to_string(g, lang="eng", config=f"--psm {psm}") for psm in (6, 11)]
    return "\n".join(out)


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
        texts.append(pytesseract.image_to_string(g, lang=lang, config=cfg))
    return "\n\n".join(texts)
