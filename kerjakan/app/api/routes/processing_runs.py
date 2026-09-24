from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import ProcessingApiClient
from app.api.schemas import (
    ActiveProcessingRunResponse,
    ProcessingRunListResponse,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import WorkspaceMembership
from app.db.session import get_session
from app.security.auth import CurrentUser
from app.security.authorization import ROLE_PERMISSIONS

router = APIRouter(prefix="/v1/processing-runs", tags=["processing-runs"])
PROCESSING_RUN_WINDOW = timedelta(hours=1)


@router.get("/active", response_model=ActiveProcessingRunResponse)
async def get_latest_active_processing_run(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: ProcessingApiClient,
) -> ActiveProcessingRunResponse:
    workspace_ids = await readable_workspace_ids(session, current_user.id)
    if not workspace_ids:
        return ActiveProcessingRunResponse(item=None)

    try:
        upstream = await client.post(
            "/internal/processing-runs/active",
            headers=service_headers(settings),
            json={"workspace_ids": [str(workspace_id) for workspace_id in workspace_ids]},
        )
    except httpx.TimeoutException as exc:
        raise processing_service_unavailable_error() from exc
    except httpx.HTTPError as exc:
        raise processing_service_unavailable_error() from exc
    if upstream.status_code != 200:
        raise processing_service_unavailable_error()
    try:
        result = ActiveProcessingRunResponse.model_validate(upstream.json())
    except (ValueError, TypeError) as exc:
        raise processing_service_unavailable_error() from exc
    if result.item is not None and result.item.workspace_id not in workspace_ids:
        raise processing_service_unavailable_error()
    return result


@router.get("/recent", response_model=ProcessingRunListResponse)
async def list_recent_processing_runs(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: ProcessingApiClient,
) -> ProcessingRunListResponse:
    workspace_ids = await readable_workspace_ids(session, current_user.id)
    if not workspace_ids:
        return ProcessingRunListResponse(
            items=[],
            total=0,
            since=datetime.now(UTC) - PROCESSING_RUN_WINDOW,
        )

    try:
        upstream = await client.post(
            "/internal/processing-runs/recent",
            headers=service_headers(settings),
            json={"workspace_ids": [str(workspace_id) for workspace_id in workspace_ids]},
        )
    except httpx.TimeoutException as exc:
        raise processing_service_unavailable_error() from exc
    except httpx.HTTPError as exc:
        raise processing_service_unavailable_error() from exc
    if upstream.status_code != 200:
        raise processing_service_unavailable_error()
    try:
        result = ProcessingRunListResponse.model_validate(upstream.json())
    except (ValueError, TypeError) as exc:
        raise processing_service_unavailable_error() from exc
    if any(item.workspace_id not in workspace_ids for item in result.items):
        raise processing_service_unavailable_error()
    return result


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


def service_headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.processing_api_token.get_secret_value()}"
    }


def processing_service_unavailable_error() -> AppError:
    return AppError(
        status_code=503,
        code="processing_service_unavailable",
        title="Processing status unavailable",
        detail="Processing status could not be loaded. Try again shortly.",
    )
