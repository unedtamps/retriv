"""Accepting an uploaded document: scan, validate, store, enqueue.

The route owns the HTTP status codes; this module only reports whether the
upload was a duplicate. It exists so the steps that decide what gets stored
live in one place rather than inside a request handler.
"""

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import Document, ProcessingRun
from app.documents.validation import StagedUpload, validate_document
from app.infrastructure.scanner import (
    MalwareDetectedError,
    MalwareScanner,
    ScannerUnavailableError,
)
from app.infrastructure.storage import ObjectStorage, StorageError
from app.processing.service import enqueue_delete, enqueue_extract, load_active_run

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestedDocument:
    document: Document
    run: ProcessingRun | None
    duplicate: bool


async def ingest_staged_upload(
    *,
    session: AsyncSession,
    settings: Settings,
    storage: ObjectStorage,
    scanner: MalwareScanner,
    workspace_id: UUID,
    uploader_id: UUID,
    staged: StagedUpload,
) -> IngestedDocument:
    """Move one staged file through scanning, validation and storage.

    Raises ``AppError`` for anything a caller should see -- malware, an
    unusable file, storage being down -- and leaves ``staged.path`` alone: the
    caller owns the temporary file and removes it when it is done.
    """
    await scan_upload(scanner, staged.path)
    validation = await to_thread.run_sync(validate_document, staged, settings)

    existing = await find_duplicate(session, workspace_id, staged.sha256)
    if existing is not None:
        existing_run = await load_active_run(session, existing)
        # Only a document on its way out blocks a re-upload. One that is
        # still being indexed is a perfectly good duplicate: the run is
        # already queued, so returning it is the honest answer.
        if existing.deleted_at is not None:
            raise AppError(
                status_code=409,
                code="document_deletion_in_progress",
                title="Document deletion in progress",
                detail="An identical document is currently being deleted.",
            )
        return IngestedDocument(document=existing, run=existing_run, duplicate=True)

    document_id = uuid4()
    object_key = f"workspaces/{workspace_id}/objects/{staged.sha256}/{document_id}"
    try:
        await to_thread.run_sync(
            partial(
                storage.upload_file,
                staged.path,
                bucket=settings.s3_raw_bucket,
                key=object_key,
                content_type=validation.media_type,
                sha256=staged.sha256,
            )
        )
    except StorageError as exc:
        raise storage_unavailable_error("The document could not be stored.") from exc

    document = Document(
        id=document_id,
        workspace_id=workspace_id,
        original_filename=staged.original_filename,
        detected_media_type=validation.media_type,
        size_bytes=staged.size_bytes,
        sha256=staged.sha256,
        bucket=settings.s3_raw_bucket,
        object_key=object_key,
        created_by_user_id=uploader_id,
    )
    try:
        session.add(document)
        # Flush before enqueueing so the run's foreign key resolves.
        # Both rows commit together: a document cannot exist without a
        # way to be processed.
        await session.flush()
        run = await enqueue_extract(session, document, settings)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        concurrent_document = await find_duplicate(session, workspace_id, staged.sha256)
        if concurrent_document is not None:
            await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
            return IngestedDocument(
                document=concurrent_document,
                run=await load_active_run(session, concurrent_document),
                duplicate=True,
            )
        await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
        raise
    except Exception:
        await session.rollback()
        await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
        raise

    # Accepted, not created: the pipeline has the document and will report
    # progress through /v1/processing-runs.
    return IngestedDocument(document=document, run=run, duplicate=False)


async def accept_document_delete(
    *,
    session: AsyncSession,
    storage: ObjectStorage,
    settings: Settings,
    document_id: UUID,
    workspace_id: UUID,
) -> Document | None:
    """Accept a delete and let the worker finish it.

    The row is marked deleted and a delete run is queued; the worker clears
    the Qdrant points and the artifacts, then removes the row. It cannot be
    removed here: the Qdrant point IDs live in the embedding ledger, so
    deleting the document first would orphan every vector it ever produced.
    The raw object is removed immediately because nothing downstream needs it.

    Returns the document, or ``None`` when it disappeared between the
    caller's permission check and this lock.
    """
    locked_document = await session.scalar(
        select(Document)
        .where(Document.id == document_id, Document.workspace_id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_document is None:
        await session.rollback()
        return None

    await enqueue_delete(session, locked_document, settings)
    await session.commit()

    try:
        await to_thread.run_sync(
            partial(
                storage.delete_object,
                bucket=locked_document.bucket,
                key=locked_document.object_key,
            )
        )
    except StorageError as exc:
        raise storage_unavailable_error("The document could not be deleted.") from exc
    return locked_document


async def scan_upload(scanner: MalwareScanner, path: Path) -> None:
    try:
        await to_thread.run_sync(scanner.scan_file, path)
    except MalwareDetectedError as exc:
        raise AppError(
            status_code=422,
            code="malware_detected",
            title="Malware detected",
            detail="The uploaded file was rejected by the malware scanner.",
        ) from exc
    except ScannerUnavailableError as exc:
        raise AppError(
            status_code=503,
            code="malware_scanner_unavailable",
            title="Malware scanner unavailable",
            detail="The document cannot be accepted while malware scanning is unavailable.",
        ) from exc


async def find_duplicate(
    session: AsyncSession, workspace_id: UUID, sha256: str
) -> Document | None:
    return await session.scalar(
        select(Document).where(
            Document.workspace_id == workspace_id,
            Document.sha256 == sha256,
        )
    )


async def delete_uploaded_object(
    storage: ObjectStorage, bucket: str, key: str
) -> None:
    try:
        await to_thread.run_sync(partial(storage.delete_object, bucket=bucket, key=key))
    except StorageError:
        # A reconciliation task can remove the orphan if compensating deletion fails.
        logger.exception(
            "Failed to remove orphaned object", extra={"bucket": bucket, "key": key}
        )


def storage_unavailable_error(detail: str) -> AppError:
    return AppError(
        status_code=503,
        code="object_storage_unavailable",
        title="Object storage unavailable",
        detail=detail,
    )
