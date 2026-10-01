from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import MemberManagementWorkspaceContext
from app.api.schemas import (
    OwnerMemberRequest,
    WorkspaceListResponse,
    WorkspaceMemberCandidateListResponse,
    WorkspaceMemberCandidateResponse,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
    WorkspaceRole,
)
from app.core.errors import AppError
from app.db.models import User, Workspace, WorkspaceMembership
from app.db.session import get_session
from app.security.auth import CurrentUser
from app.security.authorization import ROLE_PERMISSIONS

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


@router.get("", response_model=WorkspaceListResponse)
async def list_workspaces(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> WorkspaceListResponse:
    total = await session.scalar(select(func.count()).select_from(Workspace))
    result = await session.execute(
        select(Workspace, WorkspaceMembership)
        .outerjoin(
            WorkspaceMembership,
            and_(
                WorkspaceMembership.workspace_id == Workspace.id,
                WorkspaceMembership.user_id == current_user.id,
            ),
        )
        .order_by(Workspace.name, Workspace.id)
        .offset(offset)
        .limit(limit)
    )
    return WorkspaceListResponse(
        items=[workspace_response(workspace, membership) for workspace, membership in result.all()],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{workspace_id}/member-candidates",
    response_model=WorkspaceMemberCandidateListResponse,
)
async def list_workspace_member_candidates(
    _workspace: MemberManagementWorkspaceContext,
    workspace_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    search: Annotated[str, Query(min_length=2, max_length=255)],
    limit: Annotated[int, Query(ge=1, le=20)] = 20,
) -> WorkspaceMemberCandidateListResponse:
    normalized_search = search.strip().lower()
    if len(normalized_search) < 2:
        raise AppError(
            status_code=422,
            code="search_term_too_short",
            title="Search term too short",
            detail="Enter at least two non-whitespace characters.",
        )

    existing_members = select(WorkspaceMembership.user_id).where(
        WorkspaceMembership.workspace_id == workspace_id
    )
    result = await session.scalars(
        select(User)
        .where(
            User.is_active.is_(True),
            User.username.contains(normalized_search, autoescape=True),
            User.id.not_in(existing_members),
        )
        .order_by(User.username, User.id)
        .limit(limit)
    )
    return WorkspaceMemberCandidateListResponse(
        items=[
            WorkspaceMemberCandidateResponse(id=user.id, username=user.username)
            for user in result.all()
        ]
    )


@router.get(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberListResponse,
)
async def list_workspace_members(
    _workspace: MemberManagementWorkspaceContext,
    session: Annotated[AsyncSession, Depends(get_session)],
    workspace_id: UUID,
) -> WorkspaceMemberListResponse:
    result = await session.execute(
        select(WorkspaceMembership, User)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(User.username, User.id)
    )
    return WorkspaceMemberListResponse(
        items=[member_response(membership, user) for membership, user in result.all()]
    )


@router.put(
    "/{workspace_id}/members/{user_id}",
    response_model=WorkspaceMemberResponse,
)
async def upsert_workspace_member(
    _workspace: MemberManagementWorkspaceContext,
    workspace_id: UUID,
    user_id: UUID,
    payload: OwnerMemberRequest,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkspaceMemberResponse:
    await session.execute(
        select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
    )
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise member_user_not_found_error()

    membership = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )
    if membership is None:
        membership = WorkspaceMembership(
            user_id=user_id,
            workspace_id=workspace_id,
            role=payload.role,
            granted_by_user_id=current_user.id,
        )
        session.add(membership)
    else:
        if membership.role == "owner":
            raise owner_assignment_error()
        membership.role = payload.role
        membership.granted_by_user_id = current_user.id

    await session.commit()
    await session.refresh(membership)
    return member_response(membership, user)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_workspace_member(
    _workspace: MemberManagementWorkspaceContext,
    workspace_id: UUID,
    user_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await session.execute(
        select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
    )
    membership = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )
    if membership is None:
        raise member_not_found_error()
    if membership.role == "owner":
        raise owner_assignment_error()

    await session.delete(membership)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def workspace_response(
    workspace: Workspace, membership: WorkspaceMembership | None
) -> WorkspaceResponse:
    role = cast(WorkspaceRole, membership.role) if membership is not None else None
    permissions = ROLE_PERMISSIONS.get(role or "", frozenset())
    return WorkspaceResponse(
        id=workspace.id,
        slug=workspace.slug,
        name=workspace.name,
        description=workspace.description,
        created_at=workspace.created_at,
        membership_role=role,
        can_upload="upload" in permissions,
        can_download="download" in permissions,
        can_search_context="search_context" in permissions,
        can_manage_members="manage_members" in permissions,
        can_delete="delete" in permissions,
    )


def member_response(
    membership: WorkspaceMembership, user: User
) -> WorkspaceMemberResponse:
    return WorkspaceMemberResponse(
        user_id=user.id,
        username=user.username,
        role=cast(WorkspaceRole, membership.role),
        granted_by_user_id=membership.granted_by_user_id,
        created_at=membership.created_at,
        updated_at=membership.updated_at,
    )


def member_user_not_found_error() -> AppError:
    return AppError(
        status_code=404,
        code="user_not_found",
        title="User not found",
        detail="The requested active user does not exist.",
    )


def member_not_found_error() -> AppError:
    return AppError(
        status_code=404,
        code="workspace_member_not_found",
        title="Workspace member not found",
        detail="The requested user is not a member of this workspace.",
    )


def owner_assignment_error() -> AppError:
    return AppError(
        status_code=409,
        code="owner_assignment_restricted",
        title="Owner assignment restricted",
        detail="Only a platform administrator can assign or remove workspace owners.",
    )
