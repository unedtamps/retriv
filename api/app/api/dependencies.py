from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import Document, User, Workspace
from app.db.session import get_session
from app.infrastructure.scanner import MalwareScanner, get_malware_scanner
from app.infrastructure.storage import ObjectStorage, get_object_storage
from app.security.auth import CurrentUser
from app.security.authorization import load_workspace_access, require_platform_admin


async def get_workspace(
    workspace_id: UUID,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Workspace:
    access = await load_workspace_access(
        session,
        workspace_id=workspace_id,
        user=current_user,
        permission="read",
    )
    return access.workspace


async def get_upload_workspace(
    workspace_id: UUID,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Workspace:
    access = await load_workspace_access(
        session,
        workspace_id=workspace_id,
        user=current_user,
        permission="upload",
    )
    return access.workspace


async def get_download_workspace(
    workspace_id: UUID,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Workspace:
    access = await load_workspace_access(
        session,
        workspace_id=workspace_id,
        user=current_user,
        permission="download",
    )
    return access.workspace


async def get_mutation_workspace(
    workspace_id: UUID,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Workspace:
    access = await load_workspace_access(
        session,
        workspace_id=workspace_id,
        user=current_user,
        permission="delete",
    )
    return access.workspace


async def get_member_management_workspace(
    workspace_id: UUID,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Workspace:
    access = await load_workspace_access(
        session,
        workspace_id=workspace_id,
        user=current_user,
        permission="manage_members",
    )
    return access.workspace


async def get_admin_user(current_user: CurrentUser) -> User:
    return require_platform_admin(current_user)


WorkspaceContext = Annotated[Workspace, Depends(get_workspace)]
UploadWorkspaceContext = Annotated[Workspace, Depends(get_upload_workspace)]
DownloadWorkspaceContext = Annotated[Workspace, Depends(get_download_workspace)]
MutationWorkspaceContext = Annotated[Workspace, Depends(get_mutation_workspace)]
MemberManagementWorkspaceContext = Annotated[
    Workspace, Depends(get_member_management_workspace)
]
PlatformAdmin = Annotated[User, Depends(get_admin_user)]
StorageDependency = Annotated[ObjectStorage, Depends(get_object_storage)]
ScannerDependency = Annotated[MalwareScanner, Depends(get_malware_scanner)]


async def get_agent_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=settings.agent_timeout_seconds) as client:
        yield client


AgentClient = Annotated[httpx.AsyncClient, Depends(get_agent_client)]


async def get_processing_api_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=settings.processing_api_base_url,
        timeout=settings.processing_api_timeout_seconds,
    ) as client:
        yield client


ProcessingApiClient = Annotated[
    httpx.AsyncClient, Depends(get_processing_api_client)
]


async def get_document(
    document_id: UUID,
    workspace: WorkspaceContext,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Document:
    document = await session.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.workspace_id == workspace.id,
        )
    )
    if document is None:
        raise AppError(
            status_code=404,
            code="document_not_found",
            title="Document not found",
            detail="The requested document does not exist in this workspace.",
        )
    return document


DocumentContext = Annotated[Document, Depends(get_document)]


async def get_downloadable_document(
    document_id: UUID,
    workspace: DownloadWorkspaceContext,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Document:
    document = await session.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.workspace_id == workspace.id,
        )
    )
    if document is None:
        raise AppError(
            status_code=404,
            code="document_not_found",
            title="Document not found",
            detail="The requested document does not exist in this workspace.",
        )
    return document


async def get_mutable_document(
    document_id: UUID,
    workspace: MutationWorkspaceContext,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Document:
    document = await session.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.workspace_id == workspace.id,
        )
    )
    if document is None:
        raise AppError(
            status_code=404,
            code="document_not_found",
            title="Document not found",
            detail="The requested document does not exist in this workspace.",
        )
    return document


DownloadableDocumentContext = Annotated[
    Document, Depends(get_downloadable_document)
]
MutableDocumentContext = Annotated[Document, Depends(get_mutable_document)]
