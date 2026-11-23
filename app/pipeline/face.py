"""Face detection (YuNet) and recognition (SFace) through OpenCV - CPU only,
runs the same on Apple Silicon and Intel/AMD Synology boxes."""
from __future__ import annotations

import threading
from dataclasses import dataclass

import cv2
import numpy as np

from ..config import get_settings

_lock = threading.Lock()
_det = None
_rec = None


@dataclass
class Face:
    raw: np.ndarray  # YuNet row: x,y,w,h, 5 landmarks (x,y), score
    img_w: int
    img_h: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        x, y, w, h = self.raw[:4]
        return int(x), int(y), int(w), int(h)

    @property
    def score(self) -> float:
        return float(self.raw[14])

    @property
    def area_ratio(self) -> float:
        return float(self.raw[2] * self.raw[3]) / float(self.img_w * self.img_h)

    @property
    def yaw(self) -> float:
        """Signed head-turn estimate: nose offset from the eye midpoint, in units
        of eye distance. ~0 looking at the camera, |>0.2| for a clear turn."""
        ex1, ey1, ex2, ey2, nx = self.raw[4], self.raw[5], self.raw[6], self.raw[7], self.raw[8]
        eye_dist = float(np.hypot(ex2 - ex1, ey2 - ey1)) or 1.0
        return float((nx - (ex1 + ex2) / 2) / eye_dist)


def models_available() -> bool:
    d = get_settings().models_dir
    return (d / "yunet.onnx").exists() and (d / "sface.onnx").exists()


def _load() -> None:
    global _det, _rec
    if _det is None:
        d = get_settings().models_dir
        _det = cv2.FaceDetectorYN.create(str(d / "yunet.onnx"), "", (320, 320), 0.7, 0.3, 500)
        _rec = cv2.FaceRecognizerSF.create(str(d / "sface.onnx"), "")


def detect(img: np.ndarray) -> list[Face]:
    """Faces sorted largest first."""
    h, w = img.shape[:2]
    with _lock:
        _load()
        _det.setInputSize((w, h))
        _, faces = _det.detect(img)
    if faces is None:
        return []
    out = [Face(f, w, h) for f in faces]
    out.sort(key=lambda f: f.raw[2] * f.raw[3], reverse=True)
    return out


def embed(img: np.ndarray, face: Face) -> np.ndarray:
    with _lock:
        _load()
        feat = _rec.feature(_rec.alignCrop(img, face.raw))
    v = feat.flatten().astype(np.float32)
    return v / (np.linalg.norm(v) or 1.0)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def crop_square(img: np.ndarray, face: Face, expand: float = 2.2) -> np.ndarray:
    """Square crop centred on the face with surrounding context."""
    x, y, w, h = face.box
    side = int(max(w, h) * expand)
    cx, cy = x + w // 2, y + h // 2
    x0, y0 = max(0, cx - side // 2), max(0, cy - side // 2)
    x1, y1 = min(img.shape[1], x0 + side), min(img.shape[0], y0 + side)
    return img[y0:y1, x0:x1]
