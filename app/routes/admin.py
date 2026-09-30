import secrets
from typing import Annotated
from uuid import UUID

from anyio import to_thread
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import PlatformAdmin
from app.api.schemas import (
    AdminMemberRequest,
    AdminPasswordResponse,
    AdminUserCreateRequest,
    AdminUserListItemResponse,
    AdminUserListResponse,
    AdminUserResponse,
    WorkspaceCreateRequest,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from app.core.errors import AppError
from app.db.models import User, Workspace, WorkspaceMembership
from app.db.session import get_session
from app.routes.workspaces import member_response
from app.security.auth import hash_password

router = APIRouter(prefix="/v1/admin", tags=["administration"])


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
    search: Annotated[str | None, Query(max_length=255)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AdminUserListResponse:
    condition = None
    if search is not None and (normalized_search := search.strip()):
        condition = User.username.contains(normalized_search.lower(), autoescape=True)

    count_query = select(func.count()).select_from(User)
    users_query = select(User).order_by(User.username, User.id).offset(offset).limit(limit)
    if condition is not None:
        count_query = count_query.where(condition)
        users_query = users_query.where(condition)

    total = await session.scalar(count_query)
    users = await session.scalars(users_query)
    return AdminUserListResponse(
        items=[
            AdminUserListItemResponse(
                id=user.id,
                username=user.username,
                role=user.role,
                is_active=user.is_active,
                created_at=user.created_at,
            )
            for user in users.all()
        ],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.post("/users", response_model=AdminUserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreateRequest,
    response: Response,
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminUserResponse:
    existing = await session.scalar(select(User).where(User.username == payload.username))
    if existing is not None:
        raise user_conflict_error()

    initial_password = secrets.token_urlsafe(24)
    password_hash = await to_thread.run_sync(hash_password, initial_password)
    user = User(
        username=payload.username,
        password_hash=password_hash,
        role=payload.role,
        is_active=True,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise user_conflict_error() from exc

    response.headers["Cache-Control"] = "no-store"
    return AdminUserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        initial_password=initial_password,
    )


@router.post("/users/{user_id}/password", response_model=AdminPasswordResponse)
async def reset_user_password(
    user_id: UUID,
    response: Response,
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminPasswordResponse:
    user = await session.get(User, user_id)
    if user is None:
        raise user_not_found_error()

    initial_password = secrets.token_urlsafe(24)
    user.password_hash = await to_thread.run_sync(hash_password, initial_password)
    user.auth_version += 1
    await session.commit()

    response.headers["Cache-Control"] = "no-store"
    return AdminPasswordResponse(user_id=user.id, initial_password=initial_password)


@router.post("/workspaces", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkspaceResponse:
    owner = await session.get(User, payload.owner_user_id)
    if owner is None or not owner.is_active:
        raise user_not_found_error()

    workspace = Workspace(
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
    )
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMembership(
            user_id=owner.id,
            workspace_id=workspace.id,
            role="owner",
            granted_by_user_id=_admin.id,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise workspace_conflict_error() from exc

    return WorkspaceResponse(
        id=workspace.id,
        slug=workspace.slug,
        name=workspace.name,
        description=workspace.description,
        created_at=workspace.created_at,
        membership_role=None,
        can_upload=False,
        can_download=False,
        can_search_context=False,
        can_manage_members=False,
    )


@router.get(
    "/workspaces/{workspace_id}/members",
    response_model=WorkspaceMemberListResponse,
)
async def list_members(
    workspace_id: UUID,
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkspaceMemberListResponse:
    if await session.get(Workspace, workspace_id) is None:
        raise workspace_not_found_error()
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
    "/workspaces/{workspace_id}/members/{user_id}",
    response_model=WorkspaceMemberResponse,
)
async def set_member_role(
    workspace_id: UUID,
    user_id: UUID,
    payload: AdminMemberRequest,
    admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkspaceMemberResponse:
    workspace = await session.scalar(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    if workspace is None:
        raise workspace_not_found_error()
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise user_not_found_error()

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
            granted_by_user_id=admin.id,
        )
        session.add(membership)
    else:
        await ensure_not_last_owner(session, membership, payload.role)
        membership.role = payload.role
        membership.granted_by_user_id = admin.id

    await session.commit()
    await session.refresh(membership)
    return member_response(membership, user)


@router.delete(
    "/workspaces/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    workspace_id: UUID,
    user_id: UUID,
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    workspace = await session.scalar(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    if workspace is None:
        raise workspace_not_found_error()

    membership = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )
    if membership is None:
        raise member_not_found_error()
    await ensure_not_last_owner(session, membership, None)
    await session.execute(
        delete(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def ensure_not_last_owner(
    session: AsyncSession,
    membership: WorkspaceMembership,
    replacement_role: str | None,
) -> None:
    if membership.role != "owner" or replacement_role == "owner":
        return
    owner_count = await session.scalar(
        select(func.count())
        .select_from(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == membership.workspace_id,
            WorkspaceMembership.role == "owner",
        )
    )
    if owner_count == 1:
        raise AppError(
            status_code=409,
            code="last_owner_required",
            title="Workspace needs an owner",
            detail="Assign another owner before demoting or removing the current owner.",
        )


def user_conflict_error() -> AppError:
    return AppError(
        status_code=409,
        code="username_already_exists",
        title="Username already exists",
        detail="A user with the supplied username already exists.",
    )


def user_not_found_error() -> AppError:
    return AppError(
        status_code=404,
        code="user_not_found",
        title="User not found",
        detail="The requested active user does not exist.",
    )


def workspace_conflict_error() -> AppError:
    return AppError(
        status_code=409,
        code="workspace_slug_already_exists",
        title="Workspace slug already exists",
        detail="A workspace with the supplied slug already exists.",
    )


def workspace_not_found_error() -> AppError:
    return AppError(
        status_code=404,
        code="workspace_not_found",
        title="Workspace not found",
        detail="The requested workspace does not exist.",
    )


def member_not_found_error() -> AppError:
    return AppError(
        status_code=404,
        code="workspace_member_not_found",
        title="Workspace member not found",
        detail="The requested user is not a member of this workspace.",
    )
