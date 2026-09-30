import base64
import ssl

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from agent.config import Settings
from agent.database import database_connect_args


def public_key_b64() -> SecretStr:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return SecretStr(base64.b64encode(public_key).decode("ascii"))


def production_settings(database_password: str) -> Settings:
    return Settings(
        environment="production",
        api_token=SecretStr("production-agent-api-token-0123456789"),
        scope_public_key_b64=public_key_b64(),
        database_url=SecretStr(
            "postgresql+asyncpg://kerjakan_agent_readonly:"
            f"{database_password}@postgres.example.test:5432/kerjakan_internal"
        ),
        qdrant_url="https://qdrant.example.test",
        qdrant_api_key=SecretStr("production-qdrant-readonly-key-0123456789"),
        embedding_base_url="https://embedding.example.test/v1",
        embedding_api_key=SecretStr("production-embedding-api-key-0123456789"),
        chat_base_url="https://chat.example.test/v1",
        chat_api_key=SecretStr("production-chat-api-key-0123456789"),
        chat_model="openai/production-chat-model",
    )


def test_production_rejects_placeholder_database_password() -> None:
    with pytest.raises(ValidationError, match="password must be generated"):
        production_settings("change-this")


def test_production_rejects_database_url_ssl_overrides() -> None:
    values = production_settings("generated-db-secret-123456").model_dump()
    values["database_url"] = SecretStr(
        "postgresql+asyncpg://kerjakan_agent_readonly:generated-db-secret-123456"
        "@postgres.example.test:5432/kerjakan_internal?sslmode=disable"
    )

    with pytest.raises(ValidationError, match="TLS through the verified"):
        Settings.model_validate(values)


def test_production_database_connections_require_verified_tls() -> None:
    connect_args = database_connect_args(production_settings("generated-db-secret-123456"))
    tls = connect_args["ssl"]

    assert isinstance(tls, ssl.SSLContext)
    assert tls.verify_mode == ssl.CERT_REQUIRED
    assert tls.check_hostname is True


def test_local_database_connection_does_not_require_tls() -> None:
    configuration = Settings(
        environment="test",
        api_token=SecretStr("agent-api-token-for-tests-0123456789"),
        scope_public_key_b64=public_key_b64(),
        qdrant_api_key=SecretStr("qdrant-readonly-key-for-tests-0123456789"),
        embedding_api_key=SecretStr("embedding-api-key-for-tests-0123456789"),
        chat_api_key=SecretStr("chat-api-key-for-tests-0123456789"),
    )

    assert database_connect_args(configuration) == {}


def test_environment_must_use_a_known_mode() -> None:
    values = production_settings("generated-db-secret-123456").model_dump()
    values["environment"] = "prod"

    with pytest.raises(ValidationError, match="environment"):
        Settings.model_validate(values)
