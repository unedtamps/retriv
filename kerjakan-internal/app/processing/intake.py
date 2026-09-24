from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.events import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    DocumentDeletedEvent,
    DocumentEvent,
    DocumentReadyEvent,
)
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import (
    ACTIVE_RUN_STATUSES,
    JOB_DELETE,
    JOB_EXTRACT,
    RUN_CANCELLED,
    RUN_QUEUED,
    RUN_UNSUPPORTED,
    ChunkEmbedding,
    DocumentChunk,
    EventInbox,
    ProcessingRun,
    SourceDocument,
)


async def accept_event(
    session: AsyncSession,
    event: DocumentEvent,
    *,
    payload: dict[str, object],
    payload_sha256: str,
    settings: Settings,
) -> bool:
    async with session.begin():
        existing_event = await session.get(EventInbox, event.id)
        if existing_event is not None:
            if existing_event.payload_sha256 != payload_sha256:
                raise AppError(
                    status_code=409,
                    code="event_payload_conflict",
                    title="Event payload conflict",
                    detail="The event ID was already received with different content.",
                )
            return True

        if event.source != settings.expected_event_source:
            raise AppError(
                status_code=422,
                code="unexpected_event_source",
                title="Unexpected event source",
                detail="The event source is not accepted by this service.",
            )

        inbox = EventInbox(
            event_id=event.id,
            event_type=event.type,
            schema_version=event.schema_version,
            payload=payload,
            payload_sha256=payload_sha256,
        )
        session.add(inbox)

        if isinstance(event, DocumentReadyEvent):
            await _accept_ready(session, event, settings)
            document = await session.get(
                SourceDocument,
                {
                    "workspace_id": event.data.workspace_id,
                    "document_id": event.data.document_id,
                },
            )
            if document is not None and document.deleted_at is not None:
                inbox.payload = {"redacted": True}
        else:
            await _accept_deleted(session, event, settings)
    return False


async def _accept_ready(
    session: AsyncSession, event: DocumentReadyEvent, settings: Settings
) -> None:
    data = event.data
    document = await session.get(
        SourceDocument,
        {"workspace_id": data.workspace_id, "document_id": data.document_id},
    )
    if document is not None:
        if document.deleted_at is not None:
            if document.sha256 != data.sha256:
                raise AppError(
                    status_code=409,
                    code="document_identity_conflict",
                    title="Document identity conflict",
                    detail="The deleted document ID has a different source hash.",
                )
            return
        if (
            document.sha256 != data.sha256
            or document.raw_bucket != data.bucket
            or document.raw_object_key != data.object_key
        ):
            raise AppError(
                status_code=409,
                code="document_identity_conflict",
                title="Document identity conflict",
                detail="The document ID was already registered with different source data.",
            )
        existing_run = await session.scalar(
            select(ProcessingRun.id).where(
                ProcessingRun.workspace_id == data.workspace_id,
                ProcessingRun.document_id == data.document_id,
                ProcessingRun.job_kind == JOB_EXTRACT,
            )
        )
        if existing_run is not None:
            return
    else:
        document = SourceDocument(
            workspace_id=data.workspace_id,
            document_id=data.document_id,
            source_event_id=event.id,
            source_occurred_at=event.occurred_at,
            original_filename=data.original_filename,
            media_type=data.media_type,
            size_bytes=data.size_bytes,
            sha256=data.sha256,
            raw_bucket=data.bucket,
            raw_object_key=data.object_key,
        )
        session.add(document)
        await session.flush()

    status = (
        RUN_QUEUED if data.media_type in {PDF_MEDIA_TYPE, DOCX_MEDIA_TYPE} else RUN_UNSUPPORTED
    )
    completed_at = datetime.now(UTC) if status == RUN_UNSUPPORTED else None
    session.add(
        ProcessingRun(
            id=uuid4(),
            workspace_id=data.workspace_id,
            document_id=data.document_id,
            job_kind=JOB_EXTRACT,
            pipeline_version=settings.pipeline_version,
            status=status,
            completed_at=completed_at,
            last_error=(
                None
                if status == RUN_QUEUED
                else f"Media type {data.media_type!r} is not processed by this pipeline"
            ),
        )
    )


async def _accept_deleted(
    session: AsyncSession, event: DocumentDeletedEvent, settings: Settings
) -> None:
    data = event.data
    document = await session.get(
        SourceDocument,
        {"workspace_id": data.workspace_id, "document_id": data.document_id},
    )
    if document is None:
        document = SourceDocument(
            workspace_id=data.workspace_id,
            document_id=data.document_id,
            source_event_id=event.id,
            source_occurred_at=event.occurred_at,
            sha256=data.sha256,
            deleted_at=event.occurred_at,
        )
        session.add(document)
        await session.flush()
    else:
        if document.sha256 != data.sha256:
            raise AppError(
                status_code=409,
                code="document_identity_conflict",
                title="Document identity conflict",
                detail="The deletion event does not match the registered document hash.",
            )
        document.deleted_at = event.occurred_at
        document.active_processing_run_id = None
        document.original_filename = None
        document.media_type = None
        document.size_bytes = None
        document.raw_bucket = None
        document.raw_object_key = None
        ready_inbox = await session.get(EventInbox, document.source_event_id)
        if ready_inbox is not None:
            ready_inbox.payload = {"redacted": True}

    await session.execute(
        delete(ChunkEmbedding).where(
            ChunkEmbedding.workspace_id == data.workspace_id,
            ChunkEmbedding.document_id == data.document_id,
        )
    )
    await session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.workspace_id == data.workspace_id,
            DocumentChunk.document_id == data.document_id,
        )
    )
    await session.execute(
        update(ProcessingRun)
        .where(
            ProcessingRun.workspace_id == data.workspace_id,
            ProcessingRun.document_id == data.document_id,
            ProcessingRun.status.in_(ACTIVE_RUN_STATUSES + (RUN_QUEUED,)),
        )
        .values(
            status=RUN_CANCELLED,
            lease_until=None,
            lease_token=None,
            completed_at=datetime.now(UTC),
            last_error="Document was deleted at the source",
        )
    )
    session.add(
        ProcessingRun(
            id=uuid4(),
            workspace_id=data.workspace_id,
            document_id=data.document_id,
            job_kind=JOB_DELETE,
            pipeline_version=settings.pipeline_version,
            status=RUN_QUEUED,
        )
    )
