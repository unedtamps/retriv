"""Enqueueing for the processing pipeline.

The upload and delete routes call these functions inside their own
transaction. There is no outbox and no webhook: the document row and its
processing run commit together or not at all, so a document can never
exist without a way to be processed.
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import (
    ACTIVE_RUN_STATUSES,
    JOB_DELETE,
    JOB_EXTRACT,
    RUN_CANCELLED,
    RUN_QUEUED,
    RUN_UNSUPPORTED,
    Document,
    ProcessingRun,
)
from app.documents.validation import CSV_MEDIA_TYPE, DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE

SUPPORTED_MEDIA_TYPES = frozenset({PDF_MEDIA_TYPE, DOCX_MEDIA_TYPE, CSV_MEDIA_TYPE})


def initial_extract_status(media_type: str) -> str:
    return RUN_QUEUED if media_type in SUPPORTED_MEDIA_TYPES else RUN_UNSUPPORTED


async def enqueue_extract(
    session: AsyncSession, document: Document, settings: Settings
) -> ProcessingRun:
    """Queue a document for extraction and indexing.

    The document must already be flushed so the run's foreign key
    resolves. Re-enqueueing a document that already has a run is a no-op,
    which is what made the old webhook replay safe.
    """
    existing_run = await session.scalar(
        select(ProcessingRun.id).where(
            ProcessingRun.document_id == document.id,
            ProcessingRun.job_kind == JOB_EXTRACT,
        )
    )
    if existing_run is not None:
        return await session.get(ProcessingRun, existing_run)  # type: ignore[return-value]

    status = initial_extract_status(document.detected_media_type)
    run = ProcessingRun(
        id=uuid4(),
        document_id=document.id,
        workspace_id=document.workspace_id,
        job_kind=JOB_EXTRACT,
        pipeline_version=settings.pipeline_version,
        status=status,
        completed_at=datetime.now(UTC) if status == RUN_UNSUPPORTED else None,
        last_error=(
            None
            if status == RUN_QUEUED
            else f"Media type {document.detected_media_type!r} is not processed by this pipeline"
        ),
    )
    session.add(run)
    document.active_processing_run_id = run.id
    return run


async def enqueue_delete(
    session: AsyncSession, document: Document, settings: Settings
) -> ProcessingRun | None:
    """Mark a document deleted and queue its cleanup.

    The row is kept until the worker finishes, because the worker needs
    the chunk embeddings' Qdrant point IDs to remove the vectors. It
    hard-deletes the document last, which cascades to the chunks and the
    embeddings. A hard delete before that would orphan the vectors.
    """
    if document.deleted_at is None:
        document.deleted_at = datetime.now(UTC)
    document.active_processing_run_id = None

    # Cancel any extraction still in flight; there is nothing left to index.
    # Scoped to JOB_EXTRACT on purpose: a delete run already queued for this
    # document is the one doing the work, and cancelling it would leave the
    # vectors orphaned.
    await session.execute(
        update(ProcessingRun)
        .where(
            ProcessingRun.document_id == document.id,
            ProcessingRun.job_kind == JOB_EXTRACT,
            ProcessingRun.status.in_((*ACTIVE_RUN_STATUSES, RUN_QUEUED)),
        )
        .values(
            status=RUN_CANCELLED,
            lease_until=None,
            lease_token=None,
            completed_at=datetime.now(UTC),
            last_error="Document was deleted",
        )
    )

    existing_delete_run = await session.scalar(
        select(ProcessingRun.id).where(
            ProcessingRun.document_id == document.id,
            ProcessingRun.job_kind == JOB_DELETE,
            ProcessingRun.status.in_((RUN_QUEUED, *ACTIVE_RUN_STATUSES)),
        )
    )
    if existing_delete_run is not None:
        return None

    run = ProcessingRun(
        id=uuid4(),
        document_id=document.id,
        workspace_id=document.workspace_id,
        job_kind=JOB_DELETE,
        pipeline_version=settings.pipeline_version,
        status=RUN_QUEUED,
    )
    session.add(run)
    return run


def derived_document_status(document: Document, run: ProcessingRun | None) -> str:
    """The document's state, derived from its active run.

    There is no status column on the document: this is the single place
    that decides what the API reports, so a document and its pipeline can
    never disagree.
    """
    if document.deleted_at is not None:
        return "deleting"
    if run is None:
        return "ready"
    if run.status == "ready":
        return "ready"
    if run.status in {"failed", "cancelled", "unsupported_format"}:
        return run.status
    return "processing"


async def load_active_run(
    session: AsyncSession, document: Document
) -> ProcessingRun | None:
    if document.active_processing_run_id is None:
        return None
    return await session.get(ProcessingRun, document.active_processing_run_id)


