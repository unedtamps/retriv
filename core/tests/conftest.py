import hashlib
import json
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_embedding_provider, get_vector_store
from app.contracts.signing import calculate_signature
from app.core.config import Settings, get_settings
from app.db.session import Base, get_session
from app.embeddings.base import VectorMatch, VectorPoint
from app.extraction.base import ExtractedChunk, ExtractionResult
from app.infrastructure.storage import (
    DownloadResult,
    ObjectNotFoundError,
    get_object_storage,
)
from app.main import create_app
from app.processing.worker import ProcessingWorker


class InMemoryObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict[str, str | None]] = {}
        self.download_error: Exception | None = None
        self.upload_error: Exception | None = None
        self.fail_put_suffix: str | None = None
        self.delete_error: Exception | None = None
        self.deleted_keys: list[tuple[str, str]] = []

    def download_file(
        self,
        destination: Path,
        *,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> DownloadResult:
        if self.download_error is not None:
            raise self.download_error
        try:
            data = self.objects[(bucket, key)]
        except KeyError as exc:
            raise ObjectNotFoundError("missing") from exc
        if len(data) > max_bytes:
            raise ValueError("too large")
        destination.write_bytes(data)
        return DownloadResult(
            size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
        )

    def put_bytes(
        self,
        data: bytes,
        *,
        bucket: str,
        key: str,
        content_type: str,
        content_encoding: str | None,
        sha256: str,
    ) -> None:
        if self.upload_error is not None:
            raise self.upload_error
        if self.fail_put_suffix is not None and key.endswith(self.fail_put_suffix):
            raise RuntimeError("Injected artifact upload failure")
        self.objects[(bucket, key)] = data
        self.metadata[(bucket, key)] = {
            "content_type": content_type,
            "content_encoding": content_encoding,
            "sha256": sha256,
        }

    def delete_object(self, *, bucket: str, key: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted_keys.append((bucket, key))
        self.objects.pop((bucket, key), None)
        self.metadata.pop((bucket, key), None)

    def delete_prefix(self, *, bucket: str, prefix: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        keys = [
            object_key
            for object_bucket, object_key in self.objects
            if object_bucket == bucket and object_key.startswith(prefix)
        ]
        for key in keys:
            self.delete_object(bucket=bucket, key=key)

    def check_health(self, *, bucket: str) -> None:
        del bucket


class FakeExtractor:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.error: Exception | None = None
        self.delay_seconds = 0.0

    def extract(self, path: Path, *, media_type: str) -> ExtractionResult:
        self.calls.append((path, media_type))
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return ExtractionResult(
            document_json=b'{"schema_name":"DoclingDocument","version":"1.0.0"}',
            chunks=[
                ExtractedChunk(
                    text="Annual report\nRevenue increased by twenty percent.",
                    token_count=9,
                    section_path=["Annual report"],
                    page_numbers=[1],
                    provenance={
                        "source_refs": ["#/texts/0"],
                        "page_regions": [{"page_no": 1}],
                    },
                ),
                ExtractedChunk(
                    text="Operating costs remained stable.",
                    token_count=5,
                    section_path=["Annual report", "Costs"],
                    page_numbers=[2],
                    provenance={
                        "source_refs": ["#/texts/1"],
                        "page_regions": [{"page_no": 2}],
                    },
                ),
            ],
            extractor_version="test-docling",
        )


class FakeEmbeddingProvider:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []
        self.error: Exception | None = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.error is not None:
            raise self.error
        vectors: list[list[float]] = []
        for index, _ in enumerate(texts):
            vector = [0.0] * self.dimensions
            vector[index % self.dimensions] = 1.0
            vectors.append(vector)
        return vectors

    async def aclose(self) -> None:
        return None


class FakeVectorStore:
    def __init__(self) -> None:
        self.points: dict[UUID, VectorPoint] = {}
        self.ensure_calls = 0
        self.upsert_calls: list[list[VectorPoint]] = []
        self.deleted_documents: list[tuple[UUID, UUID]] = []
        self.search_calls: list[tuple[list[UUID], str, int]] = []
        self.error: Exception | None = None

    async def ensure_collection(self) -> None:
        self.ensure_calls += 1
        if self.error is not None:
            raise self.error

    async def upsert(self, points: list[VectorPoint]) -> None:
        if self.error is not None:
            raise self.error
        self.upsert_calls.append(points)
        self.points.update({point.id: point for point in points})

    async def delete_points(self, point_ids: list[UUID]) -> None:
        if self.error is not None:
            raise self.error
        for point_id in point_ids:
            self.points.pop(point_id, None)

    async def delete_document(self, workspace_id: UUID, document_id: UUID) -> None:
        if self.error is not None:
            raise self.error
        self.deleted_documents.append((workspace_id, document_id))
        self.points = {
            point_id: point
            for point_id, point in self.points.items()
            if point.payload.get("workspace_id") != str(workspace_id)
            or point.payload.get("document_id") != str(document_id)
        }

    async def search(
        self,
        vector: list[float],
        *,
        workspace_ids: list[UUID],
        embedding_profile: str,
        limit: int,
    ) -> list[VectorMatch]:
        del vector
        if self.error is not None:
            raise self.error
        self.search_calls.append((workspace_ids, embedding_profile, limit))
        allowed = {str(workspace_id) for workspace_id in workspace_ids}
        return [
            VectorMatch(id=point.id, score=0.9, payload=point.payload)
            for point in self.points.values()
            if point.payload.get("workspace_id") in allowed
            and point.payload.get("embedding_profile") == embedding_profile
        ][:limit]

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class ServiceContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    storage: InMemoryObjectStorage
    extractor: FakeExtractor
    embedding_provider: FakeEmbeddingProvider
    vector_store: FakeVectorStore

    def worker(self, settings: Settings | None = None) -> ProcessingWorker:
        return ProcessingWorker(
            self.session_factory,
            self.storage,
            self.extractor,
            settings or self.settings,
            self.embedding_provider,
            self.vector_store,
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        webhook_key_id="test-primary",
        webhook_secret=SecretStr("test-webhook-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
        api_token=SecretStr("test-internal-api-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
        s3_endpoint_url="http://unused",
        s3_access_key="test",
        s3_secret_key=SecretStr("test-storage-secret"),
        s3_raw_bucket="raw-documents",
        s3_artifact_bucket="context-documents",
        processing_temp_dir=tmp_path,
        processing_retry_base_seconds=0.01,
        processing_retry_max_seconds=0.01,
        embedding_api_key=SecretStr("test-embedding-api-key"),
        embedding_dimensions=8,
        embedding_batch_size=2,
        qdrant_api_key=SecretStr("test-qdrant-api-key"),
    )


@pytest_asyncio.fixture
async def db_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


@pytest.fixture
def object_storage() -> InMemoryObjectStorage:
    return InMemoryObjectStorage()


@pytest.fixture
def embedding_provider(settings: Settings) -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(settings.embedding_dimensions)


@pytest.fixture
def vector_store() -> FakeVectorStore:
    return FakeVectorStore()


@pytest.fixture
def extractor() -> FakeExtractor:
    return FakeExtractor()


@pytest.fixture
def context(
    settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    object_storage: InMemoryObjectStorage,
    extractor: FakeExtractor,
    embedding_provider: FakeEmbeddingProvider,
    vector_store: FakeVectorStore,
) -> ServiceContext:
    return ServiceContext(
        settings,
        db_session_factory,
        object_storage,
        extractor,
        embedding_provider,
        vector_store,
    )


@pytest.fixture
def app(
    settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    object_storage: InMemoryObjectStorage,
    embedding_provider: FakeEmbeddingProvider,
    vector_store: FakeVectorStore,
) -> Iterator[Any]:
    application = create_app(settings)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_object_storage] = lambda: object_storage
    application.dependency_overrides[get_embedding_provider] = lambda: embedding_provider
    application.dependency_overrides[get_vector_store] = lambda: vector_store
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.fixture
def internal_api_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}


def ready_event(
    content: bytes,
    *,
    workspace_id: UUID | None = None,
    document_id: UUID | None = None,
    event_id: UUID | None = None,
    media_type: str = "application/pdf",
    filename: str = "report.pdf",
    sha256: str | None = None,
) -> dict[str, Any]:
    workspace_id = workspace_id or uuid4()
    document_id = document_id or uuid4()
    digest = sha256 or hashlib.sha256(content).hexdigest()
    return {
        "id": str(event_id or uuid4()),
        "type": "document.ready",
        "source": "retriv-api",
        "subject": f"document/{document_id}",
        "schema_version": 1,
        "occurred_at": datetime.now(UTC).isoformat(),
        "data": {
            "document_id": str(document_id),
            "workspace_id": str(workspace_id),
            "bucket": "raw-documents",
            "object_key": f"workspaces/{workspace_id}/objects/{digest}/{document_id}",
            "original_filename": filename,
            "media_type": media_type,
            "size_bytes": len(content),
            "sha256": digest,
        },
    }


def deleted_event(ready: dict[str, Any]) -> dict[str, Any]:
    data = ready["data"]
    document_id = data["document_id"]
    return {
        "id": str(uuid4()),
        "type": "document.deleted",
        "source": "retriv-api",
        "subject": f"document/{document_id}",
        "schema_version": 1,
        "occurred_at": datetime.now(UTC).isoformat(),
        "data": {
            "document_id": document_id,
            "workspace_id": data["workspace_id"],
            "sha256": data["sha256"],
        },
    }


async def send_event(
    client: httpx.AsyncClient,
    settings: Settings,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    key_id: str | None = None,
    timestamp: int | None = None,
    body: bytes | None = None,
    signature_body: bytes | None = None,
) -> httpx.Response:
    timestamp = timestamp or int(datetime.now(UTC).timestamp())
    event_id = UUID(payload["id"])
    body = body or json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    signature = calculate_signature(
        secret or settings.webhook_secret.get_secret_value(),
        timestamp,
        event_id,
        signature_body or body,
    )
    return await client.post(
        "/internal/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(event_id),
            "X-Retriv-Key-Id": key_id or settings.webhook_key_id,
            "X-Retriv-Timestamp": str(timestamp),
            "X-Retriv-Signature": signature,
        },
    )
