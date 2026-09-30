from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.dependencies import AnswerHarnessDependency
from app.agent.retrieval import OutOfScopeSearchResult, RetrievalUnavailable
from app.agent.scope import MAX_SCOPE_WORKSPACES
from app.api.schemas import AgentAskResponse, AskContextRequest
from app.core.errors import AppError
from app.db.models import WorkspaceMembership
from app.db.session import get_session
from app.security.auth import CurrentUser
from app.security.authorization import ROLE_PERMISSIONS

router = APIRouter(tags=["agent"])


@router.post("/v1/agent/ask", response_model=AgentAskResponse)
async def ask_context(
    payload: AskContextRequest,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    harness: AnswerHarnessDependency,
    response: Response,
) -> AgentAskResponse:
    """Answer a question from the caller's searchable workspaces.

    The harness is called directly. The scope is derived from the
    caller's memberships here, and the retriever re-checks it against
    every Qdrant point and every PostgreSQL row, so an out-of-scope read
    fails closed rather than reaching the model.
    """
    workspace_ids = await searchable_workspace_ids(session, current_user.id)
    if not workspace_ids:
        raise context_access_required_error()
    if len(workspace_ids) > MAX_SCOPE_WORKSPACES:
        raise context_scope_too_large_error()

    try:
        result = await harness.answer(payload.question, workspace_ids, [])
    except OutOfScopeSearchResult as exc:
        raise out_of_scope_error() from exc
    except RetrievalUnavailable as exc:
        raise answer_unavailable_error(
            "The authorized sources could not be read. Try again shortly."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - mapped to a problem response
        raise answer_unavailable_error(
            "The answer could not be generated. Try again shortly."
        ) from exc

    # Defence in depth. The retriever already rejects an out-of-scope
    # point, so reaching this means the harness itself drifted; refuse
    # the whole answer rather than returning part of it.
    if any(source.workspace_id not in workspace_ids for source in result.sources):
        raise out_of_scope_error()

    response.headers["Cache-Control"] = "no-store"
    return AgentAskResponse(answer=result.answer, sources=result.sources)


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


def context_access_required_error() -> AppError:
    return AppError(
        status_code=403,
        code="context_access_required",
        title="Workspace context access required",
        detail=(
            "Join a workspace with permission to search its context before asking a question."
        ),
    )


def context_scope_too_large_error() -> AppError:
    return AppError(
        status_code=422,
        code="context_scope_too_large",
        title="Workspace context scope is too large",
        detail=(
            "Ask an administrator to reduce your searchable workspace memberships "
            "before using workspace context."
        ),
    )


def out_of_scope_error() -> AppError:
    return AppError(
        status_code=403,
        code="context_scope_violation",
        title="Context scope violation",
        detail="A source outside your authorized workspaces was requested.",
    )


def answer_unavailable_error(detail: str) -> AppError:
    return AppError(
        status_code=503,
        code="answer_service_unavailable",
        title="Answer service unavailable",
        detail=detail,
    )
