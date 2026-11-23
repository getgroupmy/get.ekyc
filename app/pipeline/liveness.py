"""Liveness / presentation-attack checks.

Active: the app shows the user a random sequence (look straight, turn one way,
turn the other) and uploads one frame per step. We verify the head actually
moved as instructed and that every frame is the same person as the selfie. This
defeats a printed photo or a still image on a screen.

Passive (optional): a single-image anti-spoof CNN, used when its ONNX file is
present. See README for how to add it.
"""
from __future__ import annotations

import secrets
import threading

import cv2
import numpy as np

from ..config import get_settings
from . import face as facelib
from .checks import FAIL, PASS, REVIEW, check

STEPS = ("center", "left", "right")


def new_challenge() -> list[str]:
    steps = list(STEPS)
    for i in range(len(steps) - 1, 0, -1):  # Fisher-Yates with a CSPRNG
        j = secrets.randbelow(i + 1)
        steps[i], steps[j] = steps[j], steps[i]
    return steps


def active(frames: list[np.ndarray], challenge: list[str], selfie_embedding: np.ndarray | None) -> dict:
    s = get_settings()
    if len(frames) != len(challenge):
        return check("liveness", FAIL, "liveness_frames_missing", expected=len(challenge), received=len(frames))
    yaws: dict[str, float] = {}
    sims: list[float] = []
    for step, img in zip(challenge, frames):
        faces = facelib.detect(img)
        if not faces:
            return check("liveness", FAIL, "liveness_no_face", step=step)
        yaws[step] = faces[0].yaw
        if selfie_embedding is not None:
            sims.append(facelib.similarity(facelib.embed(img, faces[0]), selfie_embedding))
    detail = {"yaw": {k: round(v, 3) for k, v in yaws.items()}, "min_similarity": round(min(sims), 3) if sims else None}

    # Front cameras may or may not mirror the image, so we do not trust the sign
    # itself - only that "left" and "right" are clear turns in opposite directions.
    left, right, center = yaws["left"], yaws["right"], yaws["center"]
    turned = abs(left) >= s.liveness_yaw_turn and abs(right) >= s.liveness_yaw_turn and left * right < 0
    if not turned or abs(center) > s.liveness_yaw_center:
        return check("liveness", FAIL, "liveness_challenge_failed", **detail)
    # Turned faces match less strongly than frontal ones, hence the lower bar.
    if sims and min(sims) < s.face_review_threshold:
        return check("liveness", FAIL, "liveness_face_mismatch", **detail)
    return check("liveness", PASS, **detail)


_net = None
_net_lock = threading.Lock()


def passive_available() -> bool:
    return get_settings().antispoof_model.exists()


def _spoof_margin(img: np.ndarray, face: facelib.Face) -> float:
    """MiniFASNetV2-SE (128x128, RGB, square crop 1.5x the face, edges
    reflected): real logit minus spoof logit. Higher = more likely a live face."""
    global _net
    x, y, w, h = face.box
    side = int(max(w, h) * 1.5)
    cx, cy = x + w / 2, y + h / 2
    x0, y0 = int(cx - side / 2), int(cy - side / 2)
    padded = cv2.copyMakeBorder(img, side, side, side, side, cv2.BORDER_REFLECT_101)
    crop = padded[y0 + side:y0 + 2 * side, x0 + side:x0 + 2 * side]
    rgb = cv2.cvtColor(cv2.resize(crop, (128, 128), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    blob = rgb.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    with _net_lock:
        if _net is None:
            _net = cv2.dnn.readNetFromONNX(str(get_settings().antispoof_model))
        _net.setInput(blob)
        logits = _net.forward()[0]
    return float(logits[0] - logits[1])


def passive(frames: list[tuple[np.ndarray, facelib.Face]]) -> dict | None:
    """Single-image anti-spoofing on the selfie (and the straight-on liveness
    frame): catches photos of screens and prints. The lowest margin counts. A
    second opinion only: a low score sends the case to a person (or rejects,
    with EKYC_PASSIVE_LIVENESS_ACTION=reject)."""
    s = get_settings()
    if not passive_available() or not frames:
        return None
    usable = [(img, f) for img, f in frames if min(f.box[2], f.box[3]) >= 64]  # the model needs >= 64 px faces
    if not usable:
        return check("passive_liveness", "skipped", "face_too_small_for_liveness")
    margins = [_spoof_margin(img, f) for img, f in usable]
    worst = min(margins)
    detail = {"margin": round(worst, 2), "threshold": s.passive_liveness_threshold, "frames": len(margins)}
    if worst >= s.passive_liveness_threshold:
        return check("passive_liveness", PASS, **detail)
    status = FAIL if s.passive_liveness_action == "reject" else REVIEW
    return check("passive_liveness", status, "possible_spoof", **detail)
