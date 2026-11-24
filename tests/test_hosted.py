"""Hosted verification page: the two-way hand-over."""
from urllib.parse import parse_qs, urlsplit

from tests import synth
from tests.conftest import ADMIN

BASE = "/v1/verifications"


def _token(url):
    return urlsplit(url).path.split("/v/")[1]


def _jpeg_file(data):
    return {"file": ("x.jpg", data, "image/jpeg")}


def test_create_returns_link_and_page_is_safe(client, api_key):
    r = client.post(BASE, json={"document_type": "passport", "external_ref": "drv-9", "test": True,
                                "redirect_url": "getride://ekyc-done?from=signup"}, headers=api_key)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["hosted_url"].startswith("http") and "/v/" in v["hosted_url"] and v["hosted_url_expires_at"]
    page = client.get(urlsplit(v["hosted_url"]).path)
    assert page.status_code == 200 and "Identity check" in page.text
    assert page.headers["referrer-policy"] == "no-referrer" and page.headers["cache-control"] == "no-store"
    info = client.get("/v1/hosted/" + _token(v["hosted_url"])).json()
    assert info["state"] == "created" and info["consent_needed"] and info["liveness_challenge"]
    assert "result" not in info and "claimed" not in info
    q = parse_qs(urlsplit(info["cancel_url"]).query)
    assert info["cancel_url"].startswith("getride://ekyc-done?from=signup&")
    assert q["verification_id"] == [v["id"]] and q["status"] == ["cancelled"] and q["external_ref"] == ["drv-9"]


def test_bad_redirects_and_tokens_refused(client, api_key):
    for bad in ("javascript:alert(1)", "http://evil.example/x", "data://x", "notaurl"):
        assert client.post(BASE, json={"redirect_url": bad}, headers=api_key).status_code == 422, bad
    assert client.post(BASE, json={"redirect_url": "http://localhost:3000/done"}, headers=api_key).status_code == 201
    assert client.get("/v1/hosted/" + "x" * 43).status_code == 404
    assert client.get("/v1/hosted/short").status_code == 404


def test_consent_needed_before_upload(client, api_key):
    v = client.post(BASE, json={"document_type": "passport", "test": True}, headers=api_key).json()
    t = _token(v["hosted_url"])
    img = synth.jpeg(synth.selfie())
    assert client.put(f"/v1/hosted/{t}/media/selfie", files=_jpeg_file(img)).status_code == 428
    assert client.post(f"/v1/hosted/{t}/consent", json={"accepted": True, "lang": "ms"}).status_code == 200
    got = client.get(f"{BASE}/{v['id']}", headers=api_key)
    assert got.status_code == 200
    adm = client.get(f"/admin/api/verifications/{v['id']}", headers=ADMIN).json()
    assert adm["consent"]["via"] == "hosted" and adm["consent"]["lang"] == "ms"
    assert client.put(f"/v1/hosted/{t}/media/selfie", files=_jpeg_file(img)).status_code == 200
    assert client.put(f"/v1/hosted/{t}/media/passport", files=_jpeg_file(img)).status_code == 422


def test_new_link_replaces_old(client, api_key):
    v = client.post(BASE, json={"test": True}, headers=api_key).json()
    old = _token(v["hosted_url"])
    new = client.post(f"{BASE}/{v['id']}/link", headers=api_key).json()
    assert client.get("/v1/hosted/" + old).status_code == 404
    assert client.get("/v1/hosted/" + _token(new["hosted_url"])).status_code == 200


def test_full_hosted_flow_hands_back(client, api_key, need_models):
    v = client.post(BASE, json={"document_type": "passport", "country": "UTO", "require_liveness": False, "test": True,
                                "external_ref": "drv-hosted", "redirect_url": "https://app.example.com/kyc/done",
                                "claimed": {"full_name": "Anna Maria Eriksson"}}, headers=api_key).json()
    t = _token(v["hosted_url"])
    client.post(f"/v1/hosted/{t}/consent", json={"accepted": True})
    doc = synth.passport_image(synth.td3_lines(), synth.face_bgr())
    up = client.put(f"/v1/hosted/{t}/media/document_front", files=_jpeg_file(doc)).json()
    assert up["document"]["name"]  # detection result shown to the user, no personal data
    assert "full_name" not in str(up)
    client.put(f"/v1/hosted/{t}/media/selfie", files=_jpeg_file(synth.jpeg(synth.selfie())))
    assert client.post(f"/v1/hosted/{t}/submit").status_code == 202
    st = client.get(f"/v1/hosted/{t}/status").json()
    assert st["finished"] and st["state"] in ("approved", "review", "rejected")
    q = parse_qs(urlsplit(st["return_url"]).query)
    assert st["return_url"].startswith("https://app.example.com/kyc/done?") and q["status"] == [st["state"]]
    assert client.post(f"/v1/hosted/{t}/submit").status_code == 409  # once only
    full = client.get(f"{BASE}/{v['id']}", headers=api_key).json()
    assert full["status"] == st["state"] and full["result"]  # the result goes to the server, not the page


def test_client_downloads_own_photo_only(client, api_key):
    v = client.post(BASE, json={"test": True}, headers=api_key).json()
    img = synth.jpeg(synth.selfie())
    client.put(f"{BASE}/{v['id']}/media/selfie", files=_jpeg_file(img), headers=api_key)
    r = client.get(f"{BASE}/{v['id']}/media/selfie", headers=api_key)
    assert r.status_code == 200 and r.content == img and r.headers["content-type"] == "image/jpeg"
    assert client.get(f"{BASE}/{v['id']}/media/document_back", headers=api_key).status_code == 404
    other = client.post("/admin/api/clients", json={"name": "other-dl"}, headers=ADMIN).json()
    assert client.get(f"{BASE}/{v['id']}/media/selfie", headers={"X-API-Key": other["api_key"]}).status_code == 404
