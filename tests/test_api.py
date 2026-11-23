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
    assert v["status"] == "review" and "document_fields_missing" in v["reasons"]
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


def test_saved_samples_and_detect(client, api_key, need_models):
    front = synth.passport_image(synth.td3_lines(number="R66666666"), synth.face_bgr())
    up = lambda name, data: {"file": (name, data, "image/jpeg")}  # noqa: E731
    a = client.post("/admin/api/samples", data={"label": "Test passport", "kind": "document_front", "make_default": "true"},
                    files=up("p.jpg", front), headers=ADMIN).json()
    b = client.post("/admin/api/samples", data={"label": "Selfie", "kind": "selfie", "make_default": "true"},
                    files=up("s.jpg", synth.jpeg(synth.selfie())), headers=ADMIN).json()
    c = client.post("/admin/api/samples", data={"label": "Other front", "kind": "document_front"},
                    files=up("o.jpg", front), headers=ADMIN).json()
    items = {x["id"]: x for x in client.get("/admin/api/samples", headers=ADMIN).json()["items"]}
    assert items[a["id"]]["is_default"] and not items[c["id"]]["is_default"]
    client.post(f"/admin/api/samples/{c['id']}/default", headers=ADMIN)
    items = {x["id"]: x for x in client.get("/admin/api/samples", headers=ADMIN).json()["items"]}
    assert items[c["id"]]["is_default"] and not items[a["id"]]["is_default"]  # one default per kind
    assert client.get(f"/admin/api/samples/{a['id']}/image", headers=ADMIN).content[:2] == b"\xff\xd8"

    d = client.post("/admin/api/detect", data={"sample_id": a["id"]}, headers=ADMIN).json()
    assert d["document_type"] == "passport" and d["country"] == "UTO" and d["needs_selfie"] and d["how"] == "mrz"
    d2 = client.post("/v1/verifications/detect", files=up("p.jpg", front), headers=api_key).json()
    assert d2["document_type"] == "passport"

    cid = client.get("/admin/api/clients", headers=ADMIN).json()["items"][0]["id"]
    r = client.post("/admin/api/try", data={"client_id": cid, "document_front_sample": a["id"], "selfie_sample": b["id"]},
                    headers=ADMIN)
    assert r.status_code == 202, r.text
    v = client.get(f"/admin/api/verifications/{r.json()['id']}", headers=ADMIN).json()
    assert v["test"] and v["result"]["extracted"]["document_type"] == "passport" and v["result"]["scores"]["face_match"] > 0.5
    for x in (a, b, c):
        assert client.delete(f"/admin/api/samples/{x['id']}", headers=ADMIN).status_code == 204
    assert client.get("/admin/api/samples", headers=ADMIN).json()["items"] == []
    client.delete("/admin/api/tests", headers=ADMIN)


def test_vehicle_document_needs_no_selfie(client, api_key, need_models):
    voc = synth.card_image(None, ["SIJIL PEMILIKAN KENDERAAN", "No. Pendaftaran: WXY 1234", "Tahun Dibuat: 2019"])
    r = client.post(BASE, json={"document_type": "vehicle_registration", "claimed": {"plate": "WXY1234"}}, headers=api_key)
    assert r.json()["required_media"] == ["document_front"]
    vid = r.json()["id"]
    client.put(f"{BASE}/{vid}/media/document_front", files={"file": ("v.jpg", voc, "image/jpeg")}, headers=api_key)
    assert client.post(f"{BASE}/{vid}/submit", headers=api_key).status_code == 202
    v = client.get(f"{BASE}/{vid}", headers=api_key).json()
    assert v["status"] == "approved", v["reasons"]
    assert v["result"]["extracted"]["plate"] == "WXY1234"
    client.delete(f"{BASE}/{vid}", headers=api_key)


def test_client_usage_counts_and_permanent_delete(client, api_key, need_models):
    new = client.post("/admin/api/clients", json={"name": "spare"}, headers=ADMIN).json()
    assert client.delete(f"/admin/api/clients/{new['id']}/permanent", headers=ADMIN).status_code == 409  # still active
    client.delete(f"/admin/api/clients/{new['id']}", headers=ADMIN)
    row = next(c for c in client.get("/admin/api/clients", headers=ADMIN).json()["items"] if c["id"] == new["id"])
    assert row["deletable"] and row["uses"] == 0
    assert client.delete(f"/admin/api/clients/{new['id']}/permanent", headers=ADMIN).status_code == 204
    assert all(c["id"] != new["id"] for c in client.get("/admin/api/clients", headers=ADMIN).json()["items"])
    # a used key is kept
    used = client.post("/admin/api/clients", json={"name": "used"}, headers=ADMIN).json()
    key = {"X-API-Key": used["api_key"]}
    doc = synth.passport_image(synth.td3_lines(number="U77777777"), synth.face_bgr())
    _run(client, key, {"document_type": "passport"}, {"document_front": doc, "selfie": synth.jpeg(synth.selfie())})
    client.delete(f"/admin/api/clients/{used['id']}", headers=ADMIN)
    row = next(c for c in client.get("/admin/api/clients", headers=ADMIN).json()["items"] if c["id"] == used["id"])
    assert row["uses"] == 1 and row["approved"] + row["review"] + row["rejected"] == 1 and not row["deletable"]
    assert client.delete(f"/admin/api/clients/{used['id']}/permanent", headers=ADMIN).status_code == 409


def test_reused_photo_under_another_user_goes_to_review(client, api_key, need_models):
    import cv2
    voc_lines = ["SIJIL PEMILIKAN KENDERAAN", "No. Pendaftaran: JKL 4321", "Tahun Dibuat: 2020"]
    first = synth.card_image(None, voc_lines)
    a = _run(client, api_key, {"document_type": "vehicle_registration", "external_ref": "owner-a"}, {"document_front": first})
    assert a["status"] == "approved", a["reasons"]
    # the same photo, shrunk and recompressed, sent for someone else
    img = cv2.imdecode(__import__("numpy").frombuffer(first, "uint8"), 1)
    again = cv2.imencode(".jpg", cv2.resize(img, None, fx=0.7, fy=0.7), [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
    b = _run(client, api_key, {"document_type": "vehicle_registration", "external_ref": "owner-b"}, {"document_front": again})
    assert b["status"] == "review" and "image_reused" in b["reasons"]
    for v in (a, b):
        client.delete(f"{BASE}/{v['id']}", headers=api_key)


def test_integration_guide_is_served(client):
    page = client.get("/guide")
    assert page.status_code == 200 and "Add identity verification to your app" in page.text
    meta = client.get("/guide/meta").json()
    assert any(t["id"] == "vehicle_registration" for t in meta["document_types"])
    assert meta["reasons"]["face_mismatch"] and meta["max_upload_mb"] > 0
    assert "api_key" not in page.text.lower().replace("x-api-key", "")  # no secrets on the page
