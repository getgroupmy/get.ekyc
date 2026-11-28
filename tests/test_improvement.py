"""The improvement set: the last days' real checks, owner-only download, gone
when erased, and the hosted notice says so."""
import io
import json
import zipfile
from datetime import timedelta

from tests import synth
from tests.conftest import ADMIN
from tests.test_api import BASE, _run
from tests.test_roles import _browser, _user


def test_improvement_set_download_and_erasure(client, api_key, need_models):
    doc = synth.passport_image(synth.td3_lines(number="I12345678"), synth.face_bgr())
    v = _run(client, api_key, {"document_type": "passport", "external_ref": "imp-1"},
             {"document_front": doc, "selfie": synth.jpeg(synth.selfie())})
    t = _run(client, api_key, {"document_type": "passport", "test": True, "external_ref": "imp-t"},
             {"document_front": doc, "selfie": synth.jpeg(synth.selfie())})
    try:
        s = client.get("/admin/api/improvement", headers=ADMIN).json()
        assert s["days"] == 7 and s["count"] >= 1 and s["by_type"].get("passport", 0) >= 1

        r = client.get("/admin/api/improvement.zip", headers=ADMIN)
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert f"{v['id']}/result.json" in names and f"{v['id']}/document_front.jpg" in names
        assert f"{v['id']}/selfie.jpg" in names and "README.txt" in names
        assert not any(n.startswith(t["id"] + "/") for n in names), "test runs are not in the set"
        label = json.loads(z.read(f"{v['id']}/result.json"))
        assert label["status"] == v["status"] and "external_ref" not in label
        from sqlalchemy import select

        from app.db import AuditLog, session_scope
        with session_scope() as db:
            assert db.scalar(select(AuditLog).where(AuditLog.action == "improvement.exported")) is not None

        # erased: out of the set at once
        assert client.delete(f"{BASE}/{v['id']}", headers=api_key).status_code in (200, 204)
        z = zipfile.ZipFile(io.BytesIO(client.get("/admin/api/improvement.zip", headers=ADMIN).content)) \
            if client.get("/admin/api/improvement", headers=ADMIN).json()["count"] else None
        assert z is None or not any(n.startswith(v["id"] + "/") for n in z.namelist())
    finally:
        client.delete(f"{BASE}/{v['id']}", headers=api_key)
        client.delete(f"/admin/api/verifications/{t['id']}", headers=ADMIN)


def test_only_the_owner_downloads(client, need_models):
    _user(client, "imp-admin", "admin")
    b = _browser("imp-admin")
    try:
        assert b.get("/admin/api/improvement").status_code == 403
        assert b.get("/admin/api/improvement.zip").status_code == 403
    finally:
        b.__exit__(None, None, None)
        client.delete("/admin/api/admins/imp-admin", headers=ADMIN)


def test_old_checks_leave_the_set_and_images_wait_for_it(client, api_key, need_models, monkeypatch):
    from app import service
    from app.config import get_settings
    from app.db import Verification, session_scope, utcnow
    doc = synth.passport_image(synth.td3_lines(number="O12345678"), synth.face_bgr())
    v = _run(client, api_key, {"document_type": "passport", "external_ref": "imp-old"},
             {"document_front": doc, "selfie": synth.jpeg(synth.selfie())})
    try:
        with session_scope() as db:
            row = db.get(Verification, v["id"])
            row.status, row.completed_at = "approved", utcnow() - timedelta(days=3)
        # a 1-day retention must still wait for the 7 improvement days
        monkeypatch.setattr(get_settings(), "retention_days", 1)
        service.housekeeping()
        with session_scope() as db:
            assert not db.get(Verification, v["id"]).media_purged
            db.get(Verification, v["id"]).completed_at = utcnow() - timedelta(days=8)
        ids = [n.split("/")[0] for n in zipfile.ZipFile(io.BytesIO(
            client.get("/admin/api/improvement.zip", headers=ADMIN).content)).namelist()] \
            if client.get("/admin/api/improvement", headers=ADMIN).json()["count"] else []
        assert v["id"] not in ids, "older than the window: no longer offered"
        service.housekeeping()
        with session_scope() as db:
            assert db.get(Verification, v["id"]).media_purged
    finally:
        client.delete(f"{BASE}/{v['id']}", headers=api_key)


def test_hosted_notice_mentions_the_improvement_days(client, api_key):
    v = client.post(BASE, json={"external_ref": "imp-notice"}, headers=api_key).json()
    token = v["hosted_url"].rsplit("/", 1)[-1]
    info = client.get(f"/v1/hosted/{token}").json()
    assert "7 days" in info["consent_text"]["en"] and "7 hari" in info["consent_text"]["ms"]
    t = client.post(BASE, json={"test": True}, headers=api_key).json()
    info = client.get("/v1/hosted/" + t["hosted_url"].rsplit("/", 1)[-1]).json()
    assert "7 days" not in info["consent_text"]["en"]
