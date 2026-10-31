from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import DocumentSearchHit, DocumentSearchResponse
from app.db.session import get_session
from app.documents.search import find_documents
from app.routes.processing_runs import readable_workspace_ids
from app.security.auth import CurrentUser

router = APIRouter(prefix="/v1/documents", tags=["documents"])


@router.get("/search", response_model=DocumentSearchResponse)
async def search_documents(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    workspace_id: Annotated[UUID | None, Query()] = None,
) -> DocumentSearchResponse:
    """Documents whose filename contains ``q``, across every readable workspace.

    The scope is the caller's own memberships with read access, so a search
    can never name a file in a workspace the caller cannot open. Narrowing by
    ``workspace_id`` filters that scope; a workspace outside it simply matches
    nothing, which is the same answer the per-workspace route gives.
    """
    workspace_ids = await readable_workspace_ids(session, current_user.id)
    if workspace_id is not None:
        workspace_ids = [candidate for candidate in workspace_ids if candidate == workspace_id]
    summaries, total = await find_documents(
        session,
        workspace_ids,
        pattern=q.strip(),
        limit=limit,
    )
    return DocumentSearchResponse(
        items=[
            DocumentSearchHit(
                document_id=summary.document_id,
                workspace_id=summary.workspace_id,
                workspace_name=summary.workspace_name,
                filename=summary.filename,
                media_type=summary.media_type,
                size_bytes=summary.size_bytes,
                status=summary.status,
                row_count=summary.row_count,
                created_at=summary.created_at,
            )
            for summary in summaries
        ],
        total=total,
    )
