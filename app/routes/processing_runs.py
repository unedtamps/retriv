from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
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
    JOB_ACCESS,
    NON_TERMINAL_RUN_STATUSES,
    DocumentWorkspace,
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

    link_workspace, link_filename = _link_columns(workspace_ids)
    result = await session.execute(
        select(ProcessingRun, link_workspace, link_filename)
        .where(
            link_workspace.is_not(None),
            ProcessingRun.job_kind != JOB_ACCESS,
            ProcessingRun.status.in_(NON_TERMINAL_RUN_STATUSES),
        )
        .order_by(ProcessingRun.created_at.desc(), ProcessingRun.id.desc())
        .limit(1)
    )
    row = result.one_or_none()
    return ActiveProcessingRunResponse(
        item=processing_run_summary(row[0], row[1], row[2])
        if row is not None
        else None
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
    link_workspace, link_filename = _link_columns(workspace_ids)
    conditions = (
        link_workspace.is_not(None),
        ProcessingRun.job_kind != JOB_ACCESS,
        ProcessingRun.created_at >= since,
    )
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(ProcessingRun)
            .where(*conditions)
        )
        or 0
    )
    result = await session.execute(
        select(ProcessingRun, link_workspace, link_filename)
        .where(*conditions)
        .order_by(ProcessingRun.created_at.desc(), ProcessingRun.id.desc())
    )
    return ProcessingRunListResponse(
        items=[
            processing_run_summary(run, workspace_id, filename)
            for run, workspace_id, filename in result.all()
        ],
        total=total,
        since=since,
    )


def _link_columns(workspace_ids: list[UUID]) -> tuple[Any, Any]:
    """One deterministic workspace link per document, for run labels.

    A canonical document may be linked to several of the caller's
    workspaces. The run is still one row; picking the lowest workspace id
    keeps the label stable without duplicating the run in the feed.
    """
    workspace = (
        select(DocumentWorkspace.workspace_id)
        .where(
            DocumentWorkspace.document_id == ProcessingRun.document_id,
            DocumentWorkspace.workspace_id.in_(workspace_ids),
            DocumentWorkspace.deleted_at.is_(None),
        )
        .order_by(DocumentWorkspace.workspace_id)
        .limit(1)
        .scalar_subquery()
    )
    filename = (
        select(DocumentWorkspace.filename)
        .where(
            DocumentWorkspace.document_id == ProcessingRun.document_id,
            DocumentWorkspace.workspace_id.in_(workspace_ids),
            DocumentWorkspace.deleted_at.is_(None),
        )
        .order_by(DocumentWorkspace.workspace_id)
        .limit(1)
        .scalar_subquery()
    )
    return workspace, filename


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
    run: ProcessingRun, workspace_id: UUID, filename: str | None
) -> ProcessingRunSummary:
    return ProcessingRunSummary(
        id=run.id,
        workspace_id=workspace_id,
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
