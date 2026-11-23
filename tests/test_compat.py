import base64

import numpy as np
import pytest

from app.routers.compat import display_score
from tests import synth


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_display_score_matches_face_server_scale():
    assert display_score(0.363, 0.363) == pytest.approx(0.8)
    assert display_score(1.0, 0.363) == pytest.approx(1.0)
    assert round(display_score(0.6, 0.363), 2) == 0.87
    assert round(display_score(0.2, 0.363), 2) == 0.44
    assert display_score(-0.2, 0.363) == 0.0


def test_health_key_check(client, api_key):
    assert client.get("/health").json()["model"] == "SFace"
    ok = client.get("/health", headers=api_key)
    assert ok.status_code == 200 and ok.json()["key"] == "ok"
    bad = client.get("/health", headers={"x-api-key": "ek_wrong"})
    assert bad.status_code == 401 and bad.json() == {"error": "bad_key"}


def test_compare_contract(client, api_key, need_models):
    portrait = synth.jpeg(synth.face_bgr())
    same = client.post("/compare", headers=api_key,
                       json={"image1": b64(portrait), "image2": "data:image/jpeg;base64," + b64(synth.jpeg(synth.selfie()))})
    assert same.status_code == 200
    body = same.json()
    assert body["verified"] is True and body["model"] == "SFace" and body["similarity"] > 0.9 and body["cosine"] > 0.5

    blank = synth.jpeg(np.full((600, 600, 3), 120, np.uint8))
    r = client.post("/compare", headers=api_key, json={"image1": b64(portrait), "image2": b64(blank)})
    assert r.status_code == 422 and r.json() == {"error": "no_face"}
    r = client.post("/compare", headers=api_key, json={"image1": b64(b"nope"), "image2": b64(portrait)})
    assert r.status_code == 400 and r.json() == {"error": "bad_image"}
    assert client.post("/compare", json={"image1": "", "image2": ""}).status_code == 401
