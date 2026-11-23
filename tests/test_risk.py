"""Repeat attempts, face blocklist, re-authentication, sanctions, audit chain, consent."""
import numpy as np
import pytest

from tests import synth
from tests.conftest import ADMIN

BASE = "/v1/verifications"

UN_XML = b"""<?xml version="1.0"?><CONSOLIDATED_LIST><INDIVIDUALS>
<INDIVIDUAL><DATAID>1</DATAID><REFERENCE_NUMBER>QDi.999</REFERENCE_NUMBER>
<FIRST_NAME>ANNA</FIRST_NAME><SECOND_NAME>MARIA</SECOND_NAME><THIRD_NAME>ERIKSSON</THIRD_NAME>
<INDIVIDUAL_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>Anna Eriksdottir</ALIAS_NAME></INDIVIDUAL_ALIAS>
<INDIVIDUAL_DATE_OF_BIRTH><TYPE_OF_DATE>EXACT</TYPE_OF_DATE><DATE>1974-08-12</DATE></INDIVIDUAL_DATE_OF_BIRTH>
</INDIVIDUAL></INDIVIDUALS></CONSOLIDATED_LIST>"""


def _run(client, key, body, files):
    from tests.test_api import _run as run
    return run(client, key, body, files)


def _passport(number="P11111111", dob="740812", surname="ERIKSSON", given="ANNA MARIA"):
    return synth.passport_image(synth.td3_lines(number=number, dob=dob, surname=surname, given=given), synth.face_bgr())


def test_same_id_number_with_a_different_name_is_flagged(client, api_key, need_models):
    selfie = synth.jpeg(synth.selfie())
    a = _run(client, api_key, {"document_type": "passport", "external_ref": "r-1"},
             {"document_front": _passport(), "selfie": selfie})
    b = _run(client, api_key, {"document_type": "passport", "external_ref": "r-2"},
             {"document_front": _passport(surname="LARSSON", given="EVA"), "selfie": selfie})
    assert "document_details_changed" in b["reasons"]
    for v in (a, b):
        client.delete(f"{BASE}/{v['id']}", headers=api_key)


def test_blocked_face_is_refused_for_every_client(client, api_key, need_models):
    selfie = synth.jpeg(synth.selfie())
    a = _run(client, api_key, {"document_type": "passport", "external_ref": "fraudster"},
             {"document_front": _passport("B22222222"), "selfie": selfie})
    r = client.post(f"/admin/api/verifications/{a['id']}/block", data={"reason": "rented account"}, headers=ADMIN)
    assert r.status_code == 200
    other = client.post("/admin/api/clients", json={"name": "other-app"}, headers=ADMIN).json()
    b = _run(client, {"X-API-Key": other["api_key"]}, {"document_type": "passport", "external_ref": "new-name"},
             {"document_front": _passport("B33333333"), "selfie": selfie})
    assert b["status"] == "rejected" and b["reasons"][0] == "face_blocklisted"
    bl = client.get("/admin/api/blocklist", headers=ADMIN).json()["items"]
    for x in bl:
        client.delete(f"/admin/api/blocklist/{x['id']}", headers=ADMIN)
    client.delete(f"{BASE}/{a['id']}", headers=api_key)


def test_reauth_matches_the_verified_driver(client, api_key, need_models):
    selfie = synth.jpeg(synth.selfie())
    a = _run(client, api_key, {"document_type": "passport", "external_ref": "driver-77"},
             {"document_front": _passport("D44444444"), "selfie": selfie})
    if a["status"] == "review":  # other tests share this face: approve it as a reviewer would
        client.post(f"/admin/api/verifications/{a['id']}/decision", json={"decision": "approved"}, headers=ADMIN)
    same = client.post(f"{BASE}/reauth", data={"external_ref": "driver-77"},
                       files={"selfie": ("s.jpg", synth.jpeg(synth.selfie(5)), "image/jpeg")}, headers=api_key).json()
    assert same["match"] and same["verification_id"] == a["id"]
    nobody = client.post(f"{BASE}/reauth", data={"external_ref": "driver-77"},
                         files={"selfie": ("s.jpg", synth.jpeg(np.full((500, 500, 3), 120, np.uint8)), "image/jpeg")},
                         headers=api_key).json()
    assert not nobody["match"] and nobody["reason"] == "no_face"
    assert client.post(f"{BASE}/reauth", data={"external_ref": "unknown"},
                       files={"selfie": ("s.jpg", selfie, "image/jpeg")}, headers=api_key).status_code == 404
    client.delete(f"{BASE}/{a['id']}", headers=api_key)


def test_sanctions_screening_and_rescreen(client, api_key, need_models):
    from app import sanctions, service
    from app.config import get_settings
    s = get_settings()
    s.sanctions_enabled = True
    try:
        (sanctions._dir() / "un.xml").write_bytes(UN_XML)
        assert sanctions.screen("ANNA MARIA ERIKSSON", "1974-08-12")[0]["reference"] == "QDi.999"
        assert sanctions.screen("ERIKSSON ANNA MARIA", None)  # any order
        assert not sanctions.screen("ANNA MARIA ERIKSSON", "1990-01-01")  # birth year contradicts
        assert not sanctions.screen("SITI AMINAH", None)
        v = _run(client, api_key, {"document_type": "passport", "external_ref": "s-1"},
                 {"document_front": _passport("S55555555"), "selfie": synth.jpeg(synth.selfie())})
        assert v["status"] == "review" and "sanctions_possible_match" in v["reasons"]
        assert client.get("/admin/api/sanctions", headers=ADMIN).json()["entries"] == 1
        client.delete(f"{BASE}/{v['id']}", headers=api_key)
    finally:
        (sanctions._dir() / "un.xml").unlink(missing_ok=True)
        s.sanctions_enabled = False


def test_audit_chain_detects_edits(client, api_key):
    from app.db import AuditLog, session_scope
    assert client.get("/admin/api/audit/verify", headers=ADMIN).json()["ok"]
    with session_scope() as db:
        row = db.query(AuditLog).filter(AuditLog.row_hash.is_not(None)).order_by(AuditLog.id).first()
        original = row.detail
        row.detail = "tampered"
    broken = client.get("/admin/api/audit/verify", headers=ADMIN).json()
    assert not broken["ok"] and broken["broken_at_id"] == row.id
    with session_scope() as db:
        db.get(AuditLog, row.id).detail = original
    assert client.get("/admin/api/audit/verify", headers=ADMIN).json()["ok"]


def test_consent_is_recorded(client, api_key):
    r = client.post(BASE, json={"document_type": "passport", "consent": {"version": "2026-11",
                                "accepted_at": "2026-11-23T10:00:00Z", "text_sha256": "ab" * 32}}, headers=api_key)
    vid = r.json()["id"]
    v = client.get(f"/admin/api/verifications/{vid}", headers=ADMIN).json()
    assert v["consent"]["version"] == "2026-11"
    assert any("consent 2026-11" in (a["detail"] or "") for a in v["audit"])


@pytest.mark.parametrize("postcode,state,ok", [("43000", "SELANGOR", True), ("43000", "JOHOR", False),
                                               ("68000", "KUALA LUMPUR", True), ("88000", "SABAH", True),
                                               ("00000", "SELANGOR", None)])
def test_postcode_state(postcode, state, ok):
    from app.pipeline import mykad
    assert mykad.postcode_matches_state(postcode, state) is ok


def test_learning_report_from_reviewer_decisions(client, api_key, need_models):
    r = client.get("/admin/api/learning", headers=ADMIN).json()
    assert {"reviewed_cases", "face_match", "suggestions", "engine_vs_reviewer"} <= set(r)
    v = _run(client, api_key, {"document_type": "passport", "external_ref": "learn-1"},
             {"document_front": _passport("L66666666"), "selfie": synth.jpeg(synth.selfie())})
    client.post(f"/admin/api/verifications/{v['id']}/decision", json={"decision": "approved"}, headers=ADMIN)
    r2 = client.get("/admin/api/learning", headers=ADMIN).json()
    assert r2["approved_by_reviewer"] >= r["approved_by_reviewer"] + 1
    assert r2["face_match"]["genuine_scored"] >= 1
    client.delete(f"{BASE}/{v['id']}", headers=api_key)
