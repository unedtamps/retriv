"""Finding a document by filename or extracted intelligence across readable workspaces.

Retrieval answers "what does this workspace say about X". This answers the
question before it: "which file is that in". Without it a model can only learn
a document_id from a search result or the CSV catalogue, so naming a file or a
subject company in a question is not something it can act on.

The search matches case-insensitively against both the workspace filename and
the canonical ``DocumentProfile.search_text`` (entities, aliases, document
types, titles, periods, document numbers). Multi-word queries such as
``"Apple 2025"`` match when every term appears across the filename and profile
metadata. Exact and prefix filename hits still rank above metadata-only matches,
and latest versions rank ahead of superseded ones.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    VERSION_RELATION_SUPERSEDES,
    Document,
    DocumentProfile,
    DocumentVersionGroup,
    DocumentVersionMember,
    DocumentWorkspace,
    ProcessingRun,
    Workspace,
)
from app.documents.versions import DocumentVersionSignal, load_workspace_version_signals
from app.processing.service import derived_document_status
from app.retrieval.sql import ROW_STORED_MEDIA_TYPES, escape_like

_MAX_QUERY_TOKENS = 6


@dataclass(frozen=True)
class DocumentSummary:
    """One document, with enough beside its name to tell two apart."""

    document_id: UUID
    workspace_id: UUID
    workspace_name: str
    filename: str
    media_type: str
    size_bytes: int
    status: str
    row_count: int
    created_at: datetime
    title: str | None = None
    doc_category: str | None = None
    doc_subtype: str | None = None
    primary_entities: tuple[str, ...] = ()
    period_label: str | None = None
    effective_date: str | None = None
    version_label: str | None = None
    version_signal: DocumentVersionSignal | None = None

    @property
    def is_row_stored(self) -> bool:
        """Whether this file's content is rows rather than text.

        A CSV, workbook and retriv-json payload all land in the same row store,
        so a listing that offers one to ``query_rows`` has to offer the others.
        Keyed on the media type the pipeline detected, not on the filename: a
        workbook is queryable whatever it was called.
        """
        return self.media_type in ROW_STORED_MEDIA_TYPES


def _entity_display_names(profile: DocumentProfile | None) -> tuple[str, ...]:
    if profile is None or not isinstance(profile.primary_entities, list):
        return ()
    names: list[str] = []
    for entry in profile.primary_entities:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return tuple(names)


def _pattern_clause(pattern: str) -> Any:
    escaped_full = f"%{escape_like(pattern)}%"
    full_match = or_(
        DocumentWorkspace.filename.ilike(escaped_full, escape="\\"),
        DocumentProfile.search_text.ilike(escaped_full, escape="\\"),
    )
    tokens = [tok for tok in pattern.strip().split() if tok][:_MAX_QUERY_TOKENS]
    if len(tokens) <= 1:
        return full_match
    token_clauses = [
        or_(
            DocumentWorkspace.filename.ilike(f"%{escape_like(tok)}%", escape="\\"),
            DocumentProfile.search_text.ilike(f"%{escape_like(tok)}%", escape="\\"),
        )
        for tok in tokens
    ]
    return or_(full_match, and_(*token_clauses))


async def find_documents(
    session: AsyncSession,
    workspace_ids: list[UUID],
    *,
    pattern: str | None,
    limit: int,
    include_superseded: bool = True,
) -> tuple[list[DocumentSummary], int]:
    """Documents in ``workspace_ids`` matching ``pattern`` by filename or intelligence profile.

    Returns the page and the total that matched before the page, so the caller
    can say "showing 20 of 63" instead of implying the list is complete.
    """
    if not workspace_ids:
        return [], 0

    condition = (
        DocumentWorkspace.workspace_id.in_(workspace_ids)
        & DocumentWorkspace.deleted_at.is_(None)
        & Document.deleted_at.is_(None)
    )
    statement = (
        select(
            Document,
            DocumentWorkspace,
            ProcessingRun,
            Workspace.name,
            DocumentProfile,
            func.count().over().label("total"),
        )
        .join(DocumentWorkspace, DocumentWorkspace.document_id == Document.id)
        .join(Workspace, Workspace.id == DocumentWorkspace.workspace_id)
        .outerjoin(ProcessingRun, ProcessingRun.id == Document.active_processing_run_id)
        .outerjoin(DocumentProfile, DocumentProfile.document_id == Document.id)
        .outerjoin(
            DocumentVersionMember,
            and_(
                DocumentVersionMember.workspace_id == DocumentWorkspace.workspace_id,
                DocumentVersionMember.document_id == DocumentWorkspace.document_id,
            ),
        )
        .outerjoin(
            DocumentVersionGroup,
            DocumentVersionGroup.id == DocumentVersionMember.group_id,
        )
        .where(condition)
    )

    if not include_superseded:
        statement = statement.where(
            or_(
                DocumentVersionGroup.id.is_(None),
                DocumentVersionGroup.relation_kind != VERSION_RELATION_SUPERSEDES,
                DocumentVersionMember.is_latest.is_(True),
            )
        )

    superseded_rank = case(
        (
            and_(
                DocumentVersionGroup.relation_kind == VERSION_RELATION_SUPERSEDES,
                DocumentVersionMember.is_latest.is_(False),
            ),
            1,
        ),
        else_=0,
    )

    if pattern:
        needle = pattern.casefold()
        statement = statement.where(_pattern_clause(pattern))
        rank = case(
            (func.lower(DocumentWorkspace.filename) == needle, 0),
            (
                DocumentWorkspace.filename.ilike(f"{escape_like(needle)}%", escape="\\"),
                1,
            ),
            (
                DocumentWorkspace.filename.ilike(f"%{escape_like(pattern)}%", escape="\\"),
                2,
            ),
            else_=3,
        )
        statement = statement.order_by(
            rank, superseded_rank, DocumentWorkspace.filename, Document.id
        )
    else:
        statement = statement.order_by(
            superseded_rank, DocumentWorkspace.created_at.desc(), Document.id
        )

    result = await session.execute(statement.limit(limit))
    rows = result.all()

    signals = await load_workspace_version_signals(
        session,
        workspace_ids=[link.workspace_id for _, link, _, _, _, _ in rows],
        document_ids=[document.id for document, _, _, _, _, _ in rows],
    )

    summaries = [
        DocumentSummary(
            document_id=document.id,
            workspace_id=link.workspace_id,
            workspace_name=workspace_name,
            filename=link.filename,
            media_type=document.detected_media_type,
            size_bytes=document.size_bytes,
            status=derived_document_status(document, run),
            row_count=int(run.row_count or 0) if run is not None else 0,
            created_at=link.created_at,
            title=profile.title if profile is not None else None,
            doc_category=profile.doc_category if profile is not None else None,
            doc_subtype=profile.doc_subtype if profile is not None else None,
            primary_entities=_entity_display_names(profile),
            period_label=profile.period_label if profile is not None else None,
            effective_date=profile.effective_date if profile is not None else None,
            version_label=profile.version_label if profile is not None else None,
            version_signal=signals.get((link.workspace_id, document.id)),
        )
        for document, link, run, workspace_name, profile, _ in rows
    ]

    return summaries, int(rows[0].total) if rows else 0
