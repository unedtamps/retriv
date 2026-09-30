import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.models import (
    EMBEDDING_PENDING,
    JOB_EXTRACT,
    RUN_CHUNKING,
    RUN_EMBEDDING,
    ChunkEmbedding,
    DocumentChunk,
    EventInbox,
    ProcessingRun,
    SourceDocument,
)
from app.db.session import get_session
from app.main import create_app
from app.processing.worker import ProcessingWorker
from tests.conftest import (
    FakeEmbeddingProvider,
    FakeExtractor,
    FakeVectorStore,
    InMemoryObjectStorage,
    ready_event,
    send_event,
)

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")


@pytest.mark.integration
@pytest.mark.skipif(POSTGRES_URL is None, reason="TEST_POSTGRES_URL is not configured")
async def test_concurrent_identical_events_converge_in_postgresql() -> None:
    assert POSTGRES_URL is not None
    engine = create_async_engine(POSTGRES_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings().model_copy(
        update={
            "environment": "test",
            "database_url": POSTGRES_URL,
            "webhook_key_id": "postgres-test",
        }
    )
    application = create_app(settings)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = override_session
    async with factory() as session:
        await clear_database(session)

    event = ready_event(b"concurrent-pdf")
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            send_event(client, settings, event),
            send_event(client, settings, event),
        )

    assert [response.status_code for response in responses] == [202, 202], [
        response.text for response in responses
    ]
    assert sorted(response.json()["duplicate"] for response in responses) == [False, True]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(EventInbox)) == 1
        assert await session.scalar(select(func.count()).select_from(SourceDocument)) == 1
        assert await session.scalar(select(func.count()).select_from(ProcessingRun)) == 1
        await clear_database(session)
    application.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.skipif(POSTGRES_URL is None, reason="TEST_POSTGRES_URL is not configured")
async def test_chunk_and_embedding_rows_commit_in_foreign_key_order(
    settings: Settings,
    object_storage: InMemoryObjectStorage,
    extractor: FakeExtractor,
) -> None:
    assert POSTGRES_URL is not None
    engine = create_async_engine(POSTGRES_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_settings = settings.model_copy(update={"database_url": POSTGRES_URL})
    worker = ProcessingWorker(
        factory,
        object_storage,
        extractor,
        worker_settings,
        FakeEmbeddingProvider(worker_settings.embedding_dimensions),
        FakeVectorStore(),
    )
    workspace_id, document_id, run_id, lease_token = (uuid4() for _ in range(4))
    document = SourceDocument(
        workspace_id=workspace_id,
        document_id=document_id,
        source_event_id=uuid4(),
        source_occurred_at=datetime.now(UTC),
        sha256="a" * 64,
    )
    try:
        async with factory() as session:
            await clear_database(session)
            session.add(document)
            await session.flush()
            session.add(
                ProcessingRun(
                    id=run_id,
                    workspace_id=workspace_id,
                    document_id=document_id,
                    job_kind=JOB_EXTRACT,
                    pipeline_version=worker_settings.pipeline_version,
                    status=RUN_CHUNKING,
                    lease_token=lease_token,
                )
            )
            await session.commit()

        persisted = await worker._persist_chunks(
            run_id,
            lease_token,
            document,
            extractor.extract(Path("unused.pdf"), media_type="application/pdf"),
            artifact_key="artifact.json.gz",
            manifest_key="manifest.json",
        )

        async with factory() as session:
            run = await session.get(ProcessingRun, run_id)
            chunks = (await session.scalars(select(DocumentChunk))).all()
            embeddings = (await session.scalars(select(ChunkEmbedding))).all()
        assert persisted is True
        assert run is not None and run.status == RUN_EMBEDDING
        assert run.chunk_count == 2
        assert len(chunks) == len(embeddings) == 2
        assert {embedding.chunk_id for embedding in embeddings} == {
            chunk.id for chunk in chunks
        }
        assert {embedding.status for embedding in embeddings} == {EMBEDDING_PENDING}
    finally:
        async with factory() as session:
            await clear_database(session)
        await engine.dispose()


async def clear_database(session: AsyncSession) -> None:
    for model in (
        ChunkEmbedding,
        DocumentChunk,
        ProcessingRun,
        SourceDocument,
        EventInbox,
    ):
        await session.execute(delete(model))
    await session.commit()
