import logging
import re
from functools import partial
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import APIRouter, Depends, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    DocumentContext,
    DownloadableDocumentContext,
    MutableDocumentContext,
    ScannerDependency,
    StorageDependency,
    UploadWorkspaceContext,
    WorkspaceContext,
)
from app.api.schemas import DocumentListResponse, DocumentResponse
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import Document, ProcessingRun
from app.db.session import get_session
from app.documents.validation import StagedUpload, stage_upload, validate_document
from app.infrastructure.scanner import MalwareDetectedError, ScannerUnavailableError
from app.infrastructure.storage import ObjectNotFoundError, StorageError, iter_object
from app.processing.service import (
    derived_document_status,
    enqueue_delete,
    enqueue_extract,
    load_active_run,
)
from app.security.auth import CurrentUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/workspaces/{workspace_id}/documents", tags=["documents"])


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    workspace: UploadWorkspaceContext,
    current_user: CurrentUser,
    file: UploadFile,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    storage: StorageDependency,
    scanner: ScannerDependency,
) -> DocumentResponse:
    staged: StagedUpload | None = None
    try:
        staged = await stage_upload(file, settings)
        await scan_upload(scanner, staged.path)
        validation = await to_thread.run_sync(validate_document, staged, settings)

        existing = await find_duplicate(session, workspace.id, staged.sha256)
        if existing is not None:
            existing_run = await load_active_run(session, existing)
            # Only a document on its way out blocks a re-upload. One that
            # is still being indexed is a perfectly good duplicate: the
            # run is already queued, so returning it is the honest answer.
            if existing.deleted_at is not None:
                raise AppError(
                    status_code=409,
                    code="document_deletion_in_progress",
                    title="Document deletion in progress",
                    detail="An identical document is currently being deleted.",
                )
            response.status_code = status.HTTP_200_OK
            return document_response(existing, existing_run, duplicate=True)

        document_id = uuid4()
        object_key = f"workspaces/{workspace.id}/objects/{staged.sha256}/{document_id}"
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
            raise storage_unavailable_error(
                "The document could not be stored."
            ) from exc

        document = Document(
            id=document_id,
            workspace_id=workspace.id,
            original_filename=staged.original_filename,
            detected_media_type=validation.media_type,
            size_bytes=staged.size_bytes,
            sha256=staged.sha256,
            bucket=settings.s3_raw_bucket,
            object_key=object_key,
            created_by_user_id=current_user.id,
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
            concurrent_document = await find_duplicate(
                session, workspace.id, staged.sha256
            )
            if concurrent_document is not None:
                await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
                response.status_code = status.HTTP_200_OK
                return document_response(
                    concurrent_document,
                    await load_active_run(session, concurrent_document),
                    duplicate=True,
                )
            await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
            raise
        except Exception:
            await session.rollback()
            await delete_uploaded_object(storage, settings.s3_raw_bucket, object_key)
            raise

        # Accepted, not created: the pipeline has the document and will
        # report progress through /v1/processing-runs.
        response.status_code = status.HTTP_202_ACCEPTED
        return document_response(document, run)
    finally:
        await file.close()
        if staged is not None:
            await to_thread.run_sync(partial(staged.path.unlink, missing_ok=True))


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    workspace: WorkspaceContext,
    _current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DocumentListResponse:
    # deleted_at is filtered here but not on the single-document route.
    # Deletion is asynchronous: the row is kept until the worker removes
    # the vectors, so listing it would show a document that cannot be
    # downloaded and is already unsearchable, and a refresh straight
    # after a delete would put it back. The single-document route still
    # answers 200 with status "deleting" so a client holding the id can
    # tell what happened.
    condition = and_(Document.workspace_id == workspace.id, Document.deleted_at.is_(None))
    total = await session.scalar(
        select(func.count()).select_from(Document).where(condition)
    )
    # The active run is joined rather than fetched per row: the list view
    # needs a status for every document, and a query per document would
    # be one round trip each.
    result = await session.execute(
        select(Document, ProcessingRun)
        .outerjoin(ProcessingRun, ProcessingRun.id == Document.active_processing_run_id)
        .where(condition)
        .order_by(Document.created_at.desc(), Document.id)
        .offset(offset)
        .limit(limit)
    )
    return DocumentListResponse(
        items=[document_response(item, run) for item, run in result.all()],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document_metadata(
    document: DocumentContext,
    _current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentResponse:
    run = await load_active_run(session, document)
    return document_response(document, run)


@router.get("/{document_id}/download")
async def download_document(
    document: DownloadableDocumentContext,
    storage: StorageDependency,
) -> StreamingResponse:
    if document.deleted_at is not None:
        raise AppError(
            status_code=409,
            code="document_deletion_in_progress",
            title="Document deletion in progress",
            detail="The document cannot be downloaded while deletion is in progress.",
        )
    try:
        stream = await to_thread.run_sync(
            partial(
                storage.open_object, bucket=document.bucket, key=document.object_key
            )
        )
    except ObjectNotFoundError as exc:
        raise AppError(
            status_code=502,
            code="stored_object_missing",
            title="Stored object missing",
            detail="Document metadata exists but its stored object is unavailable.",
        ) from exc
    except StorageError as exc:
        raise storage_unavailable_error(
            "The document could not be downloaded."
        ) from exc

    return StreamingResponse(
        iter_object(stream),
        media_type=document.detected_media_type,
        headers={
            "Content-Length": str(document.size_bytes),
            "Content-Disposition": content_disposition(document.original_filename),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document: MutableDocumentContext,
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: StorageDependency,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Accept a delete and let the worker finish it.

    The row is marked deleted and a delete run is queued; the worker
    clears the Qdrant points and the artifacts, then removes the row.
    It cannot be removed here: the Qdrant point IDs live in the
    embedding ledger, so deleting the document first would orphan every
    vector it ever produced. The raw object is removed immediately
    because nothing downstream needs it.
    """
    locked_document = await session.scalar(
        select(Document)
        .where(Document.id == document.id, Document.workspace_id == document.workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_document is None:
        await session.rollback()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    document = locked_document

    await enqueue_delete(session, document, settings)
    await session.commit()

    try:
        await to_thread.run_sync(
            partial(
                storage.delete_object, bucket=document.bucket, key=document.object_key
            )
        )
    except StorageError as exc:
        raise storage_unavailable_error("The document could not be deleted.") from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def scan_upload(scanner: ScannerDependency, path: Path) -> None:
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
    storage: StorageDependency, bucket: str, key: str
) -> None:
    try:
        await to_thread.run_sync(partial(storage.delete_object, bucket=bucket, key=key))
    except StorageError:
        # A reconciliation task can remove the orphan if compensating deletion fails.
        logger.exception(
            "Failed to remove orphaned object", extra={"bucket": bucket, "key": key}
        )


def document_response(
    document: Document,
    run: ProcessingRun | None = None,
    *,
    duplicate: bool = False,
) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        workspace_id=document.workspace_id,
        original_filename=document.original_filename,
        detected_media_type=document.detected_media_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        status=derived_document_status(document, run),
        row_count=run.row_count if run is not None else 0,
        created_by_user_id=document.created_by_user_id,
        created_at=document.created_at,
        duplicate=duplicate,
    )


def content_disposition(filename: str) -> str:
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename).strip("._") or "document"
    encoded_filename = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_filename}"


def storage_unavailable_error(detail: str) -> AppError:
    return AppError(
        status_code=503,
        code="object_storage_unavailable",
        title="Object storage unavailable",
        detail=detail,
    )
