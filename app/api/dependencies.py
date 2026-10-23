from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import Document, DocumentWorkspace, User, Workspace
from app.db.session import get_session
from app.infrastructure.scanner import MalwareScanner, get_malware_scanner
from app.infrastructure.storage import ObjectStorage, get_object_storage
from app.security.api_keys import (
    authenticate_api_key,
    bearer_api_key,
    looks_like_api_key,
)
from app.security.auth import CurrentUser, authentication_error, decode_access_token
from app.security.authorization import load_workspace_access, require_platform_admin


async def get_bearer_user(
    authorization: Annotated[str | None, Header()] = None,
    *,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """Resolve either credential kind to the user behind it.

    Two credentials exist and both are bearer tokens in one header: the
    dashboard's short-lived JWT, and the long-lived ``rk_`` API key an MCP
    client holds. They are told apart by shape -- ``looks_like_api_key`` is a
    prefix and length check, so no JWT can be mistaken for a key -- and both
    paths end at the same 401, so a caller cannot use the response to learn
    which one was wrong.

    This is deliberately narrower than ``get_current_user``. Only the document
    download route accepts both, because it is the one operation a tool hands
    out as a URL for the agent to fetch with the same header. Every other
    route stays JWT-only, so an API key cannot reach the write endpoints.
    """
    if not authorization:
        raise authentication_error()
    try:
        token = bearer_api_key(authorization)
    except AppError:
        raise authentication_error() from None

    if looks_like_api_key(token):
        user = await authenticate_api_key(session, token)
        if user is None:
            raise authentication_error()
        return user

    claims = decode_access_token(token, settings)
    user = await session.get(User, claims.sub)
    if user is None or not user.is_active or user.auth_version != claims.auth_version:
        raise authentication_error()
    return user


BearerUser = Annotated[User, Depends(get_bearer_user)]


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
    current_user: BearerUser,
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


@dataclass(frozen=True)
class ScopedDocument:
    """A canonical document as seen through one workspace link."""

    document: Document
    link: DocumentWorkspace

    @property
    def id(self) -> UUID:
        return self.document.id

    @property
    def workspace_id(self) -> UUID:
        return self.link.workspace_id

    @property
    def original_filename(self) -> str:
        return self.link.filename

    @property
    def detected_media_type(self) -> str:
        return self.document.detected_media_type

    @property
    def size_bytes(self) -> int:
        return self.document.size_bytes

    @property
    def sha256(self) -> str:
        return self.document.sha256

    @property
    def bucket(self) -> str:
        return self.document.bucket

    @property
    def object_key(self) -> str:
        return self.document.object_key

    @property
    def created_by_user_id(self) -> UUID | None:
        return self.link.created_by_user_id

    @property
    def created_at(self) -> datetime:
        return self.link.created_at

    @property
    def deleted_at(self) -> datetime | None:
        return self.link.deleted_at or self.document.deleted_at

    @property
    def active_processing_run_id(self) -> UUID | None:
        return self.document.active_processing_run_id


async def load_scoped_document(
    session: AsyncSession, *, workspace_id: UUID, document_id: UUID
) -> ScopedDocument | None:
    row = (
        await session.execute(
            select(Document, DocumentWorkspace)
            .join(
                DocumentWorkspace,
                DocumentWorkspace.document_id == Document.id,
            )
            .where(
                Document.id == document_id,
                DocumentWorkspace.workspace_id == workspace_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    document, link = row
    return ScopedDocument(document=document, link=link)


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


async def get_document(
    document_id: UUID,
    workspace: WorkspaceContext,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScopedDocument:
    document = await load_scoped_document(
        session, workspace_id=workspace.id, document_id=document_id
    )
    if document is None:
        raise AppError(
            status_code=404,
            code="document_not_found",
            title="Document not found",
            detail="The requested document does not exist in this workspace.",
        )
    return document


DocumentContext = Annotated[ScopedDocument, Depends(get_document)]


async def get_downloadable_document(
    document_id: UUID,
    workspace: DownloadWorkspaceContext,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScopedDocument:
    document = await load_scoped_document(
        session, workspace_id=workspace.id, document_id=document_id
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
) -> ScopedDocument:
    document = await load_scoped_document(
        session, workspace_id=workspace.id, document_id=document_id
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
    ScopedDocument, Depends(get_downloadable_document)
]
MutableDocumentContext = Annotated[ScopedDocument, Depends(get_mutable_document)]
