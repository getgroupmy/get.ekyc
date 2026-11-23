"""Encrypted image storage on local disk (the NAS / Mac volume)."""
from __future__ import annotations

from pathlib import Path

from . import crypto
from .config import get_settings


def _full(rel: str) -> Path:
    base = get_settings().media_dir.resolve()
    p = (base / rel).resolve()
    if base not in p.parents:
        raise ValueError("path escapes media dir")
    return p


def save(verification_id: str, media_id: str, data: bytes) -> str:
    rel = f"{verification_id}/{media_id}.bin"
    p = _full(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(crypto.encrypt(data, aad=media_id.encode()))
    return rel


def load(rel: str, media_id: str) -> bytes:
    return crypto.decrypt(_full(rel).read_bytes(), aad=media_id.encode())


def delete(rel: str) -> None:
    p = _full(rel)
    p.unlink(missing_ok=True)
    try:
        p.parent.rmdir()
    except OSError:
        pass
