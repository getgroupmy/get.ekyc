"""Runtime configuration. Every setting can be overridden with an EKYC_* env var."""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EKYC_", env_file=".env", extra="ignore")

    # --- required secrets -------------------------------------------------
    admin_token: str  # protects /admin/*; at least 24 chars
    storage_key: str  # base64 of 32 random bytes; encrypts images + PII at rest

    # --- paths ------------------------------------------------------------
    data_dir: Path = Path("/data")
    models_dir: Path = Path("/app/models")
    database_url: str = ""  # default: sqlite file inside data_dir

    # --- decision policy --------------------------------------------------
    auto_approve: bool = True  # False => every clean result still goes to manual review
    min_age: int = 18
    face_match_threshold: float = 0.363  # SFace cosine similarity; >= is a match
    face_review_threshold: float = 0.25  # between review and match => manual review
    duplicate_face_threshold: float = 0.55
    liveness_default: bool = True
    liveness_yaw_turn: float = 0.20  # min |nose offset / eye distance| for a head turn
    liveness_yaw_center: float = 0.13  # max for the "center" step
    name_match_threshold: int = 80  # 0-100 fuzzy score for claimed name vs document
    # Optional passive anti-spoof model (see README). Used only if the file exists.
    passive_liveness_model: Path | None = None  # default: <models_dir>/antispoof.onnx
    passive_liveness_threshold: float = 0.5  # real-minus-spoof logit margin below this is flagged
    passive_liveness_action: str = "review"  # "review" or "reject"

    # --- limits / housekeeping -------------------------------------------
    max_upload_mb: int = 12
    session_ttl_minutes: int = 60  # unsubmitted sessions expire after this
    retention_days: int = 0  # 0 = keep; N = delete images N days after completion
    sanctions_enabled: bool = True  # screen people against the UN (and MOHA CSV) lists
    sanctions_un_url: str = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
    test_retention_hours: int = 48  # test verifications are deleted entirely after this (0 = keep)
    webhook_timeout_seconds: float = 10.0
    cors_origins: str = ""  # comma separated; empty = no CORS

    @field_validator("admin_token")
    @classmethod
    def _admin_token_strong(cls, v: str) -> str:
        if len(v) < 24:
            raise ValueError("EKYC_ADMIN_TOKEN must be at least 24 characters")
        return v

    @field_validator("storage_key")
    @classmethod
    def _storage_key_valid(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("EKYC_STORAGE_KEY must be base64") from exc
        if len(raw) != 32:
            raise ValueError("EKYC_STORAGE_KEY must decode to exactly 32 bytes")
        return v

    @property
    def storage_key_bytes(self) -> bytes:
        return base64.b64decode(self.storage_key)

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'ekyc.db'}"

    @property
    def antispoof_model(self) -> Path:
        return self.passive_liveness_model or self.models_dir / "antispoof.onnx"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
