"""Resolving an MCP request to a user and their workspace scope.

Two entry points, deliberately:

* the endpoint middleware, which answers "is this a live credential?" before a
  byte of the JSON-RPC body is read, and
* every tool, which re-derives the caller from its own request headers.

The second is the one that matters for security. Tool handlers run in the
session manager's task group, not in the HTTP request that carried them, so
no contextvar set by the middleware can reach them. Re-authenticating per call
is what makes revoking a key take effect on the next call rather than at the
end of a session.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, Workspace
from app.retrieval.scope import searchable_workspace_ids
from app.security.api_keys import authenticate_api_key, bearer_api_key
from app.security.auth import authentication_error
from app.security.authorization import WorkspacePermission, load_workspace_access


@dataclass(frozen=True)
class McpPrincipal:
    """The caller behind an API key, and the workspaces they may search."""

    user: User
    workspace_ids: list[UUID]


async def user_from_authorization(
    authorization: str | None, session: AsyncSession
) -> User:
    key = bearer_api_key(authorization)
    user = await authenticate_api_key(session, key)
    if user is None:
        raise authentication_error()
    return user


async def principal_from_headers(
    headers: Mapping[str, str] | None, session: AsyncSession
) -> McpPrincipal:
    user = await user_from_authorization(
        headers.get("authorization") if headers else None, session
    )
    workspace_ids = await searchable_workspace_ids(session, user.id)
    return McpPrincipal(user=user, workspace_ids=workspace_ids)


async def require_workspace(
    session: AsyncSession,
    *,
    user: User,
    workspace_id: UUID,
    permission: WorkspacePermission,
) -> Workspace:
    """Check one workspace the caller named, rather than the whole scope.

    Reading uses the derived scope; writing takes an explicit workspace and
    the role permission for it, so a viewer cannot upload by way of a key that
    also happens to see other workspaces.
    """
    access = await load_workspace_access(
        session, workspace_id=workspace_id, user=user, permission=permission
    )
    return access.workspace
