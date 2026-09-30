from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import User, Workspace, WorkspaceMembership

WorkspaceRole = Literal["owner", "editor", "viewer"]
WorkspacePermission = Literal[
    "read",
    "upload",
    "download",
    "delete",
    "search_context",
    "manage_members",
]

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(
        {"read", "upload", "download", "delete", "search_context", "manage_members"}
    ),
    "editor": frozenset({"read", "upload", "download", "delete", "search_context"}),
    "viewer": frozenset({"read", "search_context"}),
}


@dataclass(frozen=True)
class WorkspaceAccess:
    workspace: Workspace
    membership: WorkspaceMembership


async def load_workspace_access(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user: User,
    permission: WorkspacePermission,
) -> WorkspaceAccess:
    result = await session.execute(
        select(Workspace, WorkspaceMembership)
        .join(
            WorkspaceMembership,
            and_(
                WorkspaceMembership.workspace_id == Workspace.id,
                WorkspaceMembership.user_id == user.id,
            ),
        )
        .where(Workspace.id == workspace_id)
    )
    row = result.one_or_none()
    if row is None:
        raise workspace_not_found_error()

    workspace, membership = row
    if permission not in ROLE_PERMISSIONS.get(membership.role, frozenset()):
        raise insufficient_workspace_permission_error()
    return WorkspaceAccess(workspace=workspace, membership=membership)


def require_platform_admin(user: User) -> User:
    if user.role != "admin":
        raise AppError(
            status_code=403,
            code="admin_required",
            title="Administrator access required",
            detail="Administrator access is required for this operation.",
        )
    return user


def workspace_not_found_error() -> AppError:
    return AppError(
        status_code=404,
        code="workspace_not_found",
        title="Workspace not found",
        detail="The requested workspace does not exist or is not accessible.",
    )


def insufficient_workspace_permission_error() -> AppError:
    return AppError(
        status_code=403,
        code="insufficient_workspace_permission",
        title="Insufficient workspace permission",
        detail="The current user does not have permission for this workspace operation.",
    )
