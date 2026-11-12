import logging
import re
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from anyio import to_thread
from fastapi import APIRouter, Depends, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    DocumentContext,
    DownloadableDocumentContext,
    MutableDocumentContext,
    OwnerDocumentContext,
    ScannerDependency,
    StorageDependency,
    UploadWorkspaceContext,
    WorkspaceContext,
)
from app.api.schemas import (
    ChunkRegionsResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentSourceRegion,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentWorkspace,
    ProcessingRun,
)
from app.db.session import get_session
from app.documents.json_protocol import (
    JsonLimits,
    JsonProtocolError,
    validate_json_protocol,
)
from app.documents.service import (
    accept_document_delete,
    accept_document_reindex,
    ingest_staged_upload,
    storage_unavailable_error,
)
from app.documents.validation import (
    JSON_MEDIA_TYPE,
    ValidationResult,
    file_too_large_error,
    json_protocol_error,
    max_bytes_for_extension,
    sanitize_filename,
    stage_chunks,
    stage_upload,
)
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
    return document_response(result.document, result.link, result.run, duplicate=result.duplicate)


@router.post(
    "/json",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_json_data(
    workspace: UploadWorkspaceContext,
    current_user: CurrentUser,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    storage: StorageDependency,
    scanner: ScannerDependency,
    filename: Annotated[str, Query(min_length=1, max_length=255)] = "data.json",
) -> DocumentResponse:
    """Ingest a ``retriv-json`` v1 payload sent as the request body.

    The endpoint is a thin wrapper around the file path: the body is validated,
    staged as a ``.json`` document, scanned, stored, and enqueued exactly like
    an uploaded file. That keeps deduplication, reindexing, download links and
    the worker in one place.
    """

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != JSON_MEDIA_TYPE:
        raise AppError(
            status_code=415,
            code="unsupported_media_type",
            title="Unsupported media type",
            detail="JSON data ingestion requires Content-Type: application/json.",
        )

    json_limit = max_bytes_for_extension(".json", settings)
    body = await request.body()
    if not body:
        raise AppError(
            status_code=422,
            code="empty_file",
            title="Empty file",
            detail="The JSON request body is empty.",
        )
    if len(body) > json_limit:
        raise file_too_large_error(json_limit)

    try:
        await to_thread.run_sync(
            partial(
                validate_json_protocol,
                body,
                JsonLimits.from_settings(settings),
            )
        )
    except JsonProtocolError as exc:
        raise json_protocol_error(exc) from exc

    safe_filename = sanitize_filename(filename)
    if not safe_filename.lower().endswith(".json"):
        if Path(safe_filename).suffix:
            raise AppError(
                status_code=422,
                code="invalid_filename",
                title="Invalid filename",
                detail="The filename for JSON data must end with .json.",
            )
        safe_filename = f"{safe_filename}.json"
    if len(safe_filename.encode("utf-8")) > 255:
        raise AppError(
            status_code=422,
            code="invalid_filename",
            title="Invalid filename",
            detail="The filename is longer than 255 bytes.",
        )

    staged = None
    try:
        async def body_chunks() -> AsyncIterator[bytes]:
            yield body

        staged = await stage_chunks(
            safe_filename,
            body_chunks(),
            settings=settings,
            max_bytes=json_limit,
        )
        result = await ingest_staged_upload(
            session=session,
            settings=settings,
            storage=storage,
            scanner=scanner,
            workspace_id=workspace.id,
            uploader_id=current_user.id,
            staged=staged,
            validation=ValidationResult(media_type=JSON_MEDIA_TYPE),
        )
    finally:
        if staged is not None:
            await to_thread.run_sync(partial(staged.path.unlink, missing_ok=True))

    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_202_ACCEPTED
    return document_response(result.document, result.link, result.run, duplicate=result.duplicate)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    workspace: WorkspaceContext,
    _current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DocumentListResponse:
    condition = and_(
        DocumentWorkspace.workspace_id == workspace.id,
        DocumentWorkspace.deleted_at.is_(None),
        Document.deleted_at.is_(None),
    )
    total = await session.scalar(
        select(func.count())
        .select_from(Document)
        .join(DocumentWorkspace, DocumentWorkspace.document_id == Document.id)
        .where(condition)
    )
    result = await session.execute(
        select(Document, DocumentWorkspace, ProcessingRun)
        .join(DocumentWorkspace, DocumentWorkspace.document_id == Document.id)
        .outerjoin(ProcessingRun, ProcessingRun.id == Document.active_processing_run_id)
        .where(condition)
        .order_by(DocumentWorkspace.created_at.desc(), Document.id)
        .offset(offset)
        .limit(limit)
    )
    return DocumentListResponse(
        items=[document_response(document, link, run) for document, link, run in result.all()],
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
    run = await load_active_run(session, document.document)
    return document_response(document.document, document.link, run)


@router.get(
    "/{document_id}/chunks/{ordinal}/regions",
    response_model=ChunkRegionsResponse,
)
async def get_chunk_regions(
    document: DocumentContext,
    ordinal: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChunkRegionsResponse:
    """The source regions a chunk was extracted from.

    The chat citation already carries the document and chunk ordinal. This
    route keeps the viewer independent of retrieval's row shape: it can ask for
    the regions only when a PDF is actually opened.
    """
    provenance = await session.scalar(
        select(DocumentChunk.provenance).where(
            DocumentChunk.document_id == document.document.id,
            DocumentChunk.processing_run_id
            == document.document.active_processing_run_id,
            DocumentChunk.ordinal == ordinal,
        )
    )
    if provenance is None:
        raise AppError(
            status_code=404,
            code="chunk_not_found",
            title="Chunk not found",
            detail="The cited chunk does not exist in the active document run.",
        )
    regions: list[DocumentSourceRegion] = []
    for entry in provenance.get("regions", []):
        if not isinstance(entry, dict):
            continue
        page_no = entry.get("page_no")
        bbox = entry.get("bbox")
        if (
            not isinstance(page_no, int)
            or page_no < 1
            or not isinstance(bbox, (list, tuple))
            or len(bbox) != 4
            or not all(isinstance(value, (int, float)) for value in bbox)
        ):
            continue
        x0, y0, x1, y1 = (float(value) for value in bbox)
        if x1 <= x0 or y1 <= y0:
            continue
        regions.append(
            DocumentSourceRegion(
                page_no=page_no,
                bbox=(x0, y0, x1, y1),
                kind=str(entry.get("kind", "text")),
                ref=str(entry.get("ref", "")),
            )
        )
    return ChunkRegionsResponse(regions=regions)


@router.get("/{document_id}/download")
async def download_document(
    document: DownloadableDocumentContext,
    storage: StorageDependency,
    disposition: Annotated[Literal["inline", "attachment"], Query()] = "attachment",
) -> StreamingResponse:
    """Stream the original file, either as a download or for inline viewing.

    ``inline`` is what the dashboard's preview needs: the browser renders the
    PDF instead of saving it. It also earns a short private cache and an
    ``ETag`` -- the bytes under a given sha256 never change, so a revalidation
    costs a 304 rather than another full stream out of the object store.
    """
    if document.deleted_at is not None:
        raise AppError(
            status_code=409,
            code="document_deletion_in_progress",
            title="Document deletion in progress",
            detail="The document cannot be downloaded while deletion is in progress.",
        )
    try:
        stream = await to_thread.run_sync(
            partial(storage.open_object, bucket=document.bucket, key=document.object_key)
        )
    except ObjectNotFoundError as exc:
        raise AppError(
            status_code=502,
            code="stored_object_missing",
            title="Stored object missing",
            detail="Document metadata exists but its stored object is unavailable.",
        ) from exc
    except StorageError as exc:
        raise storage_unavailable_error("The document could not be downloaded.") from exc

    inline = disposition == "inline"
    headers = {
        "Content-Length": str(document.size_bytes),
        "Content-Disposition": content_disposition(document.original_filename, inline=inline),
        "Cache-Control": "private, max-age=300" if inline else "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if inline:
        headers["ETag"] = f'"{document.sha256}"'
    return StreamingResponse(
        iter_object(stream),
        media_type=document.detected_media_type,
        headers=headers,
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
        document_id=document.document.id,
        workspace_id=document.link.workspace_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{document_id}/reindex",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_document(
    document: OwnerDocumentContext,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentResponse:
    """Re-run extraction with the current pipeline, on the owner's order.

    The raw file never moves: the worker fetches it again, re-extracts,
    re-describes the pictures and re-embeds, and the new run supersedes the
    old one in place. This is how a document gains whatever the pipeline
    has learned since it was first uploaded -- its images, its tables, its
    reading order.
    """
    run = await accept_document_reindex(
        session=session,
        document_id=document.document.id,
        workspace_id=document.link.workspace_id,
        settings=settings,
    )
    if run is None:
        raise AppError(
            status_code=404,
            code="document_not_found",
            title="Document not found",
            detail="The requested document does not exist in this workspace.",
        )
    return document_response(document.document, document.link, run)


def document_response(
    document: Document,
    link: DocumentWorkspace,
    run: ProcessingRun | None = None,
    *,
    duplicate: bool = False,
) -> DocumentResponse:
    if link.deleted_at is not None and document.deleted_at is None:
        status = "deleting"
    else:
        status = derived_document_status(document, run)
    return DocumentResponse(
        id=document.id,
        workspace_id=link.workspace_id,
        original_filename=link.filename,
        detected_media_type=document.detected_media_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        status=status,
        row_count=run.row_count if run is not None else 0,
        created_by_user_id=link.created_by_user_id,
        created_at=link.created_at,
        duplicate=duplicate,
    )


def content_disposition(filename: str, *, inline: bool = False) -> str:
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename).strip("._") or "document"
    encoded_filename = quote(filename, safe="")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_filename}"
