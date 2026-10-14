"""Scope helpers for the retrieval and MCP tool paths.

The RS256 delegation token is gone: every entrypoint derives the workspace
scope from the caller's own memberships and passes it straight down, so
there is no signature to verify and no second service to trust.

The cap stays. A scope wide enough to be worth rejecting is also a scope
wide enough to be a denial-of-service risk against the vector search.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkspaceMembership
from app.security.authorization import ROLE_PERMISSIONS

MAX_SCOPE_WORKSPACES = 1000


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
