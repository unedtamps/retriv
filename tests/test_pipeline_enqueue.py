"""Enqueueing for the processing pipeline.

This replaces the webhook intake tests. The event inbox existed to make
a replayed HTTP delivery idempotent; with the document and its run
committing in one transaction there is no replay, so the idempotency now
has to come from the constraints on the rows themselves.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    JOB_DELETE,
    JOB_EXTRACT,
    RUN_CANCELLED,
    RUN_QUEUED,
    RUN_UNSUPPORTED,
    Document,
    ProcessingRun,
)
from app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.processing.service import (
    derived_document_status,
    enqueue_delete,
    enqueue_extract,
)
from tests.conftest import SeededEntities

pytestmark = pytest.mark.asyncio


def new_document(workspace_id: UUID, media_type: str = PDF_MEDIA_TYPE) -> Document:
    import hashlib

    digest = hashlib.sha256(str(uuid4()).encode()).hexdigest()
    return Document(
        id=uuid4(),
        origin_workspace_id=workspace_id,
        original_filename="report.pdf",
        detected_media_type=media_type,
        size_bytes=1024,
        sha256=digest,
        bucket="raw-documents",
        object_key=f"workspaces/{workspace_id}/objects/{digest}/{uuid4()}",
    )


async def test_enqueue_extract_queues_a_run_and_points_the_document_at_it(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    document = new_document(entities.finance.id)
    async with db_session_factory() as session, session.begin():
        session.add(document)
        await session.flush()
        run = await enqueue_extract(session, document, settings)

    assert run is not None
    assert run.job_kind == JOB_EXTRACT
    assert run.status == RUN_QUEUED
    assert run.origin_workspace_id == entities.finance.id
    assert run.pipeline_version == settings.pipeline_version
    assert document.active_processing_run_id == run.id
    assert derived_document_status(document, run) == "processing"


async def test_enqueue_extract_is_idempotent(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """The webhook's event-id dedup is gone; this is what replaces it.

    A second enqueue returns the run that already exists rather than
    creating a second one, so a retried request cannot index a document
    twice.
    """
    document = new_document(entities.finance.id)
    async with db_session_factory() as session, session.begin():
        session.add(document)
        await session.flush()
        first = await enqueue_extract(session, document, settings)
        second = await enqueue_extract(session, document, settings)

    assert second is not None
    assert second.id == first.id
    async with db_session_factory() as session:
        runs = (
            await session.scalars(
                select(ProcessingRun).where(ProcessingRun.document_id == document.id)
            )
        ).all()
    assert len(runs) == 1


async def test_an_unsupported_format_fails_the_run_immediately(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A spreadsheet is stored but cannot be extracted.

    The run is created already terminal rather than queued, so the
    document reports the real outcome immediately instead of sitting on
    "processing" until someone notices nothing is happening.
    """
    document = new_document(
        entities.finance.id,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    async with db_session_factory() as session, session.begin():
        session.add(document)
        await session.flush()
        run = await enqueue_extract(session, document, settings)

    assert run.status == RUN_UNSUPPORTED
    assert run.completed_at is not None
    assert run.last_error is not None
    assert "spreadsheetml" in run.last_error
    assert derived_document_status(document, run) == "unsupported_format"


@pytest.mark.parametrize("media_type", [PDF_MEDIA_TYPE, DOCX_MEDIA_TYPE])
async def test_supported_formats_queue_normally(
    media_type: str,
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    document = new_document(entities.finance.id, media_type)
    async with db_session_factory() as session, session.begin():
        session.add(document)
        await session.flush()
        run = await enqueue_extract(session, document, settings)
    assert run.status == RUN_QUEUED


async def test_enqueue_delete_marks_and_queues_exactly_once(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    document = new_document(entities.finance.id)
    async with db_session_factory() as session, session.begin():
        session.add(document)
        await session.flush()
        first = await enqueue_delete(session, document, settings)

    assert first is not None
    assert first.job_kind == JOB_DELETE
    assert first.status == RUN_QUEUED
    assert document.deleted_at is not None
    assert document.active_processing_run_id is None

    async with db_session_factory() as session, session.begin():
        stored = await session.get(Document, document.id)
        assert stored is not None
        second = await enqueue_delete(session, stored, settings)

    assert second is None, "a queued cleanup must not be duplicated"
    async with db_session_factory() as session:
        runs = (
            await session.scalars(
                select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE)
            )
        ).all()
    assert len(runs) == 1


async def test_enqueue_delete_cancels_only_the_extraction(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    document = new_document(entities.finance.id)
    async with db_session_factory() as session, session.begin():
        session.add(document)
        await session.flush()
        extract = await enqueue_extract(session, document, settings)
        await enqueue_delete(session, document, settings)

    async with db_session_factory() as session:
        stored = await session.get(ProcessingRun, extract.id)
    assert stored is not None
    assert stored.status == RUN_CANCELLED
    assert "deleted" in (stored.last_error or "")


async def test_derived_status_covers_every_run_outcome(
    settings: Settings, entities: SeededEntities
) -> None:
    document = new_document(entities.finance.id)
    assert derived_document_status(document, None) == "ready"

    for status, expected in (
        (RUN_QUEUED, "processing"),
        ("extracting", "processing"),
        ("embedding", "processing"),
        ("deleting", "processing"),
        ("ready", "ready"),
        ("failed", "failed"),
        (RUN_CANCELLED, "cancelled"),
        (RUN_UNSUPPORTED, "unsupported_format"),
    ):
        run = ProcessingRun(
            id=uuid4(),
            document_id=document.id,
            origin_workspace_id=document.origin_workspace_id,
            job_kind=JOB_EXTRACT,
            pipeline_version=settings.pipeline_version,
            status=status,
        )
        assert derived_document_status(document, run) == expected, status

    document.deleted_at = datetime.now(UTC)
    run = ProcessingRun(
        id=uuid4(),
        document_id=document.id,
        origin_workspace_id=document.origin_workspace_id,
        job_kind=JOB_EXTRACT,
        pipeline_version=settings.pipeline_version,
        status="ready",
    )
    assert derived_document_status(document, run) == "deleting"
