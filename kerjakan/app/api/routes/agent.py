from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AgentClient
from app.api.schemas import AgentAnswerResponse, AgentAskResponse, AskContextRequest
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import WorkspaceMembership
from app.db.session import get_session
from app.security.auth import CurrentUser
from app.security.authorization import ROLE_PERMISSIONS
from app.security.context_delegation import (
    MAX_SCOPE_WORKSPACES,
    create_context_delegation,
)

router = APIRouter(tags=["agent"])


@router.post("/v1/agent/ask", response_model=AgentAskResponse)
async def ask_context(
    payload: AskContextRequest,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: AgentClient,
    response: Response,
) -> AgentAskResponse:
    workspace_ids = await searchable_workspace_ids(session, current_user.id)
    if not workspace_ids:
        raise AppError(
            status_code=403,
            code="context_access_required",
            title="Workspace context access required",
            detail=(
                "Join a workspace with permission to search its context before asking a question."
            ),
        )
    if len(workspace_ids) > MAX_SCOPE_WORKSPACES:
        raise AppError(
            status_code=422,
            code="context_scope_too_large",
            title="Workspace context scope is too large",
            detail=(
                "Ask an administrator to reduce your searchable workspace memberships "
                "before using workspace context."
            ),
        )

    delegation = create_context_delegation(current_user, workspace_ids, settings)
    try:
        upstream = await client.post(
            f"{settings.agent_base_url}/v1/answer",
            headers={
                "Authorization": f"Bearer {settings.agent_api_token.get_secret_value()}"
            },
            json={"question": payload.question, "scope_token": delegation},
        )
    except httpx.TimeoutException as exc:
        raise agent_unavailable_error("The answer service timed out.") from exc
    except httpx.HTTPError as exc:
        raise agent_unavailable_error("The answer service is unavailable.") from exc
    if upstream.status_code != 200:
        raise agent_unavailable_error(
            "The answer service could not complete the request."
        )
    try:
        agent_response = AgentAnswerResponse.model_validate(upstream.json())
    except (ValueError, TypeError) as exc:
        raise agent_unavailable_error(
            "The answer service returned an invalid response."
        ) from exc

    if any(
        source.workspace_id not in workspace_ids for source in agent_response.sources
    ):
        raise agent_unavailable_error(
            "The answer service returned an out-of-scope source."
        )
    response.headers["Cache-Control"] = "no-store"
    return AgentAskResponse(
        answer=agent_response.answer,
        sources=agent_response.sources,
    )


async def searchable_workspace_ids(session: AsyncSession, user_id: UUID) -> list[UUID]:
    roles = [
        role
        for role, permissions in ROLE_PERMISSIONS.items()
        if "search_context" in permissions
    ]
    result = await session.scalars(
        select(WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.role.in_(roles),
        )
        .order_by(WorkspaceMembership.workspace_id)
    )
    return list(result.all())


def agent_unavailable_error(detail: str) -> AppError:
    return AppError(
        status_code=503,
        code="answer_service_unavailable",
        title="Answer service unavailable",
        detail=detail,
    )
