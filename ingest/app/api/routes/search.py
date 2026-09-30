from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_embedding_provider,
    get_vector_store,
    require_internal_api_token,
)
from app.api.schemas import SemanticSearchRequest, SemanticSearchResponse, SemanticSearchResult
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import (
    EMBEDDING_INDEXED,
    ChunkEmbedding,
    DocumentChunk,
    SourceDocument,
)
from app.db.session import get_session
from app.embeddings.base import (
    EmbeddingProvider,
    EmbeddingProviderError,
    VectorMatch,
    VectorStore,
    VectorStoreError,
)

router = APIRouter(
    prefix="/internal/context",
    tags=["semantic-search"],
    dependencies=[Depends(require_internal_api_token)],
)


@router.post("/search", response_model=SemanticSearchResponse)
async def semantic_search(
    request: SemanticSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    embedding_provider: Annotated[EmbeddingProvider, Depends(get_embedding_provider)],
    vector_store: Annotated[VectorStore, Depends(get_vector_store)],
) -> SemanticSearchResponse:
    try:
        vectors = await embedding_provider.embed([request.query])
        if len(vectors) != 1:
            raise EmbeddingProviderError(
                "Embedding response count does not match the query",
                retryable=False,
            )
        matches = await vector_store.search(
            vectors[0],
            workspace_ids=request.workspace_ids,
            embedding_profile=settings.embedding_profile,
            limit=request.limit,
        )
    except EmbeddingProviderError as exc:
        raise AppError(
            status_code=503,
            code="query_embedding_unavailable",
            title="Semantic search unavailable",
            detail="The query embedding service is unavailable.",
        ) from exc
    except VectorStoreError as exc:
        raise AppError(
            status_code=503,
            code="vector_search_unavailable",
            title="Semantic search unavailable",
            detail="The vector search service is unavailable.",
        ) from exc
    allowed_workspaces = {str(workspace_id) for workspace_id in request.workspace_ids}
    safe_matches: list[tuple[VectorMatch, UUID]] = []
    for match in matches:
        chunk_id = payload_chunk_id(match)
        if (
            chunk_id is not None
            and payload_within_scope(
                match, allowed_workspaces, settings.embedding_profile
            )
        ):
            safe_matches.append((match, chunk_id))
    if not safe_matches:
        return SemanticSearchResponse(items=[])
    candidate_chunk_ids = [chunk_id for _, chunk_id in safe_matches]

    chunks = await session.execute(
        select(DocumentChunk, ChunkEmbedding, SourceDocument)
        .join(
            ChunkEmbedding,
            and_(
                ChunkEmbedding.chunk_id == DocumentChunk.id,
                ChunkEmbedding.processing_run_id == DocumentChunk.processing_run_id,
            ),
        )
        .join(
            SourceDocument,
            and_(
                SourceDocument.workspace_id == DocumentChunk.workspace_id,
                SourceDocument.document_id == DocumentChunk.document_id,
            ),
        )
        .where(
            DocumentChunk.id.in_(candidate_chunk_ids),
            DocumentChunk.workspace_id.in_(request.workspace_ids),
            SourceDocument.deleted_at.is_(None),
            SourceDocument.active_processing_run_id == DocumentChunk.processing_run_id,
            ChunkEmbedding.embedding_profile == settings.embedding_profile,
            ChunkEmbedding.model == settings.embedding_model,
            ChunkEmbedding.dimensions == settings.embedding_dimensions,
            ChunkEmbedding.status == EMBEDDING_INDEXED,
            ChunkEmbedding.content_hash == DocumentChunk.content_hash,
        )
    )
    rows_by_id = {
        chunk.id: (chunk, embedding, document)
        for chunk, embedding, document in chunks.all()
    }

    results: list[SemanticSearchResult] = []
    for match, chunk_id in safe_matches:
        row = rows_by_id.get(chunk_id)
        if row is None:
            continue
        chunk, embedding, document = row
        if not payload_matches_row(match, chunk, embedding):
            continue
        results.append(
            SemanticSearchResult(
                workspace_id=chunk.workspace_id,
                document_id=chunk.document_id,
                processing_run_id=chunk.processing_run_id,
                chunk_id=chunk.id,
                ordinal=chunk.ordinal,
                score=match.score,
                content_hash=chunk.content_hash,
                filename=document.original_filename,
                section_path=chunk.section_path,
                page_numbers=chunk.page_numbers,
                text=chunk.text,
            )
        )
    return SemanticSearchResponse(items=results)


def payload_within_scope(
    match: VectorMatch, allowed_workspaces: set[str], embedding_profile: str
) -> bool:
    return (
        match.payload.get("workspace_id") in allowed_workspaces
        and match.payload.get("embedding_profile") == embedding_profile
    )


def payload_chunk_id(match: VectorMatch) -> UUID | None:
    try:
        return UUID(str(match.payload["chunk_id"]))
    except (KeyError, ValueError):
        return None


def payload_matches_row(
    match: VectorMatch, chunk: DocumentChunk, embedding: ChunkEmbedding
) -> bool:
    return (
        match.id == embedding.qdrant_point_id
        and match.payload.get("workspace_id") == str(chunk.workspace_id)
        and match.payload.get("document_id") == str(chunk.document_id)
        and match.payload.get("processing_run_id") == str(chunk.processing_run_id)
        and match.payload.get("chunk_id") == str(chunk.id)
        and match.payload.get("content_hash") == chunk.content_hash
        and match.payload.get("embedding_profile") == embedding.embedding_profile
    )
