"""Encryption at rest (AES-256-GCM) and keyed hashing for lookups."""
from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

_NONCE = 12


def encrypt(data: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(_NONCE)
    return nonce + AESGCM(get_settings().storage_key_bytes).encrypt(nonce, data, aad or None)


def decrypt(blob: bytes, aad: bytes = b"") -> bytes:
    return AESGCM(get_settings().storage_key_bytes).decrypt(blob[:_NONCE], blob[_NONCE:], aad or None)


def keyed_hash(value: str) -> str:
    """Deterministic HMAC so we can find duplicates without storing the raw value."""
    return hmac.new(get_settings().storage_key_bytes, value.encode(), hashlib.sha256).hexdigest()


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()
