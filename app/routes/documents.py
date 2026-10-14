import logging
import re
from functools import partial
from typing import Annotated
from urllib.parse import quote

from anyio import to_thread
from fastapi import APIRouter, Depends, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
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
from app.documents.service import (
    accept_document_delete,
    ingest_staged_upload,
    storage_unavailable_error,
)
from app.documents.validation import stage_upload
from app.infrastructure.storage import ObjectNotFoundError, StorageError, iter_object
from app.processing.service import derived_document_status, load_active_run
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
    staged = None
    try:
        staged = await stage_upload(file, settings)
        result = await ingest_staged_upload(
            session=session,
            settings=settings,
            storage=storage,
            scanner=scanner,
            workspace_id=workspace.id,
            uploader_id=current_user.id,
            staged=staged,
        )
    finally:
        await file.close()
        if staged is not None:
            await to_thread.run_sync(partial(staged.path.unlink, missing_ok=True))
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_202_ACCEPTED
    return document_response(result.document, result.run, duplicate=result.duplicate)


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

    The permission check happened in the dependency; this only queues the
    work and removes the raw object.
    """
    await accept_document_delete(
        session=session,
        storage=storage,
        settings=settings,
        document_id=document.id,
        workspace_id=document.workspace_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
