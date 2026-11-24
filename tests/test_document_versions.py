"""Tests for workspace-scoped document version detection and manual confirmation."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.db.models import (
    PROFILE_STATUS_ENRICHED,
    VERSION_RELATION_PERIODIC_SERIES,
    VERSION_RELATION_SUPERSEDES,
    VERSION_SUGGESTION_CONFIRMED,
    VERSION_SUGGESTION_DISMISSED,
    VERSION_SUGGESTION_PENDING,
    Document,
    DocumentProfile,
    DocumentVersionGroup,
    DocumentVersionMember,
    DocumentVersionSuggestion,
    DocumentWorkspace,
)
from app.documents.versions import (
    cleanup_document_versions_on_unlink,
    confirm_suggestion,
    detect_version_suggestions_for_document,
    dismiss_suggestion,
    link_document_versions,
    load_document_version_detail,
    load_document_version_signals_for_workspace,
    load_pending_suggestion_counts,
    unlink_document_version,
    update_version_group,
)
from tests.conftest import SeededEntities


async def _seed_doc_with_profile(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    uploader_id: UUID,
    filename: str,
    title: str,
    doc_category: str = "other",
    doc_subtype: str | None = None,
    primary_entities: list[dict[str, Any]] | None = None,
    period_label: str | None = None,
    effective_date: str | None = None,
    document_number: str | None = None,
    version_label: str | None = None,
) -> UUID:
    doc_id = uuid4()
    doc = Document(
        id=doc_id,
        origin_workspace_id=workspace_id,
        original_filename=filename,
        detected_media_type="application/pdf",
        size_bytes=2048,
        sha256=uuid4().hex + uuid4().hex,
        bucket="raw",
        object_key=f"raw/{doc_id}",
        created_by_user_id=uploader_id,
    )
    link = DocumentWorkspace(
        workspace_id=workspace_id,
        document_id=doc_id,
        filename=filename,
        created_by_user_id=uploader_id,
    )
    profile = DocumentProfile(
        document_id=doc_id,
        status=PROFILE_STATUS_ENRICHED,
        title=title,
        doc_category=doc_category,
        doc_subtype=doc_subtype,
        primary_entities=primary_entities or [],
        counterparties=[],
        period_label=period_label,
        effective_date=effective_date,
        document_number=document_number,
        version_label=version_label,
        search_text=title.lower(),
    )
    session.add_all([doc, link, profile])
    await session.commit()
    return doc_id


async def test_detects_same_company_filings_and_ignores_other_company(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    """Two Apple 10-K filings generate a periodic_series suggestion; Microsoft 10-K does not."""
    async with db_session_factory() as session:
        apple_2024 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="10-K-2024-As-Filed.pdf",
            title="Apple Inc. Form 10-K Annual Report",
            doc_category="financial_report",
            doc_subtype="Form 10-K",
            primary_entities=[
                {
                    "name": "Apple Inc.",
                    "normalized": "apple",
                    "type": "company",
                    "aliases": ["AAPL"],
                }
            ],
            period_label="FY2024",
            effective_date="2024-09-28",
        )
        msft_2024 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="10-K-2024-MSFT.pdf",
            title="Microsoft Corporation Form 10-K Annual Report",
            doc_category="financial_report",
            doc_subtype="Form 10-K",
            primary_entities=[
                {
                    "name": "Microsoft Corporation",
                    "normalized": "microsoft",
                    "type": "company",
                    "aliases": ["MSFT"],
                }
            ],
            period_label="FY2024",
            effective_date="2024-06-30",
        )
        apple_2025 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="10-K-2025-As-Filed.pdf",
            title="Apple Inc. Form 10-K Annual Report",
            doc_category="financial_report",
            doc_subtype="Form 10-K",
            primary_entities=[
                {
                    "name": "Apple Inc.",
                    "normalized": "apple",
                    "type": "company",
                    "aliases": ["AAPL"],
                }
            ],
            period_label="FY2025",
            effective_date="2025-09-27",
        )

        suggestions = await detect_version_suggestions_for_document(
            session, workspace_id=entities.finance.id, document_id=apple_2025
        )
        assert len(suggestions) == 1
        suggestion = suggestions[0]
        assert {suggestion.document_a_id, suggestion.document_b_id} == {apple_2024, apple_2025}
        assert suggestion.suggested_newer_document_id == apple_2025
        assert suggestion.suggested_relation == VERSION_RELATION_PERIODIC_SERIES
        assert suggestion.status == VERSION_SUGGESTION_PENDING
        assert msft_2024 not in {suggestion.document_a_id, suggestion.document_b_id}

        counts = await load_pending_suggestion_counts(
            session,
            workspace_id=entities.finance.id,
            document_ids=[apple_2024, apple_2025, msft_2024],
        )
        assert counts == {apple_2024: 1, apple_2025: 1}


async def test_confirm_and_dismiss_suggestions_and_version_signals(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        policy_v1 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.legal.id,
            uploader_id=entities.user.id,
            filename="HR_Leave_Policy_v1.pdf",
            title="HR Leave Policy",
            doc_category="policy_sop",
            doc_subtype="HR Policy",
            document_number="SOP-HR-014",
            version_label="v1.0",
            effective_date="2024-01-01",
        )
        policy_v2 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.legal.id,
            uploader_id=entities.user.id,
            filename="HR_Leave_Policy_v2.pdf",
            title="HR Leave Policy",
            doc_category="policy_sop",
            doc_subtype="HR Policy",
            document_number="SOP-HR-014",
            version_label="v2.0",
            effective_date="2025-01-01",
        )

        suggestions = await detect_version_suggestions_for_document(
            session, workspace_id=entities.legal.id, document_id=policy_v2
        )
        assert len(suggestions) == 1
        suggestion = suggestions[0]
        assert suggestion.suggested_relation == VERSION_RELATION_SUPERSEDES
        assert suggestion.suggested_newer_document_id == policy_v2

        confirmed, group = await confirm_suggestion(
            session,
            workspace_id=entities.legal.id,
            suggestion_id=suggestion.id,
            actor_user_id=entities.user.id,
        )
        assert confirmed.status == VERSION_SUGGESTION_CONFIRMED
        assert group.relation_kind == VERSION_RELATION_SUPERSEDES

        signals = await load_document_version_signals_for_workspace(
            session, workspace_id=entities.legal.id, document_ids=[policy_v1, policy_v2]
        )
        assert signals[policy_v1].is_superseded is True
        assert signals[policy_v1].is_latest is False
        assert signals[policy_v1].latest_document_id == policy_v2
        assert signals[policy_v1].latest_filename == "HR_Leave_Policy_v2.pdf"
        assert signals[policy_v2].is_latest is True
        assert signals[policy_v2].is_superseded is False
        assert signals[policy_v2].version_count == 2

        # Re-running detection does not re-create a suggestion for already-linked docs.
        again = await detect_version_suggestions_for_document(
            session, workspace_id=entities.legal.id, document_id=policy_v2
        )
        assert again == []

        # Dismissing prevents re-suggesting a different pair.
        policy_v3 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.legal.id,
            uploader_id=entities.user.id,
            filename="HR_Leave_Policy_v3.pdf",
            title="HR Leave Policy",
            doc_category="policy_sop",
            document_number="SOP-HR-014",
            version_label="v3.0",
            effective_date="2026-01-01",
        )
        v3_suggestions = await detect_version_suggestions_for_document(
            session, workspace_id=entities.legal.id, document_id=policy_v3
        )
        assert len(v3_suggestions) >= 1
        for item in v3_suggestions:
            dismissed = await dismiss_suggestion(
                session,
                workspace_id=entities.legal.id,
                suggestion_id=item.id,
                actor_user_id=entities.user.id,
            )
            assert dismissed.status == VERSION_SUGGESTION_DISMISSED
        after_dismiss = await detect_version_suggestions_for_document(
            session, workspace_id=entities.legal.id, document_id=policy_v3
        )
        assert after_dismiss == []


async def test_manual_link_reorder_unlink_and_delete_cleanup(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        d1 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="policy_v1.pdf",
            title="Travel Policy",
        )
        d2 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="policy_v2.pdf",
            title="Travel Policy",
        )
        d3 = await _seed_doc_with_profile(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="policy_v3.pdf",
            title="Travel Policy",
        )

        group = await link_document_versions(
            session,
            workspace_id=entities.finance.id,
            newer_document_id=d2,
            older_document_id=d1,
            relation_kind=VERSION_RELATION_SUPERSEDES,
            actor_user_id=entities.user.id,
        )
        await link_document_versions(
            session,
            workspace_id=entities.finance.id,
            newer_document_id=d3,
            older_document_id=d2,
            actor_user_id=entities.user.id,
        )

        # Reorder to [d1, d3, d2] and switch to periodic_series
        await update_version_group(
            session,
            workspace_id=entities.finance.id,
            group_id=group.id,
            name="Travel Policy Series",
            relation_kind=VERSION_RELATION_PERIODIC_SERIES,
            member_order=[d1, d3, d2],
        )
        detail, _ = await load_document_version_detail(
            session, workspace_id=entities.finance.id, document_id=d2
        )
        assert detail is not None
        assert detail.name == "Travel Policy Series"
        assert detail.relation_kind == VERSION_RELATION_PERIODIC_SERIES
        assert [m.document_id for m in detail.members] == [d1, d3, d2]
        assert detail.members[-1].is_latest is True

        # Cleaning up d2 promotes d3 to latest.
        await cleanup_document_versions_on_unlink(
            session, workspace_id=entities.finance.id, document_id=d2
        )
        await session.commit()
        signals = await load_document_version_signals_for_workspace(
            session, workspace_id=entities.finance.id, document_ids=[d1, d3]
        )
        assert signals[d3].is_latest is True
        assert signals[d3].version_count == 2

        # Unlinking d1 leaves only d3 -> dissolves the group cleanly.
        assert await unlink_document_version(
            session, workspace_id=entities.finance.id, document_id=d1
        )
        remaining_groups = (
            await session.scalars(
                select(DocumentVersionGroup).where(DocumentVersionGroup.id == group.id)
            )
        ).all()
        remaining_members = (
            await session.scalars(
                select(DocumentVersionMember).where(DocumentVersionMember.group_id == group.id)
            )
        ).all()
        assert remaining_groups == []
        assert remaining_members == []

        with pytest.raises(AppError) as exc:
            await link_document_versions(
                session,
                workspace_id=entities.finance.id,
                newer_document_id=d3,
                older_document_id=d3,
            )
        assert exc.value.status_code == 422


async def test_version_suggestions_are_isolated_per_workspace(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        await _seed_doc_with_profile(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="MSA_Acme_v1.pdf",
            title="Master Services Agreement",
            doc_category="contract_agreement",
            document_number="MSA-2024-001",
            version_label="v1",
        )
        legal_doc = await _seed_doc_with_profile(
            session,
            workspace_id=entities.legal.id,
            uploader_id=entities.user.id,
            filename="MSA_Acme_v2.pdf",
            title="Master Services Agreement",
            doc_category="contract_agreement",
            document_number="MSA-2024-001",
            version_label="v2",
        )
        suggestions = await detect_version_suggestions_for_document(
            session, workspace_id=entities.legal.id, document_id=legal_doc
        )
        assert suggestions == []
        all_suggestions = (await session.scalars(select(DocumentVersionSuggestion))).all()
        assert all_suggestions == []
