from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OutboxSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="OUTBOX_",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = "local"
    database_url: str = ""
    target_url: str = ""
    signing_secret: SecretStr = Field(
        default=SecretStr(""), min_length=32, validate_default=True
    )
    signing_key_id: str = Field(
        default="", min_length=1, max_length=255, validate_default=True
    )
    poll_interval_seconds: float = Field(default=2.0, gt=0, le=3600)
    batch_size: int = Field(default=20, gt=0, le=1000)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    max_attempts: int = Field(default=10, ge=1, le=100)
    lease_seconds: int = Field(default=60, gt=0, le=3600)
    retry_base_seconds: float = Field(default=2.0, gt=0, le=3600)
    retry_max_seconds: float = Field(default=300.0, gt=0, le=86400)
    retention_days: int = Field(default=7, ge=1, le=3650)

    @field_validator("signing_key_id")
    @classmethod
    def validate_signing_key_id(cls, value: str) -> str:
        key_id = value.strip()
        if not key_id:
            raise ValueError("OUTBOX_SIGNING_KEY_ID must not be blank")
        return key_id

    @model_validator(mode="after")
    def validate_required_values(self) -> "OutboxSettings":
        if not self.database_url.strip():
            raise ValueError("OUTBOX_DATABASE_URL is required")
        target_url = self.target_url.strip()
        parsed = urlsplit(target_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OUTBOX_TARGET_URL must be an HTTP or HTTPS URL")
        if self.environment == "production" and parsed.scheme != "https":
            raise ValueError("OUTBOX_TARGET_URL must use HTTPS in production")
        if self.environment == "production":
            signing_secret = self.signing_secret.get_secret_value().lower()
            if any(
                marker in signing_secret
                for marker in ("change-this", "development", "replace-me")
            ):
                raise ValueError("OUTBOX_SIGNING_SECRET must be configured in production")
            database = urlsplit(self.database_url)
            if (
                not database.password
                or database.password == database.username
                or any(
                    marker in self.database_url.lower()
                    for marker in ("change-this", "development", "replace-me")
                )
            ):
                raise ValueError("OUTBOX_DATABASE_URL credentials must be configured")
        self.target_url = target_url
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("OUTBOX_RETRY_MAX_SECONDS must be >= OUTBOX_RETRY_BASE_SECONDS")
        if self.request_timeout_seconds >= self.lease_seconds:
            raise ValueError("OUTBOX_REQUEST_TIMEOUT_SECONDS must be shorter than the lease")
        return self
