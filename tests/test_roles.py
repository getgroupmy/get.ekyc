"""Roles: what each console user may do and see."""
from fastapi.testclient import TestClient

from tests import synth
from tests.conftest import ADMIN


def _user(client, name, role):
    r = client.post("/admin/api/admins", json={"username": name, "password": "pass-word-123", "role": role}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _browser(name):
    from app.main import app
    b = TestClient(app)
    b.__enter__()
    assert b.post("/admin/session/login", json={"username": name, "password": "pass-word-123"}).status_code == 200
    return b


def test_roles_and_scope(client, need_models):
    owners_client = client.post("/admin/api/clients", json={"name": "owner-app"}, headers=ADMIN).json()
    for name, role in (("dev1", "developer"), ("dev2", "developer"), ("rev1", "reviewer"), ("aud1", "auditor")):
        _user(client, name, role)
    dev, dev2, rev, aud = _browser("dev1"), _browser("dev2"), _browser("rev1"), _browser("aud1")
    try:
        me = dev.get("/admin/session/me").json()
        assert me["role"] == "developer" and not me["sees_all"] and "clients.manage" in me["permissions"]

        # developer: own keys only
        mine = dev.post("/admin/api/clients", json={"name": "dev-app"}).json()
        assert mine["created_by"] == "dev1"
        ids = {c["id"] for c in dev.get("/admin/api/clients").json()["items"]}
        assert mine["id"] in ids and owners_client["id"] not in ids
        assert dev2.get("/admin/api/clients").json()["items"] == []  # another developer sees none of them
        assert dev.delete(f"/admin/api/clients/{owners_client['id']}").status_code == 404
        assert dev2.delete(f"/admin/api/clients/{mine['id']}").status_code == 404
        # developer: Try it with own key, not with someone else's
        files = {"document_front": ("f.jpg", synth.passport_image(synth.td3_lines(number="Z12121212"), synth.face_bgr()), "image/jpeg"),
                 "selfie": ("s.jpg", synth.jpeg(synth.selfie()), "image/jpeg")}
        run = dev.post("/admin/api/try", data={"client_id": mine["id"]}, files=files)
        assert run.status_code == 202
        assert dev.post("/admin/api/try", data={"client_id": owners_client["id"]}, files=files).status_code == 404
        vid = run.json()["id"]
        assert dev.get(f"/admin/api/verifications/{vid}").status_code == 200
        assert dev2.get(f"/admin/api/verifications/{vid}").status_code == 404
        assert all(v["client"] == "dev-app" for v in dev.get("/admin/api/verifications?kind=all").json()["items"])
        # developer: no decisions, no console users
        assert dev.post(f"/admin/api/verifications/{vid}/decision", json={"decision": "approved"}).status_code == 403
        assert dev.post("/admin/api/admins", json={"username": "x1y", "password": "pass-word-123"}).status_code == 403

        # reviewer: decides, sees every case, no keys
        assert rev.get(f"/admin/api/verifications/{vid}").status_code == 200
        assert rev.post(f"/admin/api/verifications/{vid}/decision", json={"decision": "approved"}).status_code == 200
        assert rev.get("/admin/api/clients").status_code == 403
        assert rev.post("/admin/api/clients", json={"name": "no"}).status_code == 403

        # auditor: reads, changes nothing
        assert aud.get("/admin/api/audit/verify").json()["ok"]
        assert aud.get(f"/admin/api/verifications/{vid}").status_code == 200
        assert aud.post(f"/admin/api/verifications/{vid}/decision", json={"decision": "rejected"}).status_code == 403
        assert aud.delete(f"/admin/api/verifications/{vid}").status_code == 403

        # the audit trail names the people
        trail = client.get(f"/admin/api/verifications/{vid}", headers=ADMIN).json()["audit"]
        actors = {a["actor"] for a in trail}
        assert {"admin:dev1", "admin:rev1"} <= actors

        # role change applies at once
        client.post("/admin/api/admins", json={"username": "dev1", "role": "auditor"}, headers=ADMIN)
        assert dev.post("/admin/api/clients", json={"name": "late"}).status_code == 403
    finally:
        for b in (dev, dev2, rev, aud):
            b.__exit__(None, None, None)
        for name in ("dev1", "dev2", "rev1", "aud1"):
            client.delete(f"/admin/api/admins/{name}", headers=ADMIN)


def test_keep_one_owner(client):
    _user(client, "boss", "owner")
    try:
        # boss is the only owner account (the token is not an account)
        assert client.post("/admin/api/admins", json={"username": "boss", "role": "admin"}, headers=ADMIN).status_code == 409
        assert client.delete("/admin/api/admins/boss", headers=ADMIN).status_code == 409
        _user(client, "boss2", "owner")
        assert client.delete("/admin/api/admins/boss", headers=ADMIN).status_code == 204
    finally:
        _user(client, "boss", "owner")
        client.delete("/admin/api/admins/boss2", headers=ADMIN)
        # leave no owner behind for other tests: demote is blocked, so drop the table row directly
        from app.db import AdminUser, session_scope
        with session_scope() as db:
            for u in db.query(AdminUser).all():
                db.delete(u)


def test_webhook_can_be_added_later(client):
    from tests.conftest import ADMIN
    c = client.post("/admin/api/clients", json={"name": "hook-later"}, headers=ADMIN).json()
    assert c["webhook_secret"] is None
    r = client.put(f"/admin/api/clients/{c['id']}/webhook", json={"webhook_url": "https://app.example.com/hook"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["webhook_secret"]  # first secret shown once
    r2 = client.put(f"/admin/api/clients/{c['id']}/webhook", json={"webhook_url": "https://app.example.com/hook2"}, headers=ADMIN)
    assert r2.json()["webhook_secret"] is None and r2.json()["webhook_url"] == "https://app.example.com/hook2"
    r3 = client.put(f"/admin/api/clients/{c['id']}/webhook", json={"webhook_url": "https://app.example.com/hook2", "new_secret": True}, headers=ADMIN)
    assert r3.json()["webhook_secret"] and r3.json()["webhook_secret"] != r.json()["webhook_secret"]
    assert client.put(f"/admin/api/clients/{c['id']}/webhook", json={"webhook_url": "ftp://x"}, headers=ADMIN).status_code == 422


def test_sdk_download_zip(client):
    import io
    import zipfile
    from tests.conftest import ADMIN
    items = client.get("/admin/api/sdk", headers=ADMIN).json()["items"]
    fl = next(x for x in items if x["id"] == "flutter")
    assert fl["available"] and fl["version"]
    r = client.get("/admin/api/sdk/flutter.zip", headers=ADMIN)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    top = f"get_ekyc-{fl['version']}/"
    assert top + "pubspec.yaml" in names and top + "lib/get_ekyc.dart" in names and top + "README.md" in names
    assert not any(".dart_tool" in n or "/build/" in n or n.endswith("pubspec.lock") for n in names)
    assert client.get("/admin/api/sdk/flutter.zip").status_code == 401
    assert client.get("/admin/api/sdk/nope.zip", headers=ADMIN).status_code == 404
