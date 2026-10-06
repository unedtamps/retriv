from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ActiveProcessingRunResponse,
    ProcessingRunListResponse,
    ProcessingRunSummary,
)
from app.db.models import (
    NON_TERMINAL_RUN_STATUSES,
    Document,
    ProcessingRun,
    WorkspaceMembership,
)
from app.db.session import get_session
from app.security.auth import CurrentUser
from app.security.authorization import ROLE_PERMISSIONS

router = APIRouter(prefix="/v1/processing-runs", tags=["processing-runs"])
PROCESSING_RUN_WINDOW = timedelta(hours=1)


@router.get("/active", response_model=ActiveProcessingRunResponse)
async def get_latest_active_processing_run(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ActiveProcessingRunResponse:
    """The caller's newest in-flight run, across every readable workspace.

    A direct query now. The scope is derived from the caller's own
    memberships, so there is no upstream service to trust and no
    revalidation of its answer: the filter and the returned rows are
    built from the same set.
    """
    workspace_ids = await readable_workspace_ids(session, current_user.id)
    if not workspace_ids:
        return ActiveProcessingRunResponse(item=None)

    result = await session.execute(
        select(ProcessingRun, Document.original_filename)
        .outerjoin(Document, Document.id == ProcessingRun.document_id)
        .where(
            ProcessingRun.workspace_id.in_(workspace_ids),
            ProcessingRun.status.in_(NON_TERMINAL_RUN_STATUSES),
        )
        .order_by(ProcessingRun.created_at.desc(), ProcessingRun.id.desc())
        .limit(1)
    )
    row = result.one_or_none()
    return ActiveProcessingRunResponse(
        item=processing_run_summary(row[0], row[1]) if row is not None else None
    )


@router.get("/recent", response_model=ProcessingRunListResponse)
async def list_recent_processing_runs(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProcessingRunListResponse:
    workspace_ids = await readable_workspace_ids(session, current_user.id)
    if not workspace_ids:
        return ProcessingRunListResponse(
            items=[],
            total=0,
            since=datetime.now(UTC) - PROCESSING_RUN_WINDOW,
        )

    since = datetime.now(UTC) - PROCESSING_RUN_WINDOW
    conditions = (
        ProcessingRun.workspace_id.in_(workspace_ids),
        ProcessingRun.created_at >= since,
    )
    total = int(
        await session.scalar(
            select(func.count()).select_from(ProcessingRun).where(*conditions)
        )
        or 0
    )
    result = await session.execute(
        select(ProcessingRun, Document.original_filename)
        .outerjoin(Document, Document.id == ProcessingRun.document_id)
        .where(*conditions)
        .order_by(ProcessingRun.created_at.desc(), ProcessingRun.id.desc())
    )
    return ProcessingRunListResponse(
        items=[processing_run_summary(run, filename) for run, filename in result.all()],
        total=total,
        since=since,
    )


async def readable_workspace_ids(
    session: AsyncSession, user_id: UUID
) -> list[UUID]:
    readable_roles = [
        role for role, permissions in ROLE_PERMISSIONS.items() if "read" in permissions
    ]
    result = await session.scalars(
        select(WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.role.in_(readable_roles),
        )
        .order_by(WorkspaceMembership.workspace_id)
    )
    return list(result.all())


def processing_run_summary(
    run: ProcessingRun, filename: str | None
) -> ProcessingRunSummary:
    return ProcessingRunSummary(
        id=run.id,
        workspace_id=run.workspace_id,
        document_id=run.document_id,
        filename=filename,
        job_kind=run.job_kind,
        status=run.status,
        attempts=run.attempts,
        chunk_count=run.chunk_count,
        embedded_chunk_count=run.embedded_chunk_count,
        row_count=run.row_count,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )
