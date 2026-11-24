from __future__ import annotations

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import crypto, service, storage, webhooks
from ..auth import hash_password, need, new_api_key, require_admin
from ..config import get_settings
from ..db import AdminUser, ApiClient, AuditLog, BlockedFace, Media, SampleDoc, Verification, audit, get_db, utcnow
from ..permissions import ROLES, Ctx
from ..pipeline import catalog, images, integrity, mykad
from ..pipeline.detect import detect as detect_document
from ..schemas import CreateClient, Decision, verification_out

router = APIRouter(prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin)])


# --- scope: which clients (and their verifications) this user may see ---------------------
def _own_client_ids(db: Session, ctx: Ctx) -> set[str] | None:
    """None = every client; otherwise the ids of clients this user created."""
    if ctx.sees_all:
        return None
    return set(db.scalars(select(ApiClient.id).where(ApiClient.created_by == ctx.owner_tag)))


def _scoped(q, db: Session, ctx: Ctx):  # noqa: ANN001, ANN202
    ids = _own_client_ids(db, ctx)
    return q if ids is None else q.where(Verification.client_id.in_(ids or {""}))


def _client_for(db: Session, ctx: Ctx, cid: str) -> ApiClient:
    c = db.get(ApiClient, cid)
    if c is None or (not ctx.sees_all and c.created_by != ctx.owner_tag):
        raise HTTPException(404, "client not found")
    return c


def _verification_for(db: Session, ctx: Ctx, vid: str) -> Verification:
    v = db.get(Verification, vid)
    if v is None or (not ctx.sees_all and (v.client is None or v.client.created_by != ctx.owner_tag)):
        raise HTTPException(404, "verification not found")
    return v


def _client_out(c: ApiClient) -> dict:
    return {"id": c.id, "name": c.name, "key_prefix": c.key_prefix, "webhook_url": c.webhook_url,
            "active": c.active, "created_by": c.created_by, "created_at": c.created_at.isoformat() + "Z"}


@router.post("/clients", status_code=201)
def create_client(body: CreateClient, db: Session = Depends(get_db), ctx: Ctx = Depends(need("clients.manage"))):
    key = new_api_key()
    secret = secrets.token_urlsafe(32) if body.webhook_url else None
    c = ApiClient(id="cli_" + secrets.token_hex(8), name=body.name, key_hash=crypto.sha256_hex(key),
                  key_prefix=key[:10], webhook_url=str(body.webhook_url) if body.webhook_url else None,
                  webhook_secret=secret.encode() if secret else None, created_by=ctx.owner_tag)
    db.add(c)
    audit(db, ctx.actor, "client.created", detail=c.id)
    db.flush()
    # The API key and webhook secret are shown exactly once.
    return {**_client_out(c), "api_key": key, "webhook_secret": secret}


def _usage(db: Session) -> dict[str, dict]:
    rows = db.execute(select(Verification.client_id, Verification.is_test, Verification.status, func.count())
                      .group_by(Verification.client_id, Verification.is_test, Verification.status)).all()
    out: dict[str, dict] = {}
    for cid, is_test, status, n in rows:
        u = out.setdefault(cid, {"uses": 0, "approved": 0, "rejected": 0, "review": 0, "tests": 0})
        if is_test:
            u["tests"] += n
            continue
        u["uses"] += n
        if status in ("approved", "rejected", "review"):
            u[status] += n
    return out


@router.get("/clients")
def list_clients(db: Session = Depends(get_db), ctx: Ctx = Depends(need("clients.view"))):
    usage = _usage(db)
    empty = {"uses": 0, "approved": 0, "rejected": 0, "review": 0, "tests": 0}
    q = select(ApiClient).order_by(ApiClient.created_at)
    if not ctx.sees_all:
        q = q.where(ApiClient.created_by == ctx.owner_tag)
    items = []
    for c in db.scalars(q):
        u = usage.get(c.id, empty)
        items.append({**_client_out(c), **u, "deletable": not c.active and u["uses"] == 0 and u["tests"] == 0})
    return {"items": items, "can_manage": ctx.can("clients.manage")}


class WebhookChange(BaseModel):
    webhook_url: HttpUrl | None = None
    new_secret: bool = False


@router.put("/clients/{cid}/webhook")
def set_webhook(cid: str, body: WebhookChange, db: Session = Depends(get_db), ctx: Ctx = Depends(need("clients.manage"))):
    """Set, change or remove a client's webhook address. A secret is made when
    there is none yet (or when asked) and shown once; the API key is unchanged."""
    c = _client_for(db, ctx, cid)
    if not c.active:
        raise HTTPException(409, "this key is revoked")
    c.webhook_url = str(body.webhook_url) if body.webhook_url else None
    secret = None
    if c.webhook_url and (c.webhook_secret is None or body.new_secret):
        secret = secrets.token_urlsafe(32)
        c.webhook_secret = secret.encode()
    if not c.webhook_url:
        c.webhook_secret = None
    audit(db, ctx.actor, "client.webhook_changed", detail=f"{c.id}: {c.webhook_url or 'removed'}"
          + (" (new secret)" if secret else ""))
    return {**_client_out(c), "webhook_secret": secret}


@router.delete("/clients/{cid}/permanent", status_code=204)
def delete_client(cid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("clients.manage"))):
    """Only a revoked key that was never used: its verifications would otherwise
    lose the record of which app sent them."""
    c = _client_for(db, ctx, cid)
    if c.active:
        raise HTTPException(409, "revoke the key first")
    if db.scalar(select(func.count()).select_from(Verification).where(Verification.client_id == cid)):
        raise HTTPException(409, "this key has been used; it stays, revoked, as a record")
    db.delete(c)
    audit(db, ctx.actor, "client.deleted", detail=cid)


@router.delete("/clients/{cid}", status_code=204)
def revoke_client(cid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("clients.manage"))):
    c = _client_for(db, ctx, cid)
    c.active = False
    audit(db, ctx.actor, "client.revoked", detail=c.id)


@router.get("/stats")
def stats(kind: str = "real", db: Session = Depends(get_db), ctx: Ctx = Depends(need("verifications.view"))):
    q = _scoped(select(Verification.status, func.count()).group_by(Verification.status), db, ctx)
    if kind in ("test", "real"):
        q = q.where(Verification.is_test.is_(kind == "test"))
    rows = db.execute(q).all()
    tests = db.scalar(_scoped(select(func.count()).select_from(Verification).where(Verification.is_test.is_(True)), db, ctx))
    return {"by_status": {status: n for status, n in rows}, "tests": tests,
            "test_retention_hours": get_settings().test_retention_hours}


@router.delete("/tests", status_code=200)
def delete_all_tests(db: Session = Depends(get_db), ctx: Ctx = Depends(need("tests.run"))):
    n = 0
    for v in db.scalars(_scoped(select(Verification).where(Verification.is_test.is_(True)), db, ctx)):
        service.delete_entirely(db, v, ctx.actor)
        n += 1
    return {"deleted": n}


@router.post("/verifications/{vid}/test")
def mark_test(vid: str, test: bool = True, db: Session = Depends(get_db),
              ctx: Ctx = Depends(need("verifications.decide"))):
    """Mark a verification as a test (or back to real), e.g. a staff run made through the API."""
    v = _verification_for(db, ctx, vid)
    v.is_test = test
    audit(db, ctx.actor, "verification.marked_" + ("test" if test else "real"), v.id)
    return {"id": v.id, "test": v.is_test}


@router.get("/verifications")
def list_verifications(status: str | None = None, kind: str = "all", limit: int = 100, db: Session = Depends(get_db),
                       ctx: Ctx = Depends(need("verifications.view"))):
    q = _scoped(select(Verification).where(Verification.status != "deleted"), db, ctx)
    if status:
        q = q.where(Verification.status == status)
    if kind in ("test", "real"):
        q = q.where(Verification.is_test.is_(kind == "test"))
    rows = db.scalars(q.order_by(Verification.created_at.desc()).limit(max(1, min(limit, 500))))
    return {"items": [verification_out(v, service.required_media(v), admin=True) for v in rows]}


@router.get("/verifications/{vid}")
def get_verification(vid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("verifications.view"))):
    v = _verification_for(db, ctx, vid)
    out = verification_out(v, service.required_media(v), admin=True)
    if not ctx.can("media.view"):
        out["media"] = []
    log = db.scalars(select(AuditLog).where(AuditLog.verification_id == vid).order_by(AuditLog.id))
    out["audit"] = [{"at": a.at.isoformat() + "Z", "actor": a.actor, "action": a.action, "detail": a.detail}
                    for a in log]
    out["can"] = {p: ctx.can(p) for p in ("verifications.decide", "verifications.erase", "fraud.block", "tests.run")}
    audit(db, ctx.actor, "verification.viewed", vid)
    return out


@router.get("/media/{mid}")
def get_media(mid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("media.view"))):
    m = db.get(Media, mid)
    if m is None:
        raise HTTPException(404, "media not found")
    _verification_for(db, ctx, m.verification_id)
    data = storage.load(m.path, m.id)
    kind = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"
    audit(db, ctx.actor, "media.viewed", m.verification_id, m.kind)
    return Response(data, media_type=kind, headers={"Cache-Control": "no-store"})


@router.post("/verifications/{vid}/decision")
def decide(vid: str, body: Decision, db: Session = Depends(get_db), ctx: Ctx = Depends(need("verifications.decide"))):
    v = _verification_for(db, ctx, vid)
    if v.status not in service.FINAL:
        raise HTTPException(409, f"cannot decide a verification that is '{v.status}'")
    v.status = body.decision
    v.decided_by = "admin"
    v.review_note = body.note
    v.completed_at = utcnow()
    if body.decision == "approved":
        v.reasons = []
    elif not v.reasons:
        v.reasons = ["rejected_by_reviewer"]
    audit(db, ctx.actor, f"verification.manual_{body.decision}", v.id)
    db.flush()
    webhooks.notify(v, v.client)
    return verification_out(v, service.required_media(v), admin=True)


@router.delete("/verifications/{vid}", status_code=204)
def erase(vid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("verifications.view"))):
    """A test is deleted entirely (needs tests.run); a real verification is
    erased (needs verifications.erase): images and personal data removed, the
    record and audit trail kept."""
    v = _verification_for(db, ctx, vid)
    if v.is_test and ctx.can("tests.run"):
        service.delete_entirely(db, v, ctx.actor)
    elif ctx.can("verifications.erase"):
        service.erase(db, v, ctx.actor) if not v.is_test else service.delete_entirely(db, v, ctx.actor)
    else:
        raise HTTPException(403, f"your role ({ctx.role}) cannot erase verifications")


SLOTS = ("document_front", "document_back", "selfie")


async def _read_upload(f: UploadFile | None, kind: str) -> bytes | None:
    if f is None or not f.filename:
        return None
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await f.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{kind} is larger than {get_settings().max_upload_mb} MB")
    try:
        images.decode(data)
    except images.ImageError as exc:
        raise HTTPException(422, f"{kind}: {exc}") from exc
    return data


def _sample_bytes(db: Session, sample_id: str, kind: str) -> bytes:
    sd = db.get(SampleDoc, sample_id)
    if sd is None:
        raise HTTPException(404, f"saved {kind} not found")
    return storage.load(sd.path, sd.id)


# --- saved sample documents ------------------------------------------------------------
def _sample_out(sd: SampleDoc) -> dict:
    return {"id": sd.id, "label": sd.label, "kind": sd.kind, "is_default": sd.is_default,
            "created_at": sd.created_at.isoformat() + "Z"}


@router.get("/samples")
def list_samples(db: Session = Depends(get_db), ctx: Ctx = Depends(need("tests.run"))):
    return {"items": [_sample_out(x) for x in db.scalars(select(SampleDoc).order_by(SampleDoc.kind, SampleDoc.created_at))]}


@router.post("/samples", status_code=201)
async def add_sample(label: str = Form(...), kind: str = Form(...), make_default: bool = Form(False),
                     file: UploadFile = File(...), db: Session = Depends(get_db),
                     ctx: Ctx = Depends(need("samples.manage"))):
    if kind not in SLOTS:
        raise HTTPException(422, f"kind must be one of {SLOTS}")
    data = await _read_upload(file, kind)
    if data is None:
        raise HTTPException(422, "no file")
    sid = "smp_" + secrets.token_hex(10)
    sd = SampleDoc(id=sid, label=label.strip()[:120] or kind, kind=kind, path=storage.save("samples", sid, data))
    db.add(sd)
    if make_default:
        _make_default(db, sd)
    audit(db, ctx.actor, "sample.added", detail=sid)
    db.flush()
    return _sample_out(sd)


def _make_default(db: Session, sd: SampleDoc) -> None:
    for other in db.scalars(select(SampleDoc).where(SampleDoc.kind == sd.kind)):
        other.is_default = other.id == sd.id


@router.post("/samples/{sid}/default")
def set_default_sample(sid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("samples.manage"))):
    sd = db.get(SampleDoc, sid)
    if sd is None:
        raise HTTPException(404, "sample not found")
    _make_default(db, sd)
    return _sample_out(sd)


@router.delete("/samples/{sid}", status_code=204)
def delete_sample(sid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("samples.manage"))):
    sd = db.get(SampleDoc, sid)
    if sd is None:
        raise HTTPException(404, "sample not found")
    storage.delete(sd.path)
    db.delete(sd)
    audit(db, ctx.actor, "sample.deleted", detail=sid)


@router.get("/samples/{sid}/image")
def sample_image(sid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("tests.run"))):
    sd = db.get(SampleDoc, sid)
    if sd is None:
        raise HTTPException(404, "sample not found")
    data = storage.load(sd.path, sd.id)
    kind = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"
    return Response(data, media_type=kind, headers={"Cache-Control": "no-store"})


# --- detect only (nothing stored) ----------------------------------------------------------
@router.post("/detect")
async def detect(file: UploadFile | None = File(None), sample_id: str = Form(""), db: Session = Depends(get_db),
                 ctx: Ctx = Depends(need("tests.run"))):
    data = await _read_upload(file, "document") if file else (_sample_bytes(db, sample_id, "document") if sample_id else None)
    if data is None:
        raise HTTPException(422, "send a file or a sample_id")
    return detect_document(data)


# --- the console's Try it ---------------------------------------------------------------------
@router.post("/try", status_code=202)
async def try_verification(
    background: BackgroundTasks,
    client_id: str = Form(...),
    document_type: str = Form("auto"),
    country: str = Form(""),
    full_name: str = Form(""),
    document_number: str = Form(""),
    address: str = Form(""),
    plate: str = Form(""),
    document_front: UploadFile | None = File(None),
    document_back: UploadFile | None = File(None),
    selfie: UploadFile | None = File(None),
    document_front_sample: str = Form(""),
    document_back_sample: str = Form(""),
    selfie_sample: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Ctx = Depends(need("tests.run")),
):
    """Run as the chosen API client (keys are stored hashed and cannot be listed),
    with uploaded images or saved samples. Always a test."""
    c = _client_for(db, ctx, client_id)
    if not c.active:
        raise HTTPException(404, "client not found or revoked")
    if document_type != "auto" and document_type not in catalog.TYPES:
        raise HTTPException(422, "unknown document type")
    country = country.strip().upper() or None
    if country and len(country) != 3:
        raise HTTPException(422, "country must be ISO alpha-3, e.g. MYS")
    if document_number and country == "MYS" and document_type == "national_id" and not mykad.parse_number(document_number):
        raise HTTPException(422, "MyKad number must be 12 digits, YYMMDD-PB-####, dashes optional")
    media: dict[str, bytes] = {}
    uploads = {"document_front": document_front, "document_back": document_back, "selfie": selfie}
    picks = {"document_front": document_front_sample, "document_back": document_back_sample, "selfie": selfie_sample}
    for kind in SLOTS:
        data = await _read_upload(uploads[kind], kind)
        if data is None and picks[kind]:
            data = _sample_bytes(db, picks[kind], kind)
        if data is not None:
            media[kind] = data
    if "document_front" not in media:
        raise HTTPException(422, "a document front is needed (upload one or pick a saved one)")
    claimed = {k: v.strip() for k, v in (("full_name", full_name), ("document_number", document_number),
                                         ("address", address), ("plate", plate)) if v.strip()} or None
    v = Verification(id="ver_" + secrets.token_hex(12), client_id=c.id, external_ref="console-test",
                     document_type=document_type, country=country, require_liveness=False,
                     liveness_challenge=None, claimed=claimed, is_test=True)
    db.add(v)
    db.flush()
    for kind, data in media.items():
        mid = "med_" + secrets.token_hex(12)
        v.media.append(Media(id=mid, kind=kind, path=storage.save(v.id, mid, data), sha256=crypto.sha256_hex(data),
                           phash=integrity.phash(data)))
    v.status = "processing"
    v.submitted_at = utcnow()
    audit(db, ctx.actor, "verification.console_test", v.id, detail=c.id)
    db.commit()
    background.add_task(service.process, v.id)
    return {"id": v.id, "status": v.status}


# --- face blocklist (all clients) ----------------------------------------------------------
@router.post("/verifications/{vid}/block")
def block_person(vid: str, reason: str = Form("confirmed fraud"), db: Session = Depends(get_db),
                 ctx: Ctx = Depends(need("fraud.block"))):
    actor = ctx.actor
    """Add this verification's selfie face to the blocklist and reject it."""
    v = db.get(Verification, vid)
    if v is None or not v.face_embedding:
        raise HTTPException(404, "verification or its face not found")
    b = BlockedFace(id="blk_" + secrets.token_hex(8), embedding=v.face_embedding, reason=reason.strip()[:300] or "fraud",
                    verification_id=v.id)
    db.add(b)
    if v.status in service.FINAL:
        v.status, v.decided_by, v.completed_at = "rejected", "admin", utcnow()
        v.reasons = ["face_blocklisted"]
    audit(db, actor, "face.blocked", v.id, detail=b.id)
    db.flush()
    webhooks.notify(v, v.client)
    return {"id": b.id}


@router.get("/blocklist")
def list_blocklist(db: Session = Depends(get_db), ctx: Ctx = Depends(need("fraud.block"))):
    return {"items": [{"id": b.id, "reason": b.reason, "verification_id": b.verification_id,
                       "created_at": b.created_at.isoformat() + "Z"} for b in db.scalars(select(BlockedFace))]}


@router.delete("/blocklist/{bid}", status_code=204)
def unblock(bid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("fraud.block"))):
    b = db.get(BlockedFace, bid)
    if b is None:
        raise HTTPException(404, "not found")
    db.delete(b)
    audit(db, ctx.actor, "face.unblocked", b.verification_id, detail=bid)


@router.get("/audit/verify")
def verify_audit(db: Session = Depends(get_db), ctx: Ctx = Depends(need("audit.view"))):
    from ..db import verify_audit_chain
    return verify_audit_chain(db)


@router.get("/sanctions")
def sanctions_status(ctx: Ctx = Depends(need("compliance.view"))):
    from .. import sanctions
    return sanctions.status()


@router.post("/sanctions/refresh")
def sanctions_refresh(ctx: Ctx = Depends(need("compliance.manage"))):
    from .. import sanctions
    changed = sanctions.refresh(force=True)
    hits = service.rescreen_all() if changed else 0
    return {**sanctions.status(), "changed": changed, "rescreen_hits": hits}


@router.get("/learning")
def learning_report(db: Session = Depends(get_db), ctx: Ctx = Depends(need("learning.view"))):
    """What reviewers' decisions say about the engine, with suggestions (never applied automatically)."""
    from .. import learning
    return learning.report(db)


# --- console users (owners only) -----------------------------------------------------------
class NewAdmin(BaseModel):
    username: str = Field(min_length=3, max_length=60, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str | None = Field(default=None, min_length=10, max_length=200)
    role: str = Field(default="reviewer")


def _owners_left(db: Session, excluding: str | None = None) -> int:
    q = select(func.count()).select_from(AdminUser).where(AdminUser.role == "owner")
    if excluding:
        q = q.where(AdminUser.username != excluding)
    return db.scalar(q) or 0


@router.get("/roles")
def list_roles(ctx: Ctx = Depends(need("admins.manage"))):
    from ..permissions import PERMISSIONS
    return {"roles": {k: {"label": v["label"], "scope": v["scope"], "permissions": sorted(v["perms"])}
                      for k, v in ROLES.items()}, "permissions": PERMISSIONS}


@router.get("/admins")
def list_admins(db: Session = Depends(get_db), ctx: Ctx = Depends(need("admins.manage"))):
    return {"items": [{"username": a.username, "role": a.role, "created_at": a.created_at.isoformat() + "Z",
                       "last_login_at": a.last_login_at.isoformat() + "Z" if a.last_login_at else None}
                      for a in db.scalars(select(AdminUser).order_by(AdminUser.created_at))]}


@router.post("/admins", status_code=201)
def add_admin(body: NewAdmin, db: Session = Depends(get_db), ctx: Ctx = Depends(need("admins.manage"))):
    """Create a user, or for an existing username change its password and/or role."""
    if body.role not in ROLES:
        raise HTTPException(422, f"role must be one of {sorted(ROLES)}")
    name = body.username.lower()
    existing = db.get(AdminUser, name)
    if existing:
        if existing.role == "owner" and body.role != "owner" and _owners_left(db, excluding=name) == 0:
            raise HTTPException(409, "keep at least one owner")
        changed = []
        if body.password:
            existing.password_hash = hash_password(body.password)
            changed.append("password")
        if body.role != existing.role:
            existing.role = body.role
            changed.append(f"role={body.role}")
        audit(db, ctx.actor, "admin.updated", detail=f"{name}: {', '.join(changed) or 'no change'}")
        return {"username": name, "role": existing.role, "changed": True}
    if not body.password:
        raise HTTPException(422, "a new user needs a password (10+ characters)")
    db.add(AdminUser(username=name, password_hash=hash_password(body.password), role=body.role))
    audit(db, ctx.actor, "admin.created", detail=f"{name} ({body.role})")
    return {"username": name, "role": body.role, "changed": False}


@router.delete("/admins/{username}", status_code=204)
def delete_admin(username: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("admins.manage"))):
    a = db.get(AdminUser, username.lower())
    if a is None:
        raise HTTPException(404, "not found")
    if a.role == "owner" and _owners_left(db, excluding=a.username) == 0:
        raise HTTPException(409, "keep at least one owner")
    db.delete(a)
    audit(db, ctx.actor, "admin.deleted", detail=a.username)


# --- version, one-tap update, change log ---------------------------------------------------
@router.get("/system")
def system_status(ctx: Ctx = Depends(need("system.update"))):
    from .. import system
    try:
        return system.check()
    except Exception as exc:  # noqa: BLE001 - e.g. no network to fetch
        return {**system.version(), "updatable": False, "reason": str(exc)[:300], "waiting": []}


@router.post("/system/update")
def system_update(db: Session = Depends(get_db), ctx: Ctx = Depends(need("system.update"))):
    from .. import system
    try:
        out = system.update()
    except system.UpdateError as exc:
        system.record({"status": "failed", "how": "console", "by": ctx.username or "token",
                       "from": system.version().get("commit"), "to": None, "error": str(exc)[:1000], "changes": []})
        audit(db, ctx.actor, "system.update_failed", detail=str(exc)[:300])
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    system.record({"status": "updated", "how": "console", "by": ctx.username or "token",
                   "from": out["from"], "to": out["to"], "changes": out["applied"], "steps": out["steps"]})
    audit(db, ctx.actor, "system.updated", detail=f"{out['from']} -> {out['to']}")
    db.commit()
    system.restart_soon()
    return {**out, "restarting": True}


@router.get("/changelog")
def change_log(_: str = Depends(require_admin)):
    from .. import system
    return {"current": system.version(), "entries": system.changelog()}


# --- SDK downloads for app developers -------------------------------------------------------
_SDK_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2] / "sdk"
_SDKS = {
    "flutter": {"name": "Flutter SDK (iOS and Android)", "path": "flutter/get_ekyc", "package": "get_ekyc",
                "modes": ["native screens", "hosted page in the app", "browser and back"]},
}
_SKIP = {".dart_tool", "build", ".idea", ".packages"}


def _sdk_version(sdk: dict) -> str:
    import re as _re
    pub = (_SDK_ROOT / sdk["path"] / "pubspec.yaml")
    m = _re.search(r"^version:\s*(\S+)", pub.read_text(), _re.M) if pub.exists() else None
    return m.group(1) if m else "0"


@router.get("/sdk")
def sdk_list(ctx: Ctx = Depends(need("clients.view"))):
    return {"items": [{"id": k, "name": v["name"], "version": _sdk_version(v), "modes": v["modes"],
                       "available": (_SDK_ROOT / v["path"]).is_dir()} for k, v in _SDKS.items()]}


@router.get("/sdk/{sid}.zip")
def sdk_download(sid: str, db: Session = Depends(get_db), ctx: Ctx = Depends(need("clients.view"))):
    import io
    import zipfile
    sdk = _SDKS.get(sid)
    base = _SDK_ROOT / sdk["path"] if sdk else None
    if sdk is None or not base.is_dir():
        raise HTTPException(404, "no such SDK")
    top = f"{sdk['package']}-{_sdk_version(sdk)}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(base.rglob("*")):
            rel = f.relative_to(base)
            if not f.is_file() or set(rel.parts) & _SKIP or f.name == "pubspec.lock":
                continue
            z.write(f, f"{top}/{rel.as_posix()}")
    audit(db, ctx.actor, "sdk.downloaded", detail=top)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{top}.zip"', "Cache-Control": "no-store"})
