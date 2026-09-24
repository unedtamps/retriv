from base64 import b64encode
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def agent_scope_private_key() -> SecretStr:
    scope_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_b64 = b64encode(
        scope_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode("ascii")
    return SecretStr(private_key_b64)


def production_agent_settings() -> dict[str, Any]:
    return {
        "agent_base_url": "https://agent.example.test",
        "agent_api_token": SecretStr("production-agent-api-token-xxxxxxxxxxxxxx"),
        "agent_scope_private_key_b64": agent_scope_private_key(),
        "processing_api_base_url": "https://processing.example.test",
        "processing_api_token": SecretStr(
            "production-processing-api-token-xxxxxxxxxx"
        ),
    }


def test_default_jwt_secret_is_generated_per_settings_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_JWT_SECRET", raising=False)
    first = Settings(
        environment="local", agent_scope_private_key_b64=agent_scope_private_key()
    )
    second = Settings(
        environment="local", agent_scope_private_key_b64=agent_scope_private_key()
    )

    assert first.jwt_secret.get_secret_value() != second.jwt_secret.get_secret_value()


def test_production_rejects_placeholder_jwt_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret=SecretStr("replace-me-with-a-generated-secret-value-123456"),
            agent_scope_private_key_b64=agent_scope_private_key(),
        )


def test_production_requires_antivirus() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret=SecretStr("g" * 64),
            antivirus_enabled=False,
            **production_agent_settings(),
        )


def test_cors_origins_must_be_exact_origins() -> None:
    with pytest.raises(ValidationError, match="exact HTTP"):
        Settings(
            cors_allowed_origins=["*"],
            agent_scope_private_key_b64=agent_scope_private_key(),
        )

    with pytest.raises(ValidationError, match="exact HTTP"):
        Settings(
            cors_allowed_origins=["https://dashboard.example.test/path"],
            agent_scope_private_key_b64=agent_scope_private_key(),
        )


def test_production_cors_origins_require_https() -> None:
    with pytest.raises(ValidationError, match="CORS origins"):
        Settings(
            environment="production",
            jwt_secret=SecretStr("g" * 64),
            database_url="postgresql+asyncpg://ingestion:secure@postgres:5432/ingestion",
            s3_secret_key=SecretStr("production-storage-secret-xxxxxxxxxxxxxxxxxxxx"),
            s3_endpoint_url="https://objects.example.test",
            **production_agent_settings(),
            cors_allowed_origins=["http://dashboard.example.test"],
        )


def test_production_rejects_default_service_credentials() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret=SecretStr("generated-secret-value-that-is-long-enough-1234567890"),
            database_url="postgresql+asyncpg://ingestion:ingestion@postgres:5432/ingestion",
            **production_agent_settings(),
        )


def test_production_rejects_default_database_with_configured_storage() -> None:
    with pytest.raises(ValidationError, match="database credentials"):
        Settings(
            environment="production",
            jwt_secret=SecretStr("g" * 64),
            database_url="postgresql+asyncpg://ingestion:ingestion@postgres:5432/ingestion",
            s3_secret_key=SecretStr("production-storage-secret-xxxxxxxxxxxxxxxxxxxx"),
            s3_endpoint_url="https://objects.example.test",
            **production_agent_settings(),
        )
