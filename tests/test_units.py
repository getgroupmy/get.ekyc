import numpy as np
import pytest

from app import crypto, webhooks
from app.pipeline import face as facelib
from app.pipeline import liveness


def test_encrypt_roundtrip_and_tamper():
    blob = crypto.encrypt(b"secret image", aad=b"med_1")
    assert b"secret image" not in blob
    assert crypto.decrypt(blob, aad=b"med_1") == b"secret image"
    with pytest.raises(Exception):
        crypto.decrypt(blob, aad=b"med_2")
    with pytest.raises(Exception):
        crypto.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]), aad=b"med_1")


def test_webhook_signature_is_stable():
    sig = webhooks.sign(b"s3cret", "1700000000", b'{"a":1}')
    assert sig.startswith("sha256=") and sig == webhooks.sign(b"s3cret", "1700000000", b'{"a":1}')
    assert sig != webhooks.sign(b"other", "1700000000", b'{"a":1}')


def _face(yaw: float) -> facelib.Face:
    # eyes at x=100 and x=200 (distance 100), nose shifted by yaw*100
    raw = np.array([80, 80, 140, 160, 100, 120, 200, 120, 150 + yaw * 100, 160, 110, 200, 190, 200, 0.95], np.float32)
    return facelib.Face(raw, 400, 400)


@pytest.mark.parametrize("yaws,code", [
    ({"center": 0.02, "left": 0.3, "right": -0.3}, None),
    ({"center": 0.02, "left": -0.3, "right": 0.3}, None),           # mirrored camera
    ({"center": 0.02, "left": 0.02, "right": 0.02}, "liveness_challenge_failed"),  # still photo
    ({"center": 0.02, "left": 0.3, "right": 0.3}, "liveness_challenge_failed"),    # same side twice
    ({"center": 0.3, "left": 0.3, "right": -0.3}, "liveness_challenge_failed"),
])
def test_active_liveness(monkeypatch, yaws, code):
    challenge = ["left", "center", "right"]
    frames = [np.full((4, 4, 3), i, np.uint8) for i in range(3)]
    monkeypatch.setattr(facelib, "detect", lambda img: [_face(yaws[challenge[int(img[0, 0, 0])]])])
    monkeypatch.setattr(facelib, "embed", lambda img, f: np.array([1.0, 0.0], np.float32))
    result = liveness.active(frames, challenge, np.array([1.0, 0.0], np.float32))
    assert result["code"] == code
    assert result["status"] == ("pass" if code is None else "fail")


def test_active_liveness_other_person(monkeypatch):
    challenge = ["center", "left", "right"]
    yaws = [0.0, 0.3, -0.3]
    frames = [np.full((4, 4, 3), i, np.uint8) for i in range(3)]
    monkeypatch.setattr(facelib, "detect", lambda img: [_face(yaws[int(img[0, 0, 0])])])
    monkeypatch.setattr(facelib, "embed", lambda img, f: np.array([0.0, 1.0], np.float32))
    assert liveness.active(frames, challenge, np.array([1.0, 0.0], np.float32))["code"] == "liveness_face_mismatch"
    assert liveness.active(frames[:2], challenge, None)["code"] == "liveness_frames_missing"


def test_challenge_is_a_permutation():
    seen = {tuple(liveness.new_challenge()) for _ in range(200)}
    assert all(sorted(c) == ["center", "left", "right"] for c in seen) and len(seen) > 1
