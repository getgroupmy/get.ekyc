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


MIN_FACE_PX = 24  # smaller "faces" are pattern noise (a MyKad's ghost image is ~60-90 px)


def _run_detector(img: np.ndarray) -> list[Face]:
    h, w = img.shape[:2]
    with _lock:
        _load()
        _det.setInputSize((w, h))
        _, faces = _det.detect(img)
    return [] if faces is None else [Face(f, w, h) for f in faces]


def _scaled(face: Face, f: float, w: int, h: int) -> Face:
    raw = face.raw.copy()
    raw[:14] = raw[:14] / f
    return Face(raw, w, h)


def detect(img: np.ndarray) -> list[Face]:
    """Faces, best first (size weighted by detector confidence).

    When nothing is found, try again on a contrast-corrected copy (dim or
    washed-out photos) and on an enlarged copy (the small portrait on an ID
    card); coordinates always refer to the image given."""
    h, w = img.shape[:2]
    found = _run_detector(img)
    if not found:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        lab[..., 0] = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(lab[..., 0])
        found = _run_detector(cv2.cvtColor(lab, cv2.COLOR_LAB2BGR))
    if not found and max(h, w) < 1600:
        f = min(2.0, 1600 / max(h, w))
        big = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        found = [_scaled(x, f, w, h) for x in _run_detector(big)]
    found = [x for x in found if min(x.box[2], x.box[3]) >= MIN_FACE_PX]
    found.sort(key=lambda x: float(x.raw[2] * x.raw[3]) * (0.5 + x.score), reverse=True)
    return found


def embed(img: np.ndarray, face: Face) -> np.ndarray:
    """SFace embedding of the aligned face, averaged with its mirror image:
    a standard test-time step that makes the match less sensitive to pose and
    lighting from one side."""
    with _lock:
        _load()
        aligned = _rec.alignCrop(img, face.raw)
        a = _rec.feature(aligned).flatten().astype(np.float32)
        b = _rec.feature(cv2.flip(aligned, 1)).flatten().astype(np.float32)
    a, b = a / (np.linalg.norm(a) or 1.0), b / (np.linalg.norm(b) or 1.0)
    v = a + b
    return v / (np.linalg.norm(v) or 1.0)


def quality(img: np.ndarray, face: Face) -> dict:
    """Capture quality of one face (ISO 29794-5 style components, simplified):
    size, sharpness, exposure, head pose. `issues` lists what to fix."""
    x, y, w, h = face.box
    crop = img[max(0, y):y + h, max(0, x):x + w]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.size else np.zeros((1, 1), np.uint8)
    norm = cv2.resize(gray, (112, 112)) if gray.size > 1 else gray
    sharp = float(cv2.Laplacian(norm, cv2.CV_64F).var())
    bright = float(norm.mean())
    clipped = float(((norm < 8) | (norm > 247)).mean())
    ex1, ey1, ex2, ey2 = (float(v) for v in face.raw[4:8])
    roll = float(np.degrees(np.arctan2(ey2 - ey1, ex2 - ex1)))
    eye_mid_y = (ey1 + ey2) / 2
    mouth_y = (float(face.raw[11]) + float(face.raw[13])) / 2
    nose_y = float(face.raw[9])
    pitch = (nose_y - eye_mid_y) / max(1.0, mouth_y - eye_mid_y)  # ~0.5 frontal; low = up, high = down
    issues = []
    if min(w, h) < 80:
        issues.append("face_too_small")
    if sharp < 60:
        issues.append("face_blurry")
    if bright < 60:
        issues.append("face_too_dark")
    elif bright > 205 or clipped > 0.25:
        issues.append("face_overexposed")
    if abs(face.yaw) > 0.25:
        issues.append("head_turned")
    if abs(roll) > 20:
        issues.append("head_tilted")
    if not 0.25 <= pitch <= 0.8:
        issues.append("head_up_or_down")
    if face.score < 0.8:
        issues.append("face_unclear")
    return {"face_px": int(min(w, h)), "sharpness": round(sharp, 1), "brightness": round(bright, 1),
            "yaw": round(face.yaw, 3), "roll_deg": round(roll, 1), "pitch": round(pitch, 2),
            "detector_score": round(face.score, 3), "issues": issues}


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
