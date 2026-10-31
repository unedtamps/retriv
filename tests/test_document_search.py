"""Cross-workspace document search, scoped by the caller's own memberships.

The point of these tests is the scope: a search must never name a file in a
workspace the caller cannot open, and narrowing by workspace must be a filter
over that scope rather than a way around it.
"""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WorkspaceMembership
from tests.conftest import SeededEntities
from tests.document_samples import pdf_bytes

pytestmark = pytest.mark.asyncio


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
