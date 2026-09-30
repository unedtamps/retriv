from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_internal_api_token
from app.api.schemas import (
    ActiveProcessingRunResponse,
    ProcessingRunListResponse,
    ProcessingRunScopeRequest,
    ProcessingRunSummary,
)
from app.db.models import NON_TERMINAL_RUN_STATUSES, ProcessingRun, SourceDocument
from app.db.session import get_session

router = APIRouter(
    prefix="/internal/processing-runs",
    tags=["processing-runs"],
    dependencies=[Depends(require_internal_api_token)],
)
@router.post("/active", response_model=ActiveProcessingRunResponse)
async def get_latest_active_processing_run(
    payload: ProcessingRunScopeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ActiveProcessingRunResponse:
    workspace_ids = payload.workspace_ids
    result = await session.execute(
        select(ProcessingRun, SourceDocument.original_filename)
        .outerjoin(
            SourceDocument,
            and_(
                SourceDocument.workspace_id == ProcessingRun.workspace_id,
                SourceDocument.document_id == ProcessingRun.document_id,
            ),
        )
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


@router.post("/recent", response_model=ProcessingRunListResponse)
async def list_recent_processing_runs(
    payload: ProcessingRunScopeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProcessingRunListResponse:
    workspace_ids = payload.workspace_ids
    since = datetime.now(UTC) - timedelta(hours=1)
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
        select(ProcessingRun, SourceDocument.original_filename)
        .outerjoin(
            SourceDocument,
            and_(
                SourceDocument.workspace_id == ProcessingRun.workspace_id,
                SourceDocument.document_id == ProcessingRun.document_id,
            ),
        )
        .where(*conditions)
        .order_by(ProcessingRun.created_at.desc(), ProcessingRun.id.desc())
    )
    return ProcessingRunListResponse(
        items=[processing_run_summary(run, filename) for run, filename in result.all()],
        total=total,
        since=since,
    )


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
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )
