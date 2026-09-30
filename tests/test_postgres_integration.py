"""PostgreSQL-only behaviour that SQLite cannot show.

The important one is concurrent identical uploads. SQLite serialises
writers so it can never reproduce a race, and the unique constraint on
(workspace_id, sha256) is what actually settles it in production.

Skipped unless TEST_POSTGRES_URL points at a real database.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.db.models import (
    JOB_EXTRACT,
    RUN_CHUNKING,
    ChunkEmbedding,
    Document,
    DocumentChunk,
    ProcessingRun,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.db.session import get_session
from app.infrastructure.storage import get_object_storage
from app.main import create_app
from app.processing.worker import ProcessingWorker
from app.security.auth import hash_password
from tests.conftest import InMemoryObjectStorage
from tests.test_worker import (
    FakeEmbeddingProvider,
    FakeExtractor,
    FakeStorage,
    FakeVectorStore,
)

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None, reason="TEST_POSTGRES_URL is not configured"
)


def database_url() -> str:
    assert POSTGRES_URL is not None
    return POSTGRES_URL


async def clear_database(session: AsyncSession) -> None:
    for model in (
        ChunkEmbedding,
        DocumentChunk,
        ProcessingRun,
        Document,
        WorkspaceMembership,
        Workspace,
        User,
    ):
        await session.execute(delete(model))
    await session.commit()


async def seed_owner(
    session: AsyncSession, *, slug: str = "finance", username: str = "alice"
) -> tuple[UUID, dict[str, str]]:
    user = User(
        username=username, password_hash=hash_password("pw-12345678"), role="user"
    )
    workspace = Workspace(slug=slug, name=slug.title())
    session.add_all([user, workspace])
    await session.flush()
    session.add(
        WorkspaceMembership(
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
            granted_by_user_id=user.id,
        )
    )
    await session.commit()
    del username, slug
    return workspace.id, {"username": user.username, "password": "pw-12345678"}


async def test_concurrent_identical_uploads_converge_in_postgresql() -> None:
    """Two identical uploads race on the unique (workspace_id, sha256).

    One wins and returns 202; the other must not create a second document
    or a second processing run. This is what the outbox used to paper
    over; now a single constraint has to do it.
    """
    from tests.document_samples import pdf_bytes

    engine = create_async_engine(database_url(), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings().model_copy(
        update={"environment": "test", "database_url": database_url()}
    )
    application = create_app(settings)
    storage = InMemoryObjectStorage()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_object_storage] = lambda: storage
    try:
        async with factory() as session:
            await clear_database(session)
            workspace_id, credentials = await seed_owner(session)

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            token = await client.post(
                "/v1/auth/token",
                data=credentials,
            )
            assert token.status_code == 200
            headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
            content = pdf_bytes()
            responses = await asyncio.gather(
                *(
                    client.post(
                        f"/v1/workspaces/{workspace_id}/documents",
                        headers=headers,
                        files={"file": ("report.pdf", content, "application/pdf")},
                    )
                    for _ in range(2)
                )
            )

        assert sorted(response.status_code for response in responses) == [200, 202], [
            response.text for response in responses
        ]
        assert sorted(
            response.json().get("duplicate", False) for response in responses
        ) == [False, True]

        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Document)) == 1
            assert (
                await session.scalar(select(func.count()).select_from(ProcessingRun))
                == 1
            )
    finally:
        application.dependency_overrides.clear()
        async with factory() as session:
            await clear_database(session)
        await engine.dispose()


async def test_chunk_and_embedding_rows_commit_in_foreign_key_order(
    settings: Settings,
) -> None:
    """The parent rows have to be flushed before the children.

    There is no ORM relationship between the pipeline tables, so the
    ordering is the worker's responsibility.
    """
    engine = create_async_engine(database_url(), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_settings = settings.model_copy(update={"database_url": database_url()})
    extractor = FakeExtractor()
    worker = ProcessingWorker(
        factory,
        FakeStorage(),
        extractor,
        worker_settings,
        FakeEmbeddingProvider(),
        FakeVectorStore(),
    )
    run_id, lease_token = uuid4(), uuid4()
    try:
        async with factory() as session:
            await clear_database(session)
            user = User(
                username="chunking", password_hash=hash_password("pw-12345678")
            )
            workspace = Workspace(slug="chunking", name="Chunking")
            session.add_all([user, workspace])
            await session.flush()
            document = Document(
                id=uuid4(),
                workspace_id=workspace.id,
                original_filename="report.pdf",
                detected_media_type="application/pdf",
                size_bytes=1024,
                sha256="a" * 64,
                bucket="raw-documents",
                object_key=f"workspaces/{workspace.id}/objects/{uuid4()}",
            )
            session.add(document)
            await session.flush()
            session.add(
                ProcessingRun(
                    id=run_id,
                    workspace_id=workspace.id,
                    document_id=document.id,
                    job_kind=JOB_EXTRACT,
                    pipeline_version=worker_settings.pipeline_version,
                    status=RUN_CHUNKING,
                    lease_token=lease_token,
                )
            )
            document.active_processing_run_id = run_id
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
            chunks = (
                await session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.processing_run_id == run_id
                    )
                )
            ).all()
        assert persisted is True
        assert run is not None
        assert run.status == RUN_CHUNKING
        assert [chunk.ordinal for chunk in chunks] == sorted(
            chunk.ordinal for chunk in chunks
        )
    finally:
        async with factory() as session:
            await clear_database(session)
        await engine.dispose()
