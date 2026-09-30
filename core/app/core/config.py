import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="INTERNAL_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Kerjakan Internal"
    component: Literal["api", "worker"] = "api"
    environment: Literal["local", "test", "production"] = "local"
    database_url: str = (
        "postgresql+asyncpg://kerjakan_internal:kerjakan_internal@localhost:5433/kerjakan_internal"
    )

    webhook_key_id: str = "local-primary"
    webhook_secret: SecretStr = SecretStr("development-webhook-secret-change-this")
    webhook_previous_key_id: str | None = None
    webhook_previous_secret: SecretStr | None = None
    webhook_max_age_seconds: int = Field(default=300, ge=1, le=3600)
    expected_event_source: str = "enterprise-ai-ingestion"
    api_token: SecretStr = SecretStr("development-internal-api-token-change-this")

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "context-service"
    s3_secret_key: SecretStr = SecretStr("development-minio-internal-secret")
    s3_raw_bucket: str = "raw-documents"
    s3_artifact_bucket: str = "context-documents"
    s3_region: str = "us-east-1"

    max_source_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_chunks_per_document: int = Field(default=10_000, gt=0)
    max_artifact_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_extracted_text_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_chunk_payload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    processing_timeout_seconds: int = Field(default=1800, ge=30, le=86_400)
    parser_memory_limit_bytes: int = Field(default=8 * 1024 * 1024 * 1024, ge=512 * 1024 * 1024)
    parser_max_processes: int = Field(default=4096, ge=16, le=32_768)
    processing_temp_dir: Path = Path("/tmp")  # noqa: S108 - container-scoped tmpfs
    processing_poll_interval_seconds: float = Field(default=2.0, gt=0, le=3600)
    artifact_reconciliation_interval_seconds: int = Field(default=300, ge=1, le=86_400)
    processing_lease_seconds: int = Field(default=1800, ge=30, le=86_400)
    processing_max_attempts: int = Field(default=3, ge=1, le=100)
    processing_retry_base_seconds: float = Field(default=5.0, gt=0, le=3600)
    processing_retry_max_seconds: float = Field(default=300.0, gt=0, le=86_400)

    embedding_base_url: str = "http://127.0.0.1:1234/v1"
    embedding_api_key: SecretStr = SecretStr(
        "development-embedding-api-key-change-this"
    )
    embedding_model: str = "text-embedding-qwen3-embedding-8b"
    embedding_profile: str = "qwen3-embedding-8b-4096-v1"
    embedding_dimensions: int = Field(default=4096, ge=1, le=65_536)
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    embedding_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    embedding_reconciliation_interval_seconds: int = Field(
        default=300, ge=1, le=86_400
    )

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr = SecretStr("development-qdrant-api-key-change-this")
    qdrant_collection: str = "kerjakan-chunks-qwen3-8b-4096-v1"
    qdrant_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    qdrant_vectors_on_disk: bool = True

    pipeline_version: str = "2026.1"
    docling_artifacts_path: Path | None = None
    ocr_languages: list[str] = ["ind", "eng"]
    chunk_max_tokens: int = Field(default=700, ge=100, le=8192)
    chunk_tokenizer: str = "cl100k_base"

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("INTERNAL_WEBHOOK_SECRET must be at least 32 characters")
        return value

    @field_validator("api_token")
    @classmethod
    def validate_api_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("INTERNAL_API_TOKEN must be at least 32 characters")
        return value

    @field_validator("embedding_base_url", "qdrant_url")
    @classmethod
    def validate_service_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Service URLs must be absolute HTTP(S) URLs")
        if parsed.username or parsed.password:
            raise ValueError("Service credentials must not be embedded in URLs")
        if parsed.query or parsed.fragment:
            raise ValueError("Service URLs must not contain query strings or fragments")
        return value.rstrip("/")

    @field_validator("embedding_model")
    @classmethod
    def validate_embedding_model(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("The embedding model must be a non-empty model identifier")
        return value

    @field_validator("embedding_profile", "qdrant_collection")
    @classmethod
    def validate_embedding_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value):
            raise ValueError(
                "Embedding identifiers must contain only letters, numbers, '.', '_', or '-'"
            )
        return value

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if bool(self.webhook_previous_key_id) != bool(self.webhook_previous_secret):
            raise ValueError(
                "INTERNAL_WEBHOOK_PREVIOUS_KEY_ID and "
                "INTERNAL_WEBHOOK_PREVIOUS_SECRET must be configured together"
            )
        if self.webhook_previous_secret is not None:
            if len(self.webhook_previous_secret.get_secret_value()) < 32:
                raise ValueError(
                    "INTERNAL_WEBHOOK_PREVIOUS_SECRET must be at least 32 characters"
                )
            if self.webhook_previous_key_id == self.webhook_key_id:
                raise ValueError("Webhook key IDs must be distinct")
        if self.s3_raw_bucket == self.s3_artifact_bucket:
            raise ValueError("Raw and artifact buckets must be different")
        if self.processing_retry_max_seconds < self.processing_retry_base_seconds:
            raise ValueError(
                "INTERNAL_PROCESSING_RETRY_MAX_SECONDS must be >= retry base seconds"
            )
        if self.environment == "production":
            secrets: tuple[str, ...] = (
                (
                    self.webhook_secret.get_secret_value(),
                    self.api_token.get_secret_value(),
                )
                if self.component == "api"
                else (
                    self.s3_secret_key.get_secret_value(),
                    self.embedding_api_key.get_secret_value(),
                    self.qdrant_api_key.get_secret_value(),
                )
            )
            if self.component == "api":
                secrets += (
                    self.embedding_api_key.get_secret_value(),
                    self.qdrant_api_key.get_secret_value(),
                )
            if any(
                marker in secret.lower()
                for secret in secrets
                for marker in ("change-this", "development", "replace")
            ):
                raise ValueError("Production secrets must not use development placeholders")
            if any(
                marker in self.database_url.lower()
                for marker in ("change-this", "development", "replace-me")
            ):
                raise ValueError("Production database credentials must be configured")
            parsed_database_url = urlsplit(self.database_url)
            if (
                not parsed_database_url.password
                or parsed_database_url.password == parsed_database_url.username
            ):
                raise ValueError("Production database credentials must not use defaults")
            if self.component == "worker" and urlsplit(self.s3_endpoint_url).scheme != "https":
                raise ValueError("Production object storage must use HTTPS")
            if urlsplit(self.embedding_base_url).scheme != "https":
                raise ValueError("Production embedding services must use HTTPS")
            if urlsplit(self.qdrant_url).scheme != "https":
                raise ValueError("Production Qdrant must use HTTPS")
            if self.component == "worker" and self.docling_artifacts_path is None:
                raise ValueError("Production workers require baked Docling artifacts")
        return self

    def webhook_secrets(self) -> dict[str, str]:
        secrets = {self.webhook_key_id: self.webhook_secret.get_secret_value()}
        if self.webhook_previous_key_id and self.webhook_previous_secret:
            secrets[self.webhook_previous_key_id] = (
                self.webhook_previous_secret.get_secret_value()
            )
        return secrets


@lru_cache
def get_settings() -> Settings:
    return Settings()
