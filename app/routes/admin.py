import secrets
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from anyio import to_thread
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import Result, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.chat import ChatProviderError, OpenAICompatibleChatProvider
from app.agents.model_settings import fresh_session_id, key_preview
from app.agents.providers import BUILTIN_PROVIDERS, session_header_for
from app.api.dependencies import PlatformAdmin
from app.api.schemas import (
    AdminMemberRequest,
    AdminPasswordResponse,
    AdminUserCreateRequest,
    AdminUserListItemResponse,
    AdminUserListResponse,
    AdminUserResponse,
    AdminWorkspaceListResponse,
    AdminWorkspaceResponse,
    AgentModelSettingsResponse,
    AgentModelSettingsUpdate,
    AgentModelTestResult,
    AgentProviderInfo,
    WorkspaceCreateRequest,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from app.core.errors import AppError
from app.db.models import (
    AgentModelSetting,
    Document,
    DocumentWorkspace,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.db.session import get_session
from app.routes.workspaces import admin_membership_error, member_response
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


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    if user_id == admin.id:
        raise self_delete_error()
    user = await session.get(User, user_id)
    if user is None:
        raise user_not_found_error()

    # Deleting a user removes their memberships by cascade. A workspace whose
    # only owner is this user would be left with nobody who can manage it, so
    # the same last-owner rule the member-removal routes enforce applies here.
    # Lock the workspaces in id order before counting, matching the lock order
    # of the other membership routes.
    owner_workspace_ids = list(
        await session.scalars(
            select(WorkspaceMembership.workspace_id).where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role == "owner",
            )
        )
    )
    if owner_workspace_ids:
        await session.execute(
            select(Workspace)
            .where(Workspace.id.in_(owner_workspace_ids))
            .order_by(Workspace.id)
            .with_for_update()
        )
        owner_memberships = (
            await session.scalars(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.role == "owner",
                )
            )
        ).all()
        for membership in owner_memberships:
            await ensure_not_last_owner(session, membership, None)

    await session.execute(
        update(WorkspaceMembership)
        .where(WorkspaceMembership.granted_by_user_id == user_id)
        .values(granted_by_user_id=admin.id)
    )
    await session.execute(
        update(Document)
        .where(Document.created_by_user_id == user_id)
        .values(created_by_user_id=None)
    )
    await session.execute(
        update(DocumentWorkspace)
        .where(DocumentWorkspace.created_by_user_id == user_id)
        .values(created_by_user_id=None)
    )
    await session.delete(user)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/workspaces", response_model=AdminWorkspaceListResponse)
async def list_admin_workspaces(
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> AdminWorkspaceListResponse:
    total = await session.scalar(select(func.count()).select_from(Workspace))
    member_count = (
        select(func.count())
        .select_from(WorkspaceMembership)
        .where(WorkspaceMembership.workspace_id == Workspace.id)
        .correlate(Workspace)
        .scalar_subquery()
    )
    workspaces = (
        await session.execute(
            select(Workspace, member_count)
            .order_by(Workspace.name, Workspace.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    workspace_ids = [workspace.id for workspace, _ in workspaces]
    empty_membership_rows: list[tuple[WorkspaceMembership, User]] = []
    membership_rows: Result[tuple[WorkspaceMembership, User]] | list[
        tuple[WorkspaceMembership, User]
    ] = (
        await session.execute(
            select(WorkspaceMembership, User)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(WorkspaceMembership.workspace_id.in_(workspace_ids))
            .order_by(User.username, User.id)
        )
        if workspace_ids
        else empty_membership_rows
    )
    members_by_workspace: dict[UUID, list[WorkspaceMemberResponse]] = {}
    for membership, user in membership_rows:
        members_by_workspace.setdefault(membership.workspace_id, []).append(
            member_response(membership, user)
        )

    return AdminWorkspaceListResponse(
        items=[
            AdminWorkspaceResponse(
                id=workspace.id,
                slug=workspace.slug,
                name=workspace.name,
                description=workspace.description,
                created_at=workspace.created_at,
                member_count=member_count or 0,
                members=members_by_workspace.get(workspace.id, []),
            )
            for workspace, member_count in workspaces
        ],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.post("/workspaces", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkspaceResponse:
    owner = await session.get(User, payload.owner_user_id)
    if owner is None or not owner.is_active:
        raise user_not_found_error()
    if owner.role == "admin":
        raise admin_membership_error()

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
    if user.role == "admin":
        raise admin_membership_error()

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


def self_delete_error() -> AppError:
    return AppError(
        status_code=409,
        code="self_delete_restricted",
        title="Self deletion restricted",
        detail="A signed-in administrator cannot delete their own account.",
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


def _provider_choices() -> list[AgentProviderInfo]:
    """The provider options an admin picks from, custom first."""
    return [
        AgentProviderInfo(
            key="custom",
            label="Custom endpoint",
            default_model="",
            key_hint="Bring your own OpenAI-compatible URL.",
        ),
        *(
            AgentProviderInfo(
                key=key,
                label=preset.label,
                default_model=preset.default_model,
                key_hint=preset.key_hint,
            )
            for key, preset in BUILTIN_PROVIDERS.items()
        ),
    ]


@router.get("/agent-model", response_model=AgentModelSettingsResponse)
async def get_agent_model(
    _admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentModelSettingsResponse:
    """The harness's configured chat model, with the key shown only masked."""
    row = await session.get(AgentModelSetting, 1)
    if row is None:
        return AgentModelSettingsResponse(
            configured=False, available_providers=_provider_choices()
        )
    return AgentModelSettingsResponse(
        configured=True,
        provider=row.provider,
        base_url=row.base_url,
        model=row.model,
        api_key_preview=key_preview(row.api_key),
        last_test_ok=row.last_test_ok,
        last_test_model=row.last_test_model,
        last_tested_at=row.last_tested_at,
        updated_at=row.updated_at,
        key_hint=key_preview(row.api_key),
        available_providers=_provider_choices(),
    )


@router.put("/agent-model", response_model=AgentModelSettingsResponse)
async def set_agent_model(
    payload: AgentModelSettingsUpdate,
    admin: PlatformAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentModelSettingsResponse:
    """Save the harness's chat model, then test it and record the outcome.

    The settings are kept even when the connection test fails: the endpoint
    may be temporarily down, and refusing to save would leave the previous
    configuration standing with no record of the attempt. The response
    carries the test result so the admin sees a broken endpoint immediately.
    """
    api_key = payload.api_key
    if api_key is None:
        existing = await session.get(AgentModelSetting, 1)
        if existing is None:
            raise AppError(
                status_code=422,
                code="agent_model_key_required",
                title="API key required",
                detail="The first configuration must include the model endpoint's API key.",
            )
        api_key = existing.api_key

    if payload.base_url is None or payload.model is None or payload.provider is None:
        raise AppError(
            status_code=422,
            code="agent_model_unresolved",
            title="Model endpoint incomplete",
            detail="Provider resolution left the endpoint or model unset.",
        )
    provider = OpenAICompatibleChatProvider(
        base_url=payload.base_url,
        api_key=api_key,
        model=payload.model,
        session_header=session_header_for(payload.provider),
        session_id=fresh_session_id(),
    )
    test_ok = False
    test_model: str | None = None
    test_error: str | None = None
    try:
        test_model = await provider.test_connection()
        test_ok = True
    except ChatProviderError as exc:
        test_error = str(exc)
    finally:
        await provider.aclose()

    row = await session.get(AgentModelSetting, 1)
    if row is None:
        row = AgentModelSetting(id=1)
        session.add(row)
    row.provider = payload.provider
    row.base_url = payload.base_url
    row.api_key = api_key
    row.model = payload.model
    row.last_test_ok = test_ok
    row.last_test_model = test_model if test_ok else None
    row.last_tested_at = datetime.now(UTC)
    row.updated_by_user_id = admin.id
    await session.commit()
    await session.refresh(row)
    return AgentModelSettingsResponse(
        configured=True,
        provider=row.provider,
        base_url=row.base_url,
        model=row.model,
        api_key_preview=key_preview(row.api_key),
        last_test_ok=test_ok,
        last_test_model=test_model if test_ok else None,
        last_tested_at=row.last_tested_at,
        test_error=test_error,
        updated_at=row.updated_at,
        key_hint=key_preview(row.api_key),
        available_providers=_provider_choices(),
    )


@router.post("/agent-model/test", response_model=AgentModelTestResult)
async def test_agent_model(
    payload: AgentModelSettingsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentModelTestResult:
    """Try a candidate configuration without saving anything.

    ``api_key`` may be omitted to test the endpoint and model against the
    key already stored, which is how an admin checks an edit before committing
    to it.
    """
    api_key = payload.api_key
    if api_key is None:
        existing = await session.get(AgentModelSetting, 1)
        if existing is None:
            raise AppError(
                status_code=422,
                code="agent_model_key_required",
                title="API key required",
                detail="Nothing is stored yet, so the test needs an API key.",
            )
        api_key = existing.api_key

    if payload.base_url is None or payload.model is None or payload.provider is None:
        raise AppError(
            status_code=422,
            code="agent_model_unresolved",
            title="Model endpoint incomplete",
            detail="Provider resolution left the endpoint or model unset.",
        )
    provider = OpenAICompatibleChatProvider(
        base_url=payload.base_url,
        api_key=api_key,
        model=payload.model,
        session_header=session_header_for(payload.provider),
        session_id=fresh_session_id(),
    )
    try:
        model = await provider.test_connection()
        return AgentModelTestResult(ok=True, model=model)
    except ChatProviderError as exc:
        return AgentModelTestResult(ok=False, error=str(exc))
    finally:
        await provider.aclose()
