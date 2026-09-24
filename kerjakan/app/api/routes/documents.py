import logging
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4, uuid5

from anyio import to_thread
from fastapi import APIRouter, Depends, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
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
from app.db.models import Document, OutboxEvent
from app.db.session import get_session
from app.documents.validation import StagedUpload, stage_upload, validate_document
from app.events.contracts import (
    DOCUMENT_DELETED_EVENT_TYPE,
    DOCUMENT_READY_EVENT_TYPE,
    EVENT_SOURCE,
    DocumentDeletedData,
    DocumentDeletedEvent,
    DocumentReadyData,
    DocumentReadyEvent,
)
from app.infrastructure.scanner import MalwareDetectedError, ScannerUnavailableError
from app.infrastructure.storage import ObjectNotFoundError, StorageError, iter_object
from app.security.auth import CurrentUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/workspaces/{workspace_id}/documents", tags=["documents"])


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
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
            if existing.status != "ready":
                raise AppError(
                    status_code=409,
                    code="document_deletion_in_progress",
                    title="Document deletion in progress",
                    detail="An identical document is currently being deleted.",
                )
            response.status_code = status.HTTP_200_OK
            return document_response(existing, duplicate=True)

        document_id = uuid4()
        object_key = f"workspaces/{workspace.id}/objects/{staged.sha256}/{document_id}"
        try:
            await to_thread.run_sync(
                partial(
                    storage.upload_file,
                    staged.path,
                    bucket=settings.s3_bucket,
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
            bucket=settings.s3_bucket,
            object_key=object_key,
            created_by_user_id=current_user.id,
        )
        event_id = uuid4()
        event = DocumentReadyEvent(
            id=event_id,
            type=DOCUMENT_READY_EVENT_TYPE,
            source=EVENT_SOURCE,
            subject=f"document/{document.id}",
            schema_version=1,
            occurred_at=datetime.now(UTC),
            data=DocumentReadyData(
                document_id=document.id,
                workspace_id=document.workspace_id,
                bucket=document.bucket,
                object_key=document.object_key,
                original_filename=document.original_filename,
                media_type=document.detected_media_type,
                size_bytes=document.size_bytes,
                sha256=document.sha256,
            ),
        )
        try:
            session.add(document)
            session.add(
                OutboxEvent(
                    id=event_id,
                    event_type=DOCUMENT_READY_EVENT_TYPE,
                    aggregate_type="document",
                    aggregate_id=document.id,
                    workspace_id=document.workspace_id,
                    payload=event.model_dump(mode="json"),
                )
            )
            await session.flush()
            await session.commit()
        except IntegrityError:
            await session.rollback()
            concurrent_document = await find_duplicate(
                session, workspace.id, staged.sha256
            )
            if concurrent_document is not None:
                await delete_uploaded_object(storage, settings.s3_bucket, object_key)
                response.status_code = status.HTTP_200_OK
                return document_response(concurrent_document, duplicate=True)
            await delete_uploaded_object(storage, settings.s3_bucket, object_key)
            raise
        except Exception:
            await session.rollback()
            await delete_uploaded_object(storage, settings.s3_bucket, object_key)
            raise

        await session.refresh(document)
        return document_response(document)
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
    condition = Document.workspace_id == workspace.id
    total = await session.scalar(
        select(func.count()).select_from(Document).where(condition)
    )
    result = await session.scalars(
        select(Document)
        .where(condition)
        .order_by(Document.created_at.desc(), Document.id)
        .offset(offset)
        .limit(limit)
    )
    return DocumentListResponse(
        items=[document_response(item) for item in result.all()],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document_metadata(
    document: DocumentContext,
    _current_user: CurrentUser,
) -> DocumentResponse:
    return document_response(document)


@router.get("/{document_id}/download")
async def download_document(
    document: DownloadableDocumentContext,
    storage: StorageDependency,
) -> StreamingResponse:
    if document.status != "ready":
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
) -> Response:
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

    event_id = uuid5(document.id, DOCUMENT_DELETED_EVENT_TYPE)
    if document.status == "ready":
        document.status = "deleting"

    existing_event_id = await session.scalar(
        select(OutboxEvent.id).where(OutboxEvent.id == event_id)
    )
    if existing_event_id is None:
        event = DocumentDeletedEvent(
            id=event_id,
            type=DOCUMENT_DELETED_EVENT_TYPE,
            source=EVENT_SOURCE,
            subject=f"document/{document.id}",
            schema_version=1,
            occurred_at=datetime.now(UTC),
            data=DocumentDeletedData(
                document_id=document.id,
                workspace_id=document.workspace_id,
                sha256=document.sha256,
            ),
        )
        session.add(
            OutboxEvent(
                id=event_id,
                event_type=DOCUMENT_DELETED_EVENT_TYPE,
                aggregate_type="document",
                aggregate_id=document.id,
                workspace_id=document.workspace_id,
                payload=event.model_dump(mode="json"),
            )
        )
    await session.commit()

    try:
        await to_thread.run_sync(
            partial(
                storage.delete_object, bucket=document.bucket, key=document.object_key
            )
        )
    except StorageError as exc:
        raise storage_unavailable_error("The document could not be deleted.") from exc

    await session.execute(
        delete(Document).where(
            Document.id == document.id,
            Document.status == "deleting",
        )
    )
    await session.commit()
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
    document: Document, *, duplicate: bool = False
) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        workspace_id=document.workspace_id,
        original_filename=document.original_filename,
        detected_media_type=document.detected_media_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        status=document.status,
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
