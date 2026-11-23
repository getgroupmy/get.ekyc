from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        TypeDecorator, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from . import crypto
from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EncryptedJSON(TypeDecorator):
    """JSON column encrypted with the storage key (holds PII)."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        return None if value is None else crypto.encrypt(json.dumps(value).encode())

    def process_result_value(self, value: bytes | None, dialect: Any) -> Any:
        return None if value is None else json.loads(crypto.decrypt(value))


class EncryptedBytes(TypeDecorator):
    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: bytes | None, dialect: Any) -> bytes | None:
        return None if value is None else crypto.encrypt(value)

    def process_result_value(self, value: bytes | None, dialect: Any) -> bytes | None:
        return None if value is None else crypto.decrypt(value)


class Base(DeclarativeBase):
    pass


class ApiClient(Base):
    __tablename__ = "api_clients"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    webhook_secret: Mapped[str | None] = mapped_column(EncryptedBytes)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Verification(Base):
    __tablename__ = "verifications"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("api_clients.id"), index=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), index=True)
    document_type: Mapped[str] = mapped_column(String(30))
    country: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), index=True, default="created")
    require_liveness: Mapped[bool] = mapped_column(Boolean, default=True)
    liveness_challenge: Mapped[list | None] = mapped_column(JSON)
    claimed: Mapped[dict | None] = mapped_column(EncryptedJSON)
    result: Mapped[dict | None] = mapped_column(EncryptedJSON)
    reasons: Mapped[list | None] = mapped_column(JSON)  # machine readable codes, no PII
    decided_by: Mapped[str | None] = mapped_column(String(60))
    review_note: Mapped[str | None] = mapped_column(Text)
    doc_number_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    face_embedding: Mapped[bytes | None] = mapped_column(EncryptedBytes)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    media_purged: Mapped[bool] = mapped_column(Boolean, default=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    client: Mapped[ApiClient] = relationship()
    media: Mapped[list["Media"]] = relationship(back_populates="verification", cascade="all, delete-orphan")


class Media(Base):
    __tablename__ = "media"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    verification_id: Mapped[str] = mapped_column(ForeignKey("verifications.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    path: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    phash: Mapped[str | None] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    verification: Mapped[Verification] = relationship(back_populates="media")


class SampleDoc(Base):
    """Saved test images for the console's Try it (encrypted on disk like any image)."""
    __tablename__ = "sample_docs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))  # document_front | document_back | selfie
    path: Mapped[str] = mapped_column(String(300))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(60))
    verification_id: Mapped[str | None] = mapped_column(String(40), index=True)
    detail: Mapped[str | None] = mapped_column(Text)  # never put PII here


_engine = None
_Session: sessionmaker[Session] | None = None


def init_db() -> None:
    global _engine, _Session
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.media_dir.mkdir(parents=True, exist_ok=True)
    url = s.db_url
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _pragmas(conn, _):  # noqa: ANN001
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(_engine)
    _migrate(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)


def _migrate(engine) -> None:  # noqa: ANN001
    """Columns added after a database was first created (create_all only makes
    missing tables). Each step is safe to run again."""
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(engine).get_columns("verifications")}
    media_cols = {c["name"] for c in inspect(engine).get_columns("media")}
    with engine.begin() as conn:
        if "phash" not in media_cols:
            conn.execute(text("ALTER TABLE media ADD COLUMN phash VARCHAR(16)"))
        if "is_test" not in cols:
            conn.execute(text("ALTER TABLE verifications ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT 0"))
            # Runs made from the console before tests were marked
            conn.execute(text("UPDATE verifications SET is_test = 1 WHERE external_ref = 'console-test'"))


@contextmanager
def session_scope() -> Iterator[Session]:
    assert _Session is not None, "init_db() not called"
    db = _Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    with session_scope() as db:
        yield db


def audit(db: Session, actor: str, action: str, verification_id: str | None = None, detail: str | None = None) -> None:
    db.add(AuditLog(actor=actor, action=action, verification_id=verification_id, detail=detail))
