"""Console sign-in with username and password."""
from fastapi.testclient import TestClient

from tests.conftest import ADMIN


def test_admin_password_sign_in_flow(client):
    from app.main import app
    # first admin is created with the token
    r = client.post("/admin/api/admins", json={"username": "Kabeer", "password": "a-strong-passphrase"}, headers=ADMIN)
    assert r.status_code == 201 and r.json()["username"] == "kabeer"
    assert client.post("/admin/api/admins", json={"username": "x", "password": "short"}, headers=ADMIN).status_code == 422

    with TestClient(app) as browser:
        assert browser.get("/admin/session/me").json() == {"username": None, "admins_exist": True}
        assert browser.get("/admin/api/stats").status_code == 401
        assert browser.post("/admin/session/login", json={"username": "kabeer", "password": "wrong"}).status_code == 401
        ok = browser.post("/admin/session/login", json={"username": "KABEER", "password": "a-strong-passphrase", "remember": True})
        assert ok.status_code == 200
        cookie = ok.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Max-Age=2592000" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
        assert browser.get("/admin/session/me").json()["username"] == "kabeer"
        assert browser.get("/admin/api/stats").status_code == 200  # the cookie is enough
        # actions are recorded under the person's name
        vid = client.post("/v1/verifications", json={"document_type": "passport"},
                          headers={"X-API-Key": client.post("/admin/api/clients", json={"name": "s"}, headers=ADMIN).json()["api_key"]}).json()["id"]
        browser.get(f"/admin/api/verifications/{vid}")
        audit = client.get(f"/admin/api/verifications/{vid}", headers=ADMIN).json()["audit"]
        assert any(a["actor"] == "admin:kabeer" and a["action"] == "verification.viewed" for a in audit)
        browser.post("/admin/session/logout")
        assert browser.get("/admin/api/stats").status_code == 401

    with TestClient(app) as attacker:  # throttled after 5 wrong passwords
        codes = [attacker.post("/admin/session/login", json={"username": "kabeer", "password": f"guess{i}"}).status_code
                 for i in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429

    # a forged or expired cookie is refused; removing the admin ends its sessions
    from app.auth import make_session, read_session
    value, _ = make_session("kabeer", False)
    assert read_session(value) == "kabeer" and read_session(value.replace("kabeer", "admin1")) is None
    assert client.delete("/admin/api/admins/kabeer", headers=ADMIN).status_code == 204
    with TestClient(app) as old:
        old.cookies.set("ekyc_session", value, path="/admin")
        assert old.get("/admin/api/stats").status_code == 401
