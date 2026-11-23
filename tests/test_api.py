import numpy as np

from tests import synth
from tests.conftest import ADMIN

BASE = "/v1/verifications"


def _run(client, key, body, files):
    r = client.post(BASE, json={"require_liveness": False, "external_ref": "same-user", **body}, headers=key)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    for kind, data in files.items():
        r = client.put(f"{BASE}/{vid}/media/{kind}", files={"file": ("x.jpg", data, "image/jpeg")}, headers=key)
        assert r.status_code == 200, r.text
    r = client.post(f"{BASE}/{vid}/submit", headers=key)
    assert r.status_code == 202, r.text
    return client.get(f"{BASE}/{vid}", headers=key).json()  # background task already ran


def _checks(v):
    return {c["name"]: c for c in v["result"]["checks"]}


def test_auth_required(client, api_key):
    assert client.get(BASE).status_code == 401
    assert client.get(BASE, headers={"X-API-Key": "ek_wrong"}).status_code == 401
    assert client.get("/admin/api/clients").status_code == 401
    assert client.get("/admin/api/clients", headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_upload_validation(client, api_key):
    vid = client.post(BASE, json={"document_type": "passport"}, headers=api_key).json()
    assert vid["liveness_challenge"] and len(vid["required_media"]) == 5
    url = f"{BASE}/{vid['id']}"
    bad = client.put(f"{url}/media/selfie", files={"file": ("x.jpg", b"not an image", "image/jpeg")}, headers=api_key)
    assert bad.status_code == 422
    assert client.put(f"{url}/media/nope", files={"file": ("x.jpg", b"x", "image/jpeg")}, headers=api_key).status_code == 422
    r = client.post(f"{url}/submit", headers=api_key)
    assert r.status_code == 422 and "document_front" in r.json()["detail"]["missing"]


def test_other_client_cannot_read(client, api_key):
    other = client.post("/admin/api/clients", json={"name": "other"}, headers=ADMIN).json()
    vid = client.post(BASE, json={"document_type": "passport"}, headers=api_key).json()["id"]
    assert client.get(f"{BASE}/{vid}", headers={"X-API-Key": other["api_key"]}).status_code == 404


def test_passport_approved_then_duplicate_then_erased(client, api_key, need_models):
    doc = synth.passport_image(synth.td3_lines(), synth.face_bgr())
    files = {"document_front": doc, "selfie": synth.jpeg(synth.selfie())}
    body = {"document_type": "passport", "country": "UTO", "external_ref": "rider-1",
            "claimed": {"full_name": "Anna Maria Eriksson", "document_number": "L898902C3", "date_of_birth": "1974-08-12"}}
    v = _run(client, api_key, body, files)
    try:
        _approved_then_duplicate(client, api_key, body, files, v)
    finally:
        # Erase even when an assertion fails, so this approved face can't turn
        # later tests into duplicate_face reviews.
        client.delete(f"{BASE}/{v['id']}", headers=api_key)


def _approved_then_duplicate(client, api_key, body, files, v):
    assert v["status"] == "approved", v
    assert v["result"]["extracted"]["document_number"] == "L898902C3"
    assert v["result"]["extracted"]["full_name"] == "ANNA MARIA ERIKSSON"
    assert v["result"]["scores"]["face_match"] > 0.5
    assert all(c["status"] in ("pass", "skipped") for c in v["result"]["checks"])

    # same passport under another account -> flagged for manual review
    dup = _run(client, api_key, {**body, "external_ref": "rider-2"}, files)
    try:
        _review_duplicate(client, api_key, v, dup)
    finally:
        client.delete(f"/admin/api/verifications/{dup['id']}", headers=ADMIN)


def _review_duplicate(client, api_key, v, dup):
    assert dup["status"] == "review" and "duplicate_document" in dup["reasons"]
    assert dup["reason_details"][0]["message"]

    # reviewer sees it, views the image, rejects it
    detail = client.get(f"/admin/api/verifications/{dup['id']}", headers=ADMIN).json()
    assert detail["claimed"]["full_name"] == "Anna Maria Eriksson"
    media = client.get(f"/admin/api/media/{detail['media'][0]['id']}", headers=ADMIN)
    assert media.status_code == 200 and media.content[:2] == b"\xff\xd8"
    r = client.post(f"/admin/api/verifications/{dup['id']}/decision", json={"decision": "rejected", "note": "dup"}, headers=ADMIN)
    assert r.json()["status"] == "rejected" and r.json()["decided_by"] == "admin"

    # erasure removes PII and images
    assert client.delete(f"{BASE}/{v['id']}", headers=api_key).status_code == 204
    gone = client.get(f"{BASE}/{v['id']}", headers=api_key).json()
    assert gone["status"] == "deleted" and gone["result"] is None and gone["uploaded_media"] == []


def test_images_are_encrypted_on_disk(client, api_key, need_models):
    from app.config import get_settings
    doc = synth.passport_image(synth.td3_lines(number="X11111111"), synth.face_bgr())
    _run(client, api_key, {"document_type": "passport"}, {"document_front": doc, "selfie": synth.jpeg(synth.selfie())})
    blobs = list(get_settings().media_dir.rglob("*.bin"))
    assert blobs and all(b.read_bytes()[:2] != b"\xff\xd8" for b in blobs)
    assert b"X11111111" not in (get_settings().data_dir / "ekyc.db").read_bytes()


def test_expired_passport_rejected(client, api_key, need_models):
    doc = synth.passport_image(synth.td3_lines(number="E22222222", expiry="200101"), synth.face_bgr())
    v = _run(client, api_key, {"document_type": "passport"}, {"document_front": doc, "selfie": synth.jpeg(synth.selfie())})
    assert v["status"] == "rejected" and v["reasons"] == ["document_expired"]


def test_underage_and_claimed_mismatch(client, api_key, need_models):
    doc = synth.passport_image(synth.td3_lines(number="U33333333", dob="150101"), synth.face_bgr())
    v = _run(client, api_key, {"document_type": "passport", "claimed": {"document_number": "ZZ9999999"}},
             {"document_front": doc, "selfie": synth.jpeg(synth.selfie())})
    assert v["status"] == "rejected" and set(v["reasons"]) == {"underage", "document_number_mismatch"}


def test_passport_without_mrz_or_face(client, api_key, need_models):
    blank = synth.card_image(None, ["JUST SOME TEXT", "NO MACHINE READABLE ZONE"])
    no_face = synth.jpeg(np.full((800, 800, 3), 128, np.uint8))
    v = _run(client, api_key, {"document_type": "passport"}, {"document_front": blank, "selfie": no_face})
    assert v["status"] == "rejected"
    assert {"document_face_not_found", "mrz_not_found", "selfie_no_face"} <= set(v["reasons"])


def test_mykad(client, api_key, need_models):
    card = synth.card_image(synth.face_bgr(), ["KAD PENGENALAN MALAYSIA", "900514-10-5232", "ANNA BINTI ERIK", "SELANGOR"])
    v = _run(client, api_key, {"document_type": "national_id", "country": "MYS",
                               "claimed": {"full_name": "Anna binti Erik", "document_number": "900514105232"}},
             {"document_front": card, "selfie": synth.jpeg(synth.selfie())})
    assert v["status"] == "approved", v
    ex = v["result"]["extracted"]
    assert ex["date_of_birth"] == "1990-05-14" and ex["sex"] == "F" and ex["place_of_birth"] == "Selangor"


def test_typed_mykad_number_must_be_valid(client, api_key):
    r = client.post(BASE, json={"document_type": "national_id", "country": "MYS",
                                "claimed": {"document_number": "8308256367"}}, headers=api_key)
    assert r.status_code == 422 and "12 digits" in r.text
    ok = client.post(BASE, json={"document_type": "national_id", "country": "MYS",
                                 "claimed": {"document_number": "830825086367"}}, headers=api_key)
    assert ok.status_code == 201


def test_driving_licence_without_claims_goes_to_review(client, api_key, need_models):
    card = synth.card_image(synth.face_bgr(), ["DRIVING LICENCE", "CLASS D", "VALID"])
    v = _run(client, api_key, {"document_type": "driving_licence"},
             {"document_front": card, "selfie": synth.jpeg(synth.selfie())})
    assert v["status"] == "review" and "no_machine_readable_data" in v["reasons"]
    assert _checks(v)["face_match"]["status"] == "pass"


def test_console_try_runs_as_a_client(client, api_key, need_models):
    cid = client.get("/admin/api/clients", headers=ADMIN).json()["items"][0]["id"]
    files = {"document_front": ("f.jpg", synth.passport_image(synth.td3_lines(number="T44444444"), synth.face_bgr()), "image/jpeg"),
             "selfie": ("s.jpg", synth.jpeg(synth.selfie()), "image/jpeg")}
    r = client.post("/admin/api/try", data={"client_id": cid, "document_type": "auto"}, files=files, headers=ADMIN)
    assert r.status_code == 202, r.text
    v = client.get(f"/admin/api/verifications/{r.json()['id']}", headers=ADMIN).json()
    ex = v["result"]["extracted"]
    assert ex["document_type"] == "passport" and ex["document_type_source"] == "mrz" and ex["country"] == "UTO"
    assert client.post("/admin/api/try", data={"client_id": cid}, files=files).status_code == 401
    assert client.post("/admin/api/try", data={"client_id": "cli_nope"}, files=files, headers=ADMIN).status_code == 404


def test_tests_are_marked_kept_apart_and_deleted(client, api_key, need_models):
    from datetime import timedelta
    from app import service
    from app.db import Verification, session_scope, utcnow
    doc = synth.passport_image(synth.td3_lines(number="Q55555555"), synth.face_bgr())
    files = {"document_front": doc, "selfie": synth.jpeg(synth.selfie())}
    real = _run(client, api_key, {"document_type": "passport", "external_ref": "real-user"}, files)
    test = _run(client, api_key, {"document_type": "passport", "external_ref": "staff", "test": True}, files)
    assert test["test"] is True and real["test"] is False
    # the same document under a test is not a duplicate of the real one
    assert "duplicate_document" not in test["reasons"]
    stats = client.get("/admin/api/stats", headers=ADMIN).json()
    assert stats["tests"] >= 1
    tests = client.get("/admin/api/verifications?kind=test", headers=ADMIN).json()["items"]
    assert test["id"] in {v["id"] for v in tests} and real["id"] not in {v["id"] for v in tests}
    # older than the retention: housekeeping deletes it entirely, the real one stays
    with session_scope() as db:
        db.get(Verification, test["id"]).created_at = utcnow() - timedelta(hours=49)
    service.housekeeping()
    assert client.get(f"/v1/verifications/{test['id']}", headers=api_key).status_code == 404
    assert client.get(f"/v1/verifications/{real['id']}", headers=api_key).status_code == 200
    # delete-all removes the remaining tests only
    _run(client, api_key, {"document_type": "passport", "test": True}, files)
    assert client.delete("/admin/api/tests", headers=ADMIN).json()["deleted"] >= 1
    assert client.get("/admin/api/stats", headers=ADMIN).json()["tests"] == 0
    client.delete(f"/v1/verifications/{real['id']}", headers=api_key)
