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
from app.db.models import Document, DocumentWorkspace, ProcessingRun
from app.documents.validation import StagedUpload, validate_document
from app.infrastructure.scanner import (
    MalwareDetectedError,
    MalwareScanner,
    ScannerUnavailableError,
)
from app.infrastructure.storage import ObjectStorage, StorageError
from app.processing.service import (
    enqueue_access_sync,
    enqueue_delete,
    enqueue_extract,
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
        # Lock the canonical row before changing its links. A last-link
        # delete locks the same row first, so an upload cannot reactivate a
        # link while the document is being removed.
        existing = await session.scalar(
            select(Document).where(Document.id == existing.id).with_for_update()
        )
        if existing is None or existing.deleted_at is not None:
            raise document_deletion_error()
        link = await find_document_link(
            session, workspace_id, existing.id, lock=True
        )
        if link is None:
            link = DocumentWorkspace(
                workspace_id=workspace_id,
                document_id=existing.id,
                filename=staged.original_filename,
                created_by_user_id=uploader_id,
            )
            session.add(link)
            await enqueue_access_sync(session, existing, settings)
            await session.commit()
        elif link.deleted_at is not None:
            # The workspace removed this link while another workspace kept
            # the content alive. Re-uploading the same bytes re-attaches it.
            link.deleted_at = None
            link.filename = staged.original_filename
            await enqueue_access_sync(session, existing, settings)
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
        # Flush before enqueueing so the run's foreign key resolves.
        # The document, its first link and its run commit together.
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
                settings=settings,
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

    # Accepted, not created: the pipeline has the document and will report
    # progress through /v1/processing-runs.
    return IngestedDocument(document=document, link=link, run=run, duplicate=False)


async def attach_to_existing(
    *,
    session: AsyncSession,
    settings: Settings,
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
    link = await find_document_link(
        session, workspace_id, document.id, lock=True
    )
    if link is None:
        link = DocumentWorkspace(
            workspace_id=workspace_id,
            document_id=document.id,
            filename=filename,
            created_by_user_id=uploader_id,
        )
        session.add(link)
        await enqueue_access_sync(session, document, settings)
        await session.commit()
        return link
    if link.deleted_at is not None:
        link.deleted_at = None
        link.filename = filename
        await enqueue_access_sync(session, document, settings)
        await session.commit()
    return link


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
    vectors and artifacts, because the Qdrant point IDs live in rows that
    cascade away with it. The raw object is removed only when the last link
    goes away.

    Returns the canonical document, or ``None`` when it disappeared between
    the caller's permission check and this lock.
    """
    # Lock order is document then link, matching the upload path. Taking the
    # link first would let a concurrent upload and delete deadlock.
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

    already_deleted = locked_link.deleted_at is not None
    if not already_deleted:
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
    elif not already_deleted:
        await enqueue_access_sync(session, locked_document, settings)
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
