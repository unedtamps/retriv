from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_internal_api_token
from app.api.schemas import (
    ChunkListResponse,
    ChunkResponse,
    DocumentStatusResponse,
    ProcessingRunResponse,
)
from app.core.errors import AppError
from app.db.models import DocumentChunk, ProcessingRun, SourceDocument
from app.db.session import get_session

router = APIRouter(
    prefix="/internal/workspaces/{workspace_id}/documents",
    tags=["documents"],
    dependencies=[Depends(require_internal_api_token)],
)


@router.get("/{document_id}/status", response_model=DocumentStatusResponse)
async def get_document_status(
    workspace_id: UUID,
    document_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentStatusResponse:
    document = await get_document(session, workspace_id, document_id)
    result = await session.scalars(
        select(ProcessingRun)
        .where(
            ProcessingRun.workspace_id == workspace_id,
            ProcessingRun.document_id == document_id,
        )
        .order_by(ProcessingRun.created_at, ProcessingRun.id)
    )
    return DocumentStatusResponse(
        workspace_id=document.workspace_id,
        document_id=document.document_id,
        original_filename=document.original_filename,
        media_type=document.media_type,
        sha256=document.sha256,
        active_processing_run_id=document.active_processing_run_id,
        deleted_at=document.deleted_at,
        runs=[run_response(run) for run in result.all()],
    )


@router.get("/{document_id}/chunks", response_model=ChunkListResponse)
async def list_document_chunks(
    workspace_id: UUID,
    document_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ChunkListResponse:
    document = await get_document(session, workspace_id, document_id)
    if document.active_processing_run_id is None:
        return ChunkListResponse(items=[], total=0, offset=offset, limit=limit)
    condition = (
        DocumentChunk.workspace_id == workspace_id,
        DocumentChunk.document_id == document_id,
        DocumentChunk.processing_run_id == document.active_processing_run_id,
    )
    total = await session.scalar(select(func.count()).select_from(DocumentChunk).where(*condition))
    result = await session.scalars(
        select(DocumentChunk)
        .where(*condition)
        .order_by(DocumentChunk.ordinal)
        .offset(offset)
        .limit(limit)
    )
    return ChunkListResponse(
        items=[chunk_response(chunk) for chunk in result.all()],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.get("/{document_id}/chunks/{chunk_id}", response_model=ChunkResponse)
async def get_document_chunk(
    workspace_id: UUID,
    document_id: UUID,
    chunk_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChunkResponse:
    document = await get_document(session, workspace_id, document_id)
    chunk = await session.scalar(
        select(DocumentChunk).where(
            DocumentChunk.id == chunk_id,
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.document_id == document_id,
            DocumentChunk.processing_run_id == document.active_processing_run_id,
        )
    )
    if chunk is None:
        raise not_found_error("chunk")
    return chunk_response(chunk)


async def get_document(
    session: AsyncSession, workspace_id: UUID, document_id: UUID
) -> SourceDocument:
    document = await session.get(
        SourceDocument,
        {"workspace_id": workspace_id, "document_id": document_id},
    )
    if document is None:
        raise not_found_error("document")
    return document


def run_response(run: ProcessingRun) -> ProcessingRunResponse:
    return ProcessingRunResponse(
        id=run.id,
        job_kind=run.job_kind,
        pipeline_version=run.pipeline_version,
        status=run.status,
        attempts=run.attempts,
        chunk_count=run.chunk_count,
        embedded_chunk_count=run.embedded_chunk_count,
        embedding_profile=run.embedding_profile,
        embedding_model=run.embedding_model,
        embedding_dimensions=run.embedding_dimensions,
        last_error=run.last_error,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


def chunk_response(chunk: DocumentChunk) -> ChunkResponse:
    return ChunkResponse(
        id=chunk.id,
        workspace_id=chunk.workspace_id,
        document_id=chunk.document_id,
        processing_run_id=chunk.processing_run_id,
        ordinal=chunk.ordinal,
        text=chunk.text,
        token_count=chunk.token_count,
        section_path=chunk.section_path,
        page_numbers=chunk.page_numbers,
        provenance=chunk.provenance,
        content_hash=chunk.content_hash,
    )


def not_found_error(resource: str) -> AppError:
    return AppError(
        status_code=404,
        code=f"{resource}_not_found",
        title=f"{resource.title()} not found",
        detail=f"The requested {resource} does not exist in this workspace.",
    )
