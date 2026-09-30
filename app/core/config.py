import re
from functools import lru_cache
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment settings for the whole application.

    One process serves the API, the ingestion endpoints, and the answer
    harness, so there is one settings object and one RETRIV_ prefix. The
    worker is the only separate process and reads this same class with
    component="worker".
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="RETRIV_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Retriv"
    component: Literal["api", "worker"] = "api"
    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+asyncpg://retriv:retriv@localhost:5432/retriv"

    # -- authentication -------------------------------------------------
    jwt_secret: SecretStr = Field(default_factory=lambda: SecretStr(token_urlsafe(48)))
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "retriv"
    jwt_audience: str = "retriv"
    access_token_minutes: int = Field(default=60, ge=1, le=1440)
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_window_seconds: int = Field(default=300, ge=1, le=3600)
    max_password_bytes: int = Field(default=1024, ge=64, le=8192)
    cors_allowed_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

    # -- object storage -------------------------------------------------
    # Two object-store identities, not one. The API only ever touches
    # raw documents; the worker reads those and writes extracted
    # artifacts. Keeping the credentials separate means a compromise in
    # either component does not grant the other's write access.
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "retriv"
    s3_secret_key: SecretStr = SecretStr("development-minio-secret-placeholder")
    s3_artifact_access_key: str = "retriv-worker"
    s3_artifact_secret_key: SecretStr = SecretStr("development-minio-worker-secret-placeholder")
    s3_raw_bucket: str = "raw-documents"
    s3_artifact_bucket: str = "context-documents"
    s3_region: str = "us-east-1"

    # -- virus scanning -------------------------------------------------
    antivirus_enabled: bool = True
    clamd_host: str = "localhost"
    clamd_port: int = Field(default=3310, ge=1, le=65535)
    clamd_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    # -- upload validation ----------------------------------------------
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

    # -- extraction pipeline limits -------------------------------------
    max_source_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_chunks_per_document: int = Field(default=10_000, gt=0)
    max_artifact_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_extracted_text_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_chunk_payload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    pipeline_version: str = "2026.1"
    ocr_languages: list[str] = ["ind", "eng"]
    chunk_max_tokens: int = Field(default=700, ge=100, le=8192)
    chunk_tokenizer: str = "cl100k_base"
    docling_artifacts_path: Path | None = None

    # -- worker scheduling ----------------------------------------------
    processing_timeout_seconds: int = Field(default=1800, ge=30, le=86_400)
    parser_memory_limit_bytes: int = Field(
        default=8 * 1024 * 1024 * 1024, ge=512 * 1024 * 1024
    )
    parser_max_processes: int = Field(default=4096, ge=16, le=32_768)
    processing_temp_dir: Path = Path("/tmp")  # noqa: S108 - container-scoped tmpfs
    processing_poll_interval_seconds: float = Field(default=2.0, gt=0, le=3600)
    artifact_reconciliation_interval_seconds: int = Field(
        default=300, ge=1, le=86_400
    )
    processing_lease_seconds: int = Field(default=1800, ge=30, le=86_400)
    processing_max_attempts: int = Field(default=3, ge=1, le=100)
    processing_retry_base_seconds: float = Field(default=5.0, gt=0, le=3600)
    processing_retry_max_seconds: float = Field(default=300.0, gt=0, le=86_400)

    # -- embeddings -----------------------------------------------------
    embedding_base_url: str = "http://127.0.0.1:1234/v1"
    embedding_api_key: SecretStr = SecretStr("development-embedding-api-key-change-this")
    embedding_model: str = "text-embedding-qwen3-embedding-8b"
    embedding_profile: str = "qwen3-embedding-8b-4096-v1"
    embedding_dimensions: int = Field(default=4096, ge=1, le=65_536)
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    embedding_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    embedding_reconciliation_interval_seconds: int = Field(
        default=300, ge=1, le=86_400
    )

    # -- vector store ---------------------------------------------------
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr = SecretStr("development-qdrant-api-key-change-this")
    qdrant_read_only_api_key: SecretStr = SecretStr(
        "development-qdrant-read-only-key-change-this"
    )
    qdrant_collection: str = "retriv-chunks-qwen3-8b-4096-v1"
    qdrant_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    qdrant_vectors_on_disk: bool = True

    # -- answer harness -------------------------------------------------
    chat_base_url: str = "https://openrouter.ai/api/v1"
    chat_api_key: SecretStr = SecretStr("development-chat-api-key-change-this")
    chat_model: str = "replace-with/chat-model"
    chat_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    # A turn is held as "processing" until it completes; the conversation
    # route treats a turn older than this as abandoned and lets the caller
    # retry. It has to outlast the model call, not bound it.
    answer_stale_after_seconds: int = Field(default=660, ge=60, le=7200)
    max_context_chunks: int = Field(default=8, ge=1, le=20)
    min_relevance_score: float = Field(default=0.25, ge=-1.0, le=1.0)

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("RETRIV_JWT_SECRET must be at least 32 characters")
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

    @field_validator("embedding_base_url", "qdrant_url", "chat_base_url")
    @classmethod
    def validate_service_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Service URLs must be absolute HTTP(S) URLs")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Service URLs must not contain credentials or query data")
        return value.rstrip("/")

    @field_validator("chat_model")
    @classmethod
    def validate_chat_model(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("RETRIV_CHAT_MODEL must be a non-empty model identifier")
        if "/" not in value:
            raise ValueError(
                "RETRIV_CHAT_MODEL must be an OpenRouter model ID such as provider/model"
            )
        return value

    @field_validator(
        "s3_secret_key",
        "s3_artifact_secret_key",
        "embedding_api_key",
        "qdrant_api_key",
        "qdrant_read_only_api_key",
        "chat_api_key",
    )
    @classmethod
    def validate_secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("Service secrets must be at least 32 characters")
        return value

    @field_validator(
        "embedding_model",
        "embedding_profile",
        "qdrant_collection",
        "pipeline_version",
    )
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("Identifiers must be non-empty and free of whitespace")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value):
            raise ValueError(
                "Identifiers must contain only letters, numbers, '.', '_', or '-'"
            )
        return value

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if self.s3_raw_bucket == self.s3_artifact_bucket:
            raise ValueError("Raw and artifact buckets must be different")
        if self.processing_retry_max_seconds < self.processing_retry_base_seconds:
            raise ValueError(
                "RETRIV_PROCESSING_RETRY_MAX_SECONDS must be >= retry base seconds"
            )
        if self.max_artifact_bytes > self.max_chunk_payload_bytes:
            raise ValueError(
                "RETRIV_MAX_ARTIFACT_BYTES must not exceed RETRIV_MAX_CHUNK_PAYLOAD_BYTES"
            )

        if self.environment != "production":
            return self

        # The API holds the JWT secret and the upload identity; the worker
        # holds the artifact identity. Each component only validates the
        # secrets it can actually see.
        secrets: tuple[str, ...]
        if self.component == "api":
            secrets = (
                self.jwt_secret.get_secret_value(),
                self.chat_api_key.get_secret_value(),
                self.qdrant_read_only_api_key.get_secret_value(),
                self.s3_secret_key.get_secret_value(),
            )
        else:
            secrets = (
                self.s3_artifact_secret_key.get_secret_value(),
                self.embedding_api_key.get_secret_value(),
                self.qdrant_api_key.get_secret_value(),
            )
        if any(
            marker in secret.lower()
            for secret in secrets
            for marker in ("change-this", "development", "replace", "replace-me")
        ):
            raise ValueError("Production secrets must not use development placeholders")

        if any(
            marker in self.database_url.lower()
            for marker in ("change-this", "development", "replace-me")
        ):
            raise ValueError("Production database credentials must be configured")
        database = urlsplit(self.database_url)
        if not database.password or database.password == database.username:
            raise ValueError("Production database credentials must not use defaults")

        if not self.s3_endpoint_url.startswith("https://"):
            raise ValueError("Production object storage must use HTTPS")
        if any(
            urlsplit(url).scheme != "https"
            for url in (self.embedding_base_url, self.qdrant_url, self.chat_base_url)
        ):
            raise ValueError("Production embedding, Qdrant, and chat endpoints must use HTTPS")
        if self.component == "worker" and self.docling_artifacts_path is None:
            raise ValueError("Production workers require baked Docling artifacts")
        if self.component == "api" and not self.antivirus_enabled:
            raise ValueError("Antivirus scanning cannot be disabled in production")
        if self.component == "api" and any(
            not origin.startswith("https://") for origin in self.cors_allowed_origins
        ):
            raise ValueError("Production CORS origins must use HTTPS")
        if self.component == "api" and len(self.jwt_secret.get_secret_value()) < 64:
            raise ValueError("RETRIV_JWT_SECRET must be at least 64 characters in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
