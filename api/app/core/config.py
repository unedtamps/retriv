import base64
from functools import lru_cache
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="APP_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Enterprise AI Ingestion"
    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+asyncpg://ingestion:ingestion@localhost:5432/ingestion"

    jwt_secret: SecretStr = Field(default_factory=lambda: SecretStr(token_urlsafe(48)))
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "enterprise-ai-ingestion"
    jwt_audience: str = "enterprise-ai-ingestion-api"
    access_token_minutes: int = Field(default=60, ge=1, le=1440)
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_window_seconds: int = Field(default=300, ge=1, le=3600)
    max_password_bytes: int = Field(default=1024, ge=64, le=8192)
    cors_allowed_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "ingestion-api"
    s3_secret_key: SecretStr = SecretStr("development-minio-secret")
    s3_bucket: str = "raw-documents"
    s3_region: str = "us-east-1"

    antivirus_enabled: bool = True
    clamd_host: str = "localhost"
    clamd_port: int = Field(default=3310, ge=1, le=65535)
    clamd_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_pdf_pages: int = Field(default=500, gt=0)
    max_archive_entries: int = Field(default=10_000, gt=0)
    max_archive_central_directory_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_archive_uncompressed_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_archive_entry_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_archive_compression_ratio: float = Field(default=100.0, gt=1)
    max_archive_entry_compression_ratio: float = Field(default=100.0, gt=1)
    max_office_xml_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_concurrent_uploads: int = Field(default=2, ge=1, le=16)
    request_read_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    upload_temp_dir: Path = Path("/tmp")  # noqa: S108 - container-scoped tmpfs

    agent_base_url: str = "http://127.0.0.1:8090"
    agent_api_token: SecretStr = SecretStr("development-agent-api-token-change-this")
    agent_scope_private_key_b64: SecretStr
    agent_scope_ttl_seconds: int = Field(default=900, ge=30, le=900)
    agent_timeout_seconds: float = Field(default=600.0, gt=0, le=600)
    processing_api_base_url: str = "http://127.0.0.1:8082"
    processing_api_token: SecretStr = SecretStr(
        "development-internal-api-token-change-this"
    )
    processing_api_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("JWT secret must be at least 32 characters")
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_allowed_origins(cls, values: list[str]) -> list[str]:
        normalized_origins: list[str] = []
        for value in values:
            origin = value.strip().rstrip("/")
            parsed = urlsplit(origin)
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError("CORS origins must be exact HTTP(S) origins")
            if origin not in normalized_origins:
                normalized_origins.append(origin)
        return normalized_origins

    @field_validator("agent_base_url", "processing_api_base_url")
    @classmethod
    def validate_service_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Service URLs must be absolute HTTP(S) URLs")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Service URLs must not contain credentials or query data")
        return value.rstrip("/")

    @field_validator("agent_api_token", "processing_api_token")
    @classmethod
    def validate_service_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("Service secrets must be at least 32 characters")
        return value

    @field_validator("agent_scope_private_key_b64")
    @classmethod
    def validate_agent_scope_private_key(cls, value: SecretStr) -> SecretStr:
        try:
            key = load_pem_private_key(
                base64.b64decode(value.get_secret_value(), validate=True),
                password=None,
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "APP_AGENT_SCOPE_PRIVATE_KEY_B64 must be a base64-encoded PEM RSA key"
            ) from exc
        if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("The agent scope signing key must be RSA with at least 2048 bits")
        return value

    @model_validator(mode="after")
    def reject_development_secrets_in_production(self) -> "Settings":
        secret = self.jwt_secret.get_secret_value()
        if self.environment == "production" and (
            "change-this" in secret
            or "replace-me" in secret
            or "development-only" in secret
            or len(secret) < 64
        ):
            raise ValueError("APP_JWT_SECRET must be a generated secret in production")
        if self.environment == "production" and (
            any(marker in self.database_url.lower() for marker in ("change-this", "replace-me"))
            or any(
                marker in self.s3_secret_key.get_secret_value().lower()
                for marker in ("change-this", "replace-me", "development-minio-secret")
            )
            or any(
                marker in secret.get_secret_value().lower()
                for secret in (
                    self.agent_api_token,
                    self.agent_scope_private_key_b64,
                    self.processing_api_token,
                )
                for marker in ("change-this", "replace-me", "replace-with", "development")
            )
        ):
            raise ValueError("Production database and object-store credentials must be configured")
        if self.environment == "production":
            database = urlsplit(self.database_url)
            if not database.password or database.password == database.username:
                raise ValueError("Production database credentials must not use defaults")
        if self.environment == "production" and not self.s3_endpoint_url.startswith("https://"):
            raise ValueError("Production object storage must use HTTPS")
        if self.environment == "production" and (
            urlsplit(self.agent_base_url).scheme != "https"
        ):
            raise ValueError("The production agent endpoint must use HTTPS")
        if self.environment == "production" and (
            urlsplit(self.processing_api_base_url).scheme != "https"
        ):
            raise ValueError("The production processing API endpoint must use HTTPS")
        if self.environment == "production" and not self.antivirus_enabled:
            raise ValueError("Antivirus scanning cannot be disabled in production")
        if self.environment == "production" and any(
            not origin.startswith("https://") for origin in self.cors_allowed_origins
        ):
            raise ValueError("Production CORS origins must use HTTPS")
        return self


@lru_cache
def get_settings() -> Settings:
    # Required deployment settings are loaded from the environment or .env.
    return Settings()  # type: ignore[call-arg]
