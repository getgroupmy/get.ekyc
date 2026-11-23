from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_SIDE = 2200
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}


class ImageError(ValueError):
    pass


def decode(data: bytes) -> np.ndarray:
    """Bytes -> BGR image, honouring EXIF rotation and capping the size."""
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in ALLOWED_FORMATS:
            raise ImageError(f"unsupported image format {im.format}")
        im = ImageOps.exif_transpose(im).convert("RGB")
    except ImageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImageError("not a readable image") from exc
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE))
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


def quality(img: np.ndarray) -> dict:
    """Cheap capture-quality metrics used to give the user a 'retake' reason."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    scale = 800 / max(h, w)
    small = cv2.resize(gray, None, fx=scale, fy=scale) if scale < 1 else gray
    return {
        "width": w,
        "height": h,
        "sharpness": round(float(cv2.Laplacian(small, cv2.CV_64F).var()), 1),
        "brightness": round(float(small.mean()), 1),
        "glare": round(float((small > 250).mean()), 4),
    }


def rotate(img: np.ndarray, quarter_turns: int) -> np.ndarray:
    k = quarter_turns % 4
    if k == 0:
        return img
    return cv2.rotate(img, {1: cv2.ROTATE_90_CLOCKWISE, 2: cv2.ROTATE_180, 3: cv2.ROTATE_90_COUNTERCLOCKWISE}[k])
