"""Workspace-scoped document intelligence & version management routes."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    DocumentContext,
    MutableDocumentContext,
    MutationWorkspaceContext,
    WorkspaceContext,
)
from app.api.schemas import (
    ConfirmVersionSuggestionRequest,
    DocumentEntityResponse,
    DocumentProfileResponse,
    DocumentVersionGroupResponse,
    DocumentVersionMemberResponse,
    DocumentVersionsDetailResponse,
    DocumentVersionSignalResponse,
    DocumentVersionSuggestionListResponse,
    DocumentVersionSuggestionResponse,
    LinkDocumentVersionRequest,
    UpdateVersionGroupRequest,
    VersionRelationKind,
    VersionSuggestionDocResponse,
)
from app.core.errors import AppError
from app.db.models import DocumentProfile, DocumentVersionMember
from app.db.session import get_session
from app.documents.intelligence import enrich_document
from app.documents.versions import (
    DocumentVersionSignal,
    VersionGroupDetail,
    VersionSuggestionDetail,
    confirm_suggestion,
    detect_version_suggestions_for_document,
    dismiss_suggestion,
    link_document_versions,
    list_workspace_suggestions,
    load_document_version_detail,
    unlink_document_version,
    update_version_group,
)
from app.security.auth import CurrentUser

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/documents", tags=["document-versions"])


def _as_relation_kind(value: str) -> VersionRelationKind:
    if value == "periodic_series":
        return "periodic_series"
    return "supersedes"


def _serialize_entities(raw: object) -> list[DocumentEntityResponse]:
    if not isinstance(raw, list):
        return []
    result: list[DocumentEntityResponse] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        raw_type = entry.get("type")
        entity_type = (
            raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else "other"
        )
        raw_aliases = entry.get("aliases")
        aliases = (
            [a.strip() for a in raw_aliases if isinstance(a, str) and a.strip()]
            if isinstance(raw_aliases, list)
            else []
        )
        result.append(DocumentEntityResponse(name=name.strip(), type=entity_type, aliases=aliases))
    return result


def profile_response(profile: DocumentProfile | None) -> DocumentProfileResponse | None:
    if profile is None:
        return None
    topics = (
        [t for t in profile.topics if isinstance(t, str) and t.strip()]
        if isinstance(profile.topics, list)
        else []
    )
    return DocumentProfileResponse(
        status=profile.status,
        title=profile.title,
        doc_category=profile.doc_category,
        doc_subtype=profile.doc_subtype,
        language=profile.language,
        primary_entities=_serialize_entities(profile.primary_entities),
        counterparties=_serialize_entities(profile.counterparties),
        period_label=profile.period_label,
        effective_date=profile.effective_date,
        document_number=profile.document_number,
        version_label=profile.version_label,
        summary=profile.summary,
        topics=topics,
        model=profile.model,
        updated_at=profile.updated_at,
    )


def version_signal_response(
    signal: DocumentVersionSignal | None,
) -> DocumentVersionSignalResponse | None:
    if signal is None:
        return None
    return DocumentVersionSignalResponse(
        group_id=signal.group_id,
        group_name=signal.group_name,
        relation_kind=_as_relation_kind(signal.relation_kind),
        version_rank=signal.version_rank,
        version_count=signal.version_count,
        is_latest=signal.is_latest,
        is_superseded=signal.is_superseded,
        latest_document_id=signal.latest_document_id,
        latest_filename=signal.latest_filename,
        latest_period_label=signal.latest_period_label,
        version_note=signal.version_note,
    )


def version_group_response(
    group: VersionGroupDetail | None,
) -> DocumentVersionGroupResponse | None:
    if group is None:
        return None
    return DocumentVersionGroupResponse(
        group_id=group.group_id,
        name=group.name,
        relation_kind=_as_relation_kind(group.relation_kind),
        members=[
            DocumentVersionMemberResponse(
                document_id=m.document_id,
                filename=m.filename,
                title=m.title,
                period_label=m.period_label,
                effective_date=m.effective_date,
                version_label=m.version_label,
                version_rank=m.version_rank,
                is_latest=m.is_latest,
                version_note=m.version_note,
                linked_at=m.linked_at,
                created_at=m.created_at,
            )
            for m in group.members
        ],
    )


def version_suggestion_response(
    suggestion: VersionSuggestionDetail,
) -> DocumentVersionSuggestionResponse:
    return DocumentVersionSuggestionResponse(
        id=suggestion.id,
        workspace_id=suggestion.workspace_id,
        document_a=VersionSuggestionDocResponse(
            document_id=suggestion.document_a.document_id,
            filename=suggestion.document_a.filename,
            title=suggestion.document_a.title,
            doc_category=suggestion.document_a.doc_category,
            doc_subtype=suggestion.document_a.doc_subtype,
            period_label=suggestion.document_a.period_label,
            effective_date=suggestion.document_a.effective_date,
            version_label=suggestion.document_a.version_label,
            primary_entities=list(suggestion.document_a.primary_entities),
            created_at=suggestion.document_a.created_at,
        ),
        document_b=VersionSuggestionDocResponse(
            document_id=suggestion.document_b.document_id,
            filename=suggestion.document_b.filename,
            title=suggestion.document_b.title,
            doc_category=suggestion.document_b.doc_category,
            doc_subtype=suggestion.document_b.doc_subtype,
            period_label=suggestion.document_b.period_label,
            effective_date=suggestion.document_b.effective_date,
            version_label=suggestion.document_b.version_label,
            primary_entities=list(suggestion.document_b.primary_entities),
            created_at=suggestion.document_b.created_at,
        ),
        suggested_newer_document_id=suggestion.suggested_newer_document_id,
        suggested_relation=_as_relation_kind(suggestion.suggested_relation),
        confidence=suggestion.confidence,
        reasons=list(suggestion.reasons),
        status=suggestion.status,
        created_at=suggestion.created_at,
        resolved_at=suggestion.resolved_at,
    )


async def _build_document_versions_detail(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    document_id: UUID,
) -> DocumentVersionsDetailResponse:
    profile = await session.get(DocumentProfile, document_id)
    group_detail, suggestions = await load_document_version_detail(
        session,
        workspace_id=workspace_id,
        document_id=document_id,
    )
    return DocumentVersionsDetailResponse(
        document_id=document_id,
        workspace_id=workspace_id,
        profile=profile_response(profile),
        group=version_group_response(group_detail),
        pending_suggestions=[version_suggestion_response(s) for s in suggestions],
    )


@router.get("/version-suggestions", response_model=DocumentVersionSuggestionListResponse)
async def get_workspace_version_suggestions(
    workspace: WorkspaceContext,
    _current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[
        Literal["pending", "confirmed", "dismissed"], Query(alias="status")
    ] = "pending",
    document_id: Annotated[UUID | None, Query()] = None,
) -> DocumentVersionSuggestionListResponse:
    suggestions = await list_workspace_suggestions(
        session,
        workspace_id=workspace.id,
        status=status_filter,
        document_id=document_id,
    )
    items = [version_suggestion_response(s) for s in suggestions]
    return DocumentVersionSuggestionListResponse(items=items, total=len(items))


@router.post(
    "/version-suggestions/{suggestion_id}/confirm",
    response_model=DocumentVersionGroupResponse,
)
async def confirm_version_suggestion(
    suggestion_id: UUID,
    workspace: MutationWorkspaceContext,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: ConfirmVersionSuggestionRequest | None = None,
) -> DocumentVersionGroupResponse:
    payload = body or ConfirmVersionSuggestionRequest()
    suggestion, _group = await confirm_suggestion(
        session,
        workspace_id=workspace.id,
        suggestion_id=suggestion_id,
        actor_user_id=current_user.id,
        relation_kind=payload.relation_kind,
        newer_document_id=payload.newer_document_id,
        group_name=payload.group_name,
    )
    target_doc_id = payload.newer_document_id or suggestion.suggested_newer_document_id
    group_detail, _ = await load_document_version_detail(
        session,
        workspace_id=workspace.id,
        document_id=target_doc_id,
    )
    serialized = version_group_response(group_detail)
    assert serialized is not None
    return serialized


@router.post(
    "/version-suggestions/{suggestion_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_version_suggestion(
    suggestion_id: UUID,
    workspace: MutationWorkspaceContext,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await dismiss_suggestion(
        session,
        workspace_id=workspace.id,
        suggestion_id=suggestion_id,
        actor_user_id=current_user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/version-groups/{group_id}",
    response_model=DocumentVersionGroupResponse,
)
async def patch_version_group(
    group_id: UUID,
    body: UpdateVersionGroupRequest,
    workspace: MutationWorkspaceContext,
    _current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentVersionGroupResponse:
    await update_version_group(
        session,
        workspace_id=workspace.id,
        group_id=group_id,
        name=body.name,
        relation_kind=body.relation_kind,
        member_order=body.member_order,
    )
    from sqlalchemy import select

    any_member_doc_id = await session.scalar(
        select(DocumentVersionMember.document_id)
        .where(
            DocumentVersionMember.group_id == group_id,
            DocumentVersionMember.workspace_id == workspace.id,
        )
        .limit(1)
    )
    if any_member_doc_id is None:
        raise AppError(
            status_code=404,
            code="version_group_not_found",
            title="Version group not found",
            detail="The requested version group has no active members.",
        )
    group_detail, _ = await load_document_version_detail(
        session,
        workspace_id=workspace.id,
        document_id=any_member_doc_id,
    )
    serialized = version_group_response(group_detail)
    assert serialized is not None
    return serialized


@router.get("/{document_id}/versions", response_model=DocumentVersionsDetailResponse)
async def get_document_versions(
    document: DocumentContext,
    _current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentVersionsDetailResponse:
    return await _build_document_versions_detail(
        session,
        workspace_id=document.link.workspace_id,
        document_id=document.document.id,
    )


@router.post("/{document_id}/versions/link", response_model=DocumentVersionsDetailResponse)
async def link_document_version(
    document_id: UUID,
    body: LinkDocumentVersionRequest,
    workspace: MutationWorkspaceContext,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentVersionsDetailResponse:
    if body.direction == "newer":
        newer_id = document_id
        older_id = body.related_document_id
    else:
        newer_id = body.related_document_id
        older_id = document_id

    await link_document_versions(
        session,
        workspace_id=workspace.id,
        newer_document_id=newer_id,
        older_document_id=older_id,
        relation_kind=body.relation_kind,
        group_name=body.group_name,
        version_note=body.version_note,
        actor_user_id=current_user.id,
    )
    return await _build_document_versions_detail(
        session,
        workspace_id=workspace.id,
        document_id=document_id,
    )


@router.delete("/{document_id}/versions", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_document_from_versions(
    document: MutableDocumentContext,
    _current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await unlink_document_version(
        session,
        workspace_id=document.link.workspace_id,
        document_id=document.document.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{document_id}/enrich", response_model=DocumentVersionsDetailResponse)
async def refresh_document_profile(
    document: MutableDocumentContext,
    _current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentVersionsDetailResponse:
    from app.db.models import RUN_READY, ProcessingRun

    run = (
        await session.get(ProcessingRun, document.document.active_processing_run_id)
        if document.document.active_processing_run_id is not None
        else None
    )
    if run is None or run.status != RUN_READY:
        raise AppError(
            status_code=409,
            code="document_not_ready",
            title="Document is not ready",
            detail=(
                "Wait for document extraction to finish before refreshing its "
                "intelligence profile."
            ),
        )
    await enrich_document(
        session,
        document_id=document.document.id,
        run_id=run.id,
        commit=True,
    )
    await detect_version_suggestions_for_document(
        session,
        document_id=document.document.id,
        workspace_id=document.link.workspace_id,
        commit=True,
    )
    return await _build_document_versions_detail(
        session,
        workspace_id=document.link.workspace_id,
        document_id=document.document.id,
    )
