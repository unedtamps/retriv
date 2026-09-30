import hmac
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.embeddings.base import EmbeddingProvider, VectorStore
from app.embeddings.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.embeddings.qdrant import QdrantVectorStore

internal_bearer = HTTPBearer(auto_error=False)


async def get_embedding_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[EmbeddingProvider]:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.embedding_timeout_seconds,
    )
    try:
        yield provider
    finally:
        await provider.aclose()


async def get_vector_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[VectorStore]:
    store = QdrantVectorStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value(),
        collection=settings.qdrant_collection,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.qdrant_timeout_seconds,
        vectors_on_disk=settings.qdrant_vectors_on_disk,
    )
    try:
        yield store
    finally:
        await store.aclose()


async def require_internal_api_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(internal_bearer)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(
            credentials.credentials, settings.api_token.get_secret_value()
        )
    ):
        raise AppError(
            status_code=401,
            code="invalid_service_credentials",
            title="Service authentication failed",
            detail="Valid internal service credentials are required.",
        )
