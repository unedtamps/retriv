"""Accepting an uploaded document: scan, validate, store, link, enqueue.

A document is identified by its content hash, not by the workspace it was
uploaded to. Uploading the same bytes to a second workspace creates a link to
the existing canonical document and returns it; the object, chunks, and
vectors are not created twice. Only the last link owns the cleanup.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

from anyio import to_thread
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import (
    NON_TERMINAL_RUN_STATUSES,
    Document,
    DocumentWorkspace,
    ProcessingRun,
)
from app.documents.validation import StagedUpload, validate_document
from app.infrastructure.scanner import (
    MalwareDetectedError,
    MalwareScanner,
    ScannerUnavailableError,
)
from app.infrastructure.storage import ObjectStorage, StorageError
from app.processing.service import (
    enqueue_delete,
    enqueue_extract,
    enqueue_reextract,
    load_active_run,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestedDocument:
    document: Document
    link: DocumentWorkspace
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
    """Move one staged file through scanning, validation, storage and linking.

    Raises ``AppError`` for anything a caller should see -- malware, an
    unusable file, storage being down -- and leaves ``staged.path`` alone: the
    caller owns the temporary file and removes it when it is done.

    ``duplicate`` means the content already existed somewhere. It does not
    mean the workspace already had a link: this upload may be the one that
    attaches existing content to a new workspace.
    """
    await scan_upload(scanner, staged.path)
    validation = await to_thread.run_sync(validate_document, staged, settings)

    existing = await find_document_by_sha(session, staged.sha256)
    if existing is not None:
        existing = await session.scalar(
            select(Document).where(Document.id == existing.id).with_for_update()
        )
        if existing is None or existing.deleted_at is not None:
            raise document_deletion_error()
        link = await find_document_link(session, workspace_id, existing.id, lock=True)
        if link is None:
            link = DocumentWorkspace(
                workspace_id=workspace_id,
                document_id=existing.id,
                filename=staged.original_filename,
                created_by_user_id=uploader_id,
            )
            session.add(link)
            await session.commit()
        elif link.deleted_at is not None:
            link.deleted_at = None
            link.filename = staged.original_filename
            await session.commit()
        return IngestedDocument(
            document=existing,
            link=link,
            run=await load_active_run(session, existing),
            duplicate=True,
        )

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
        origin_workspace_id=workspace_id,
        original_filename=staged.original_filename,
        detected_media_type=validation.media_type,
        size_bytes=staged.size_bytes,
        sha256=staged.sha256,
        bucket=settings.s3_raw_bucket,
        object_key=object_key,
        created_by_user_id=uploader_id,
    )
    link = DocumentWorkspace(
        workspace_id=workspace_id,
        document_id=document_id,
        filename=staged.original_filename,
        created_by_user_id=uploader_id,
    )
    try:
        session.add(document)
        session.add(link)
        await session.flush()
        run = await enqueue_extract(session, document, settings)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        concurrent_document = await find_document_by_sha(session, staged.sha256)
        if concurrent_document is not None:
            if concurrent_document.deleted_at is not None:
                await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
                raise document_deletion_error() from None
            linked = await attach_to_existing(
                session=session,
                document=concurrent_document,
                workspace_id=workspace_id,
                uploader_id=uploader_id,
                filename=staged.original_filename,
            )
            await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
            return IngestedDocument(
                document=concurrent_document,
                link=linked,
                run=await load_active_run(session, concurrent_document),
                duplicate=True,
            )
        await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
        raise
    except Exception:
        await session.rollback()
        await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
        raise

    return IngestedDocument(document=document, link=link, run=run, duplicate=False)


async def attach_to_existing(
    *,
    session: AsyncSession,
    document: Document,
    workspace_id: UUID,
    uploader_id: UUID,
    filename: str,
) -> DocumentWorkspace:
    """Attach a workspace to an existing canonical document."""
    locked_document = await session.scalar(
        select(Document).where(Document.id == document.id).with_for_update()
    )
    if locked_document is None or locked_document.deleted_at is not None:
        raise document_deletion_error()
    document = locked_document
    link = await find_document_link(session, workspace_id, document.id, lock=True)
    if link is None:
        link = DocumentWorkspace(
            workspace_id=workspace_id,
            document_id=document.id,
            filename=filename,
            created_by_user_id=uploader_id,
        )
        session.add(link)
        await session.commit()
        return link
    if link.deleted_at is not None:
        link.deleted_at = None
        link.filename = filename
        await session.commit()
    return link


async def accept_document_reindex(
    *,
    session: AsyncSession,
    document_id: UUID,
    workspace_id: UUID,
    settings: Settings,
) -> ProcessingRun | None:
    """Lock the document, refuse a reindex while a run is in flight, queue one.

    The row lock makes the conflict check and the enqueue one decision: two
    owners reindexing together, or an owner reindexing while a run is
    mid-flight, cannot both pass. The owner role was checked by the caller's
    dependency; this only guards the pipeline state. ``None`` means the
    document vanished or was deleted under the lock.
    """
    locked_document = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_document is None or locked_document.deleted_at is not None:
        await session.rollback()
        return None
    locked_link = await session.scalar(
        select(DocumentWorkspace)
        .where(
            DocumentWorkspace.workspace_id == workspace_id,
            DocumentWorkspace.document_id == document_id,
        )
        .with_for_update()
    )
    if locked_link is None or locked_link.deleted_at is not None:
        await session.rollback()
        return None
    in_flight = await session.scalar(
        select(ProcessingRun.id).where(
            ProcessingRun.document_id == document_id,
            ProcessingRun.status.in_(NON_TERMINAL_RUN_STATUSES),
        )
    )
    if in_flight is not None:
        await session.rollback()
        raise AppError(
            status_code=409,
            code="reindex_conflict",
            title="Reindex conflict",
            detail="The document already has a run in progress. Wait for it to finish.",
        )
    run = await enqueue_reextract(session, locked_document, settings)
    await session.commit()
    return run


async def accept_document_delete(
    *,
    session: AsyncSession,
    storage: ObjectStorage,
    settings: Settings,
    document_id: UUID,
    workspace_id: UUID,
) -> Document | None:
    """Remove one workspace's link, and the content only when it is the last.

    The link is marked deleted first; if other links remain, nothing else is
    touched. The canonical row survives until the worker has removed the
    artifacts, and the embedding vectors cascade away with it. The raw
    object is removed only when the last link goes away.

    Returns the canonical document, or ``None`` when it disappeared between
    the caller's permission check and this lock.
    """
    locked_document = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_document is None:
        await session.rollback()
        return None

    locked_link = await session.scalar(
        select(DocumentWorkspace)
        .where(
            DocumentWorkspace.workspace_id == workspace_id,
            DocumentWorkspace.document_id == document_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_link is None:
        await session.rollback()
        return None

    if locked_link.deleted_at is None:
        locked_link.deleted_at = datetime.now(UTC)

    remaining_links = int(
        await session.scalar(
            select(func.count())
            .select_from(DocumentWorkspace)
            .where(
                DocumentWorkspace.document_id == document_id,
                DocumentWorkspace.deleted_at.is_(None),
            )
        )
        or 0
    )
    last_link = remaining_links == 0
    if last_link:
        await enqueue_delete(session, locked_document, settings)
    await session.commit()

    if not last_link:
        return locked_document

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


async def find_document_by_sha(session: AsyncSession, sha256: str) -> Document | None:
    """Find the canonical document for a content hash, across workspaces."""
    return await session.scalar(
        select(Document)
        .where(Document.sha256 == sha256)
        .order_by(Document.created_at, Document.id)
        .limit(1)
    )


async def find_document_link(
    session: AsyncSession,
    workspace_id: UUID,
    document_id: UUID,
    *,
    lock: bool = False,
) -> DocumentWorkspace | None:
    statement = select(DocumentWorkspace).where(
        DocumentWorkspace.workspace_id == workspace_id,
        DocumentWorkspace.document_id == document_id,
    )
    if lock:
        statement = statement.with_for_update()
    return await session.scalar(statement)


def document_deletion_error() -> AppError:
    return AppError(
        status_code=409,
        code="document_deletion_in_progress",
        title="Document deletion in progress",
        detail="An identical document is currently being deleted.",
    )


async def delete_uploaded_object(storage: ObjectStorage, bucket: str, key: str) -> None:
    try:
        await to_thread.run_sync(partial(storage.delete_object, bucket=bucket, key=key))
    except StorageError:
        logger.exception("Failed to remove orphaned object", extra={"bucket": bucket, "key": key})


def storage_unavailable_error(detail: str) -> AppError:
    return AppError(
        status_code=503,
        code="object_storage_unavailable",
        title="Object storage unavailable",
        detail=detail,
    )
