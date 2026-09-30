import base64
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AGENT_",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["local", "test", "production"]
    api_token: SecretStr = SecretStr("development-agent-api-token-change-this")
    scope_public_key_b64: SecretStr
    scope_issuer: str = "enterprise-ai-ingestion"

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://kerjakan_agent_readonly:change-this"
        "@127.0.0.1:5433/kerjakan_internal"
    )

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr = SecretStr("replace-with-qdrant-read-only-key")
    qdrant_collection: str = "kerjakan-chunks-qwen3-8b-4096-v1"
    qdrant_timeout_seconds: float = Field(default=20.0, gt=0, le=180)

    embedding_base_url: str = "http://127.0.0.1:1234/v1"
    embedding_api_key: SecretStr = SecretStr("replace-with-embedding-api-key")
    embedding_model: str = "text-embedding-qwen3-embedding-8b"
    embedding_profile: str = "qwen3-embedding-8b-4096-v1"
    embedding_dimensions: int = Field(default=4096, ge=1, le=65_536)
    embedding_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    min_relevance_score: float = Field(default=0.25, ge=-1.0, le=1.0)

    chat_base_url: str = "https://openrouter.ai/api/v1"
    chat_api_key: SecretStr = SecretStr("development-chat-api-key-change-this")
    chat_model: str = "replace-with-chat-model"
    chat_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    max_context_chunks: int = Field(default=8, ge=1, le=20)

    @field_validator("qdrant_url", "embedding_base_url", "chat_base_url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Agent service URLs must be absolute HTTP(S) URLs")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Agent service URLs cannot contain credentials or query data")
        return value.rstrip("/")

    @field_validator("api_token", "qdrant_api_key", "embedding_api_key", "chat_api_key")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("Agent service secrets must be at least 32 characters")
        return value

    @field_validator("scope_public_key_b64")
    @classmethod
    def validate_scope_public_key(cls, value: SecretStr) -> SecretStr:
        try:
            key = load_pem_public_key(
                base64.b64decode(value.get_secret_value(), validate=True)
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "AGENT_SCOPE_PUBLIC_KEY_B64 must be a base64-encoded PEM RSA key"
            ) from exc
        if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
            raise ValueError("The agent scope verification key must be RSA with at least 2048 bits")
        return value

    @field_validator("embedding_model", "embedding_profile", "qdrant_collection")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("Embedding and collection identifiers must be non-empty")
        return value

    @field_validator("chat_model")
    @classmethod
    def validate_chat_model(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("AGENT_CHAT_MODEL must be a non-empty model identifier")
        if "/" not in value:
            raise ValueError(
                "AGENT_CHAT_MODEL must be an OpenRouter model ID such as provider/model"
            )
        return value

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.environment == "production":
            secrets = (
                self.api_token.get_secret_value(),
                self.qdrant_api_key.get_secret_value(),
                self.embedding_api_key.get_secret_value(),
                self.chat_api_key.get_secret_value(),
                self.scope_public_key_b64.get_secret_value(),
            )
            if any(
                marker in secret.lower()
                for secret in secrets
                for marker in ("development", "change-this", "replace")
            ):
                raise ValueError("Production agent credentials must be configured")
            if any(
                marker in self.chat_model.lower()
                for marker in ("development", "change-this", "replace")
            ):
                raise ValueError("Production chat model must be configured")
            database_url = self.database_url.get_secret_value()
            if not database_url.startswith("postgresql+asyncpg://"):
                raise ValueError("Production agent database must use PostgreSQL with asyncpg")
            database = urlsplit(database_url)
            if {
                key.lower() for key, _ in parse_qsl(database.query)
            } & {"ssl", "sslmode", "sslrootcert", "sslcert", "sslkey"}:
                raise ValueError(
                    "Configure production PostgreSQL TLS through the verified agent SSL context"
                )
            if not database.password or database.password == database.username:
                raise ValueError("Production agent database credentials must not use defaults")
            database_password = unquote(database.password).lower()
            if any(
                marker in database_password
                for marker in ("change-this", "replace", "development")
            ):
                raise ValueError("Production agent database password must be generated")
            if any(
                urlsplit(url).scheme != "https"
                for url in (self.qdrant_url, self.embedding_base_url, self.chat_base_url)
            ):
                raise ValueError("Production Qdrant and model endpoints must use HTTPS")
        return self


@lru_cache
def get_settings() -> Settings:
    # Required deployment settings are loaded from the environment or .env.
    return Settings()  # type: ignore[call-arg]
