from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.main import create_app


def production_database_url(password: str = "strong-database-password") -> str:
    return f"postgresql+asyncpg://context:{password}@postgres:5432/context"


def production_worker_settings() -> dict[str, Any]:
    return {
        "component": "worker",
        "environment": "production",
        "database_url": production_database_url(),
        "s3_endpoint_url": "https://objects.example.test",
        "s3_secret_key": SecretStr("production-storage-secret-xxxxxxxxxxxxxxxxxxxx"),
        "embedding_base_url": "https://embeddings.example.test/v1",
        "embedding_api_key": SecretStr(
            "production-embedding-secret-xxxxxxxxxxxxxxxxxx"
        ),
        "qdrant_url": "https://qdrant.example.test",
        "qdrant_api_key": SecretStr("production-qdrant-secret-xxxxxxxxxxxxxxxxxxxx"),
        "docling_artifacts_path": Path("/opt/docling-models"),
    }


def test_production_api_disables_docs_and_does_not_require_worker_credentials() -> None:
    settings = Settings(
        component="api",
        environment="production",
        database_url=production_database_url(),
        webhook_secret=SecretStr("production-webhook-secret-xxxxxxxxxxxxxxxxxxxxxxxx"),
        api_token=SecretStr("production-api-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
        embedding_base_url="https://embeddings.example.test/v1",
        embedding_api_key=SecretStr("production-embedding-secret-xxxxxxxxxxxxxxxxxx"),
        qdrant_url="https://qdrant.example.test",
        qdrant_api_key=SecretStr("production-qdrant-secret-xxxxxxxxxxxxxxxxxxxx"),
    )

    app = create_app(settings)

    assert app.openapi_url is None
    assert app.docs_url is None


def test_production_worker_does_not_require_api_credentials() -> None:
    settings = Settings(**production_worker_settings())

    assert settings.component == "worker"


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://kerjakan_internal:kerjakan_internal@postgres/context",
        "postgresql+asyncpg://context:context@postgres/context",
        "postgresql+asyncpg://context@postgres/context",
    ],
)
def test_production_rejects_default_database_credentials(database_url: str) -> None:
    with pytest.raises(ValidationError, match="database credentials"):
        Settings(
            component="api",
            environment="production",
            database_url=database_url,
            webhook_secret=SecretStr(
                "production-webhook-secret-xxxxxxxxxxxxxxxxxxxxxxxx"
            ),
            api_token=SecretStr("production-api-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            embedding_base_url="https://embeddings.example.test/v1",
            embedding_api_key=SecretStr("production-embedding-secret-xxxxxxxxxxxxxxxxxx"),
            qdrant_url="https://qdrant.example.test",
            qdrant_api_key=SecretStr("production-qdrant-secret-xxxxxxxxxxxxxxxxxxxx"),
        )


def test_production_worker_requires_https_object_storage() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            **{
                **production_worker_settings(),
                "s3_endpoint_url": "http://objects.example.test",
            }
        )


def test_production_worker_requires_https_embedding_service() -> None:
    with pytest.raises(ValidationError, match="embedding services must use HTTPS"):
        Settings(
            **{
                **production_worker_settings(),
                "embedding_base_url": "http://embeddings.example.test/v1",
            }
        )


def test_production_worker_requires_https_qdrant() -> None:
    with pytest.raises(ValidationError, match="Qdrant must use HTTPS"):
        Settings(
            **{
                **production_worker_settings(),
                "qdrant_url": "http://qdrant.example.test",
            }
        )
