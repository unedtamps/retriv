"""Cross-workspace document search, scoped by the caller's own memberships.

The point of these tests is the scope: a search must never name a file in a
workspace the caller cannot open, and narrowing by workspace must be a filter
over that scope rather than a way around it.
"""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WorkspaceMembership
from tests.conftest import SeededEntities
from tests.document_samples import pdf_bytes


async def upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    workspace_id: object,
    filename: str,
) -> httpx.Response:
    return await client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        headers=headers,
        files={"file": (filename, pdf_bytes(), "application/pdf")},
    )


async def viewer_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_search_matches_across_every_readable_workspace(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    """Alice owns both workspaces, so one search spans both."""
    await upload(client, auth_headers, entities.finance.id, "quarterly-report.pdf")
    await upload(client, auth_headers, entities.legal.id, "quarterly-summary.pdf")

    response = await client.get(
        "/v1/documents/search", headers=auth_headers, params={"q": "quarterly"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    by_workspace = {item["workspace_id"]: item for item in body["items"]}
    assert set(by_workspace) == {str(entities.finance.id), str(entities.legal.id)}
    assert by_workspace[str(entities.finance.id)]["workspace_name"] == "Finance"
    assert by_workspace[str(entities.legal.id)]["workspace_name"] == "Legal"
    assert {item["filename"] for item in body["items"]} == {
        "quarterly-report.pdf",
        "quarterly-summary.pdf",
    }


async def test_search_never_leaves_the_callers_workspaces(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    """Bob is a viewer of legal only, so a finance document is not his to find."""
    await upload(client, auth_headers, entities.finance.id, "secret-finance.pdf")
    await upload(client, auth_headers, entities.legal.id, "public-legal.pdf")

    headers = await viewer_headers(client)
    response = await client.get("/v1/documents/search", headers=headers, params={"q": ".pdf"})

    assert response.status_code == 200
    body = response.json()
    assert [item["filename"] for item in body["items"]] == ["public-legal.pdf"]
    assert body["total"] == 1


async def test_search_ranks_the_exact_name_first(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    await upload(client, auth_headers, entities.finance.id, "policy-handbook-2026.pdf")
    await upload(client, auth_headers, entities.finance.id, "policy.pdf")

    response = await client.get(
        "/v1/documents/search", headers=auth_headers, params={"q": "policy.pdf"}
    )

    assert response.status_code == 200
    assert [item["filename"] for item in response.json()["items"]][0] == "policy.pdf"


async def test_a_wildcard_is_treated_as_a_literal(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    await upload(client, auth_headers, entities.finance.id, "plain-name.pdf")

    response = await client.get("/v1/documents/search", headers=auth_headers, params={"q": "%"})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_a_user_with_no_workspaces_finds_nothing(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        rows = await session.execute(
            select(WorkspaceMembership).where(WorkspaceMembership.user_id == entities.user.id)
        )
        for row in rows.scalars().all():
            await session.delete(row)
        await session.commit()

    response = await client.post(
        "/v1/auth/token",
        data={"username": "alice", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    search = await client.get("/v1/documents/search", headers=headers, params={"q": "pdf"})

    assert search.status_code == 200
    assert search.json() == {"items": [], "total": 0}


async def test_narrowing_to_a_workspace_outside_the_scope_matches_nothing(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    """Narrowing is a filter over the scope, not a way around it."""
    await upload(client, auth_headers, entities.finance.id, "only-finance.pdf")

    headers = await viewer_headers(client)
    response = await client.get(
        "/v1/documents/search",
        headers=headers,
        params={"q": "only-finance", "workspace_id": str(entities.finance.id)},
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_search_matches_extracted_profile_metadata_and_ranks_latest_first(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A query for 'Apple 2025' finds 10-K-2025-As-Filed.pdf via DocumentProfile."""
    from uuid import UUID

    from app.db.models import PROFILE_STATUS_ENRICHED, DocumentProfile
    from app.documents.intelligence import build_search_text
    from app.documents.search import find_documents
    from app.documents.versions import link_document_versions

    r1 = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents",
        headers=auth_headers,
        files={"file": ("10-K-2024-As-Filed.pdf", pdf_bytes("Apple FY2024"), "application/pdf")},
    )
    r2 = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents",
        headers=auth_headers,
        files={"file": ("10-K-2025-As-Filed.pdf", pdf_bytes("Apple FY2025"), "application/pdf")},
    )
    assert r1.status_code == 202
    assert r2.status_code == 202
    doc_2024 = UUID(r1.json()["id"])
    doc_2025 = UUID(r2.json()["id"])

    async with db_session_factory() as session, session.begin():
        for doc_id, period in ((doc_2024, "FY2024"), (doc_2025, "FY2025")):
            entities_payload = [
                {"name": "Apple Inc.", "normalized": "apple", "type": "company", "aliases": []}
            ]
            session.add(
                DocumentProfile(
                    document_id=doc_id,
                    status=PROFILE_STATUS_ENRICHED,
                    title=f"Apple Inc. Form 10-K {period}",
                    doc_category="financial_report",
                    doc_subtype="10-K",
                    primary_entities=entities_payload,
                    counterparties=[],
                    period_label=period,
                    search_text=build_search_text(
                        title=f"Apple Inc. Form 10-K {period}",
                        doc_category="financial_report",
                        doc_subtype="10-K",
                        primary_entities=entities_payload,
                        counterparties=[],
                        period_label=period,
                        effective_date=None,
                        document_number=None,
                        version_label=None,
                        summary=f"Annual report for Apple Inc. for {period}.",
                        topics=[],
                    ),
                )
            )
        await session.flush()
        await link_document_versions(
            session,
            workspace_id=entities.finance.id,
            older_document_id=doc_2024,
            newer_document_id=doc_2025,
            relation_kind="supersedes",
            actor_user_id=entities.user.id,
            commit=False,
        )

    async with db_session_factory() as session:
        only_2025, total_2025 = await find_documents(
            session,
            [entities.finance.id],
            pattern="Apple 2025",
            limit=10,
        )
        assert total_2025 == 1
        assert only_2025[0].document_id == doc_2025
        assert only_2025[0].primary_entities == ("Apple Inc.",)
        assert only_2025[0].version_signal is not None
        assert only_2025[0].version_signal.is_latest is True
        assert only_2025[0].version_signal.is_superseded is False

        both, total_both = await find_documents(
            session,
            [entities.finance.id],
            pattern="Apple 10-K",
            limit=10,
        )
        assert total_both == 2
        # Latest non-superseded document is ranked ahead of the superseded revision.
        assert both[0].document_id == doc_2025
        assert both[1].document_id == doc_2024
        assert both[1].version_signal is not None
        assert both[1].version_signal.is_superseded is True
        assert both[1].version_signal.latest_document_id == doc_2025

