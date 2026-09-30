from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Run outside the repository so a developer's .env cannot satisfy a
    field the test means to be unset. Without this, RETRIV_DOCLING_ARTIFACTS_PATH
    in .env made the worker check pass when it should have failed."""
    monkeypatch.chdir(tmp_path)
    yield

# The values a production deployment must be able to pass the validators.
# The RS256 scope keypair and the two service tokens are gone: there is no
# second process to authenticate to, so those settings no longer exist.
PRODUCTION_BASE: dict[str, Any] = {
    "environment": "production",
    "jwt_secret": SecretStr("g" * 64),
    "database_url": "postgresql+asyncpg://retriv:secure@postgres:5432/retriv",
    "s3_secret_key": SecretStr("production-storage-secret-xxxxxxxxxxxxxxxxxxxx"),
    "s3_endpoint_url": "https://objects.example.test",
    "s3_artifact_secret_key": SecretStr("production-artifact-secret-xxxxxxxxxxxxxxx"),
    "qdrant_read_only_api_key": SecretStr("production-qdrant-ro-key-xxxxxxxxxxxxxx"),
    "qdrant_url": "https://qdrant.example.test",
    "embedding_base_url": "https://embeddings.example.test/v1",
    "embedding_api_key": SecretStr("production-embedding-key-xxxxxxxxxxxxxxxx"),
    "chat_api_key": SecretStr("production-chat-api-key-xxxxxxxxxxxxxxxxxxxxx"),
    "chat_base_url": "https://openrouter.ai/api/v1",
    "chat_model": "provider/model",
    "cors_allowed_origins": ["https://dashboard.example.test"],
}

WORKER_BASE: dict[str, Any] = {
    "environment": "production",
    "component": "worker",
    "database_url": "postgresql+asyncpg://retriv:secure@postgres:5432/retriv",
    "s3_endpoint_url": "https://objects.example.test",
    "s3_artifact_secret_key": SecretStr("production-artifact-secret-xxxxxxxxxxxxxxx"),
    "qdrant_url": "https://qdrant.example.test",
    "qdrant_api_key": SecretStr("production-qdrant-rw-key-xxxxxxxxxxxxxxxx"),
    "embedding_base_url": "https://embeddings.example.test/v1",
    "embedding_api_key": SecretStr("production-embedding-key-xxxxxxxxxxxxxxxx"),
    "docling_artifacts_path": Path("/opt/docling-models"),
}


def test_default_jwt_secret_is_generated_per_settings_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RETRIV_JWT_SECRET", raising=False)
    first = Settings(environment="local")
    second = Settings(environment="local")

    assert first.jwt_secret.get_secret_value() != second.jwt_secret.get_secret_value()


def test_production_rejects_placeholder_jwt_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **{
                **PRODUCTION_BASE,
                "jwt_secret": SecretStr("replace-me-with-a-generated-secret-1234"),
            }
        )


def test_production_requires_a_long_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="at least 64"):
        Settings(**{**PRODUCTION_BASE, "jwt_secret": SecretStr("g" * 40)})


def test_production_requires_antivirus() -> None:
    with pytest.raises(ValidationError, match="Antivirus"):
        Settings(**PRODUCTION_BASE, antivirus_enabled=False)


def test_cors_origins_must_be_exact_origins() -> None:
    with pytest.raises(ValidationError, match="exact HTTP"):
        Settings(cors_allowed_origins=["*"])

    with pytest.raises(ValidationError, match="exact HTTP"):
        Settings(cors_allowed_origins=["https://dashboard.example.test/path"])


def test_production_cors_origins_require_https() -> None:
    with pytest.raises(ValidationError, match="CORS origins"):
        Settings(
            **{
                **PRODUCTION_BASE,
                "cors_allowed_origins": ["http://dashboard.example.test"],
            }
        )


def test_production_rejects_default_database_credentials() -> None:
    with pytest.raises(ValidationError, match="database credentials"):
        Settings(
            **{
                **PRODUCTION_BASE,
                "database_url": "postgresql+asyncpg://retriv:retriv@postgres:5432/retriv",
            }
        )


def test_production_rejects_default_service_credentials() -> None:
    with pytest.raises(ValidationError, match="placeholders"):
        Settings(
            **{
                **PRODUCTION_BASE,
                "chat_api_key": SecretStr("change-this-openrouter-key-do-not-use"),
            }
        )


def test_production_requires_https_everywhere() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**{**PRODUCTION_BASE, "s3_endpoint_url": "http://objects.example.test"})

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**{**PRODUCTION_BASE, "qdrant_url": "http://qdrant.example.test"})

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**{**PRODUCTION_BASE, "embedding_base_url": "http://embeddings.example.test"})


def test_production_worker_requires_baked_docling_artifacts() -> None:
    with pytest.raises(ValidationError, match="Docling artifacts"):
        Settings(
            **{k: v for k, v in WORKER_BASE.items() if k != "docling_artifacts_path"}
        )


def test_worker_accepts_a_fully_configured_production_environment() -> None:
    settings = Settings(**WORKER_BASE)
    assert settings.component == "worker"
    # The worker holds no JWT secret and no chat key. There is no user to
    # authenticate and no conversation to answer.
    assert not hasattr(settings, "jwt_secret") or settings.jwt_secret is not None


def test_local_environment_tolerates_placeholders() -> None:
    """A developer must be able to boot without a real provider key. The
    placeholder is long enough to pass the length floor and contains
    'change-this' so production still refuses it."""
    settings = Settings(
        environment="local",
        chat_api_key=SecretStr("change-this-openrouter-key-do-not-use-in-production"),
        chat_model="change-this/change-this",
    )
    assert settings.chat_model == "change-this/change-this"


def test_chat_model_must_be_a_provider_qualified_id() -> None:
    with pytest.raises(ValidationError, match="OpenRouter model ID"):
        Settings(chat_model="no-slash-here")


def test_raw_and_artifact_buckets_must_differ() -> None:
    with pytest.raises(ValidationError, match="buckets must be different"):
        Settings(
            s3_raw_bucket="same-bucket", s3_artifact_bucket="same-bucket"
        )


def test_retry_ceiling_must_not_undercut_the_base() -> None:
    with pytest.raises(ValidationError, match="RETRY_MAX_SECONDS"):
        Settings(
            processing_retry_base_seconds=10.0,
            processing_retry_max_seconds=5.0,
        )


def test_secrets_shorter_than_the_floor_are_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(chat_api_key=SecretStr("too-short"))


def test_service_urls_reject_embedded_credentials() -> None:
    with pytest.raises(ValidationError, match="absolute HTTP"):
        Settings(qdrant_url="qdrant:6333")

    with pytest.raises(ValidationError, match="credentials or query"):
        Settings(embedding_base_url="https://user:pass@embeddings.example.test/v1")


def test_identifiers_reject_whitespace_and_stray_characters() -> None:
    with pytest.raises(ValidationError):
        Settings(embedding_profile="has space")

    with pytest.raises(ValidationError):
        Settings(qdrant_collection="bad/name")
