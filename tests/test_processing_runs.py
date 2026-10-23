"""Processing-status routes, scoped by the caller's own memberships.

These used to assert the scope sent to an upstream service over HTTP.
There is no upstream now, so the scope is enforced in the query itself,
and that is what these tests check: a run in a workspace the caller
cannot read is simply never selected, rather than being fetched and then
rejected.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    JOB_EXTRACT,
    RUN_EMBEDDING,
    RUN_QUEUED,
    RUN_READY,
    Document,
    DocumentWorkspace,
    ProcessingRun,
    WorkspaceMembership,
)
from tests.conftest import SeededEntities

pytestmark = pytest.mark.asyncio


async def seed_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    document_id: UUID | None = None,
    filename: str = "report.pdf",
    status: str = RUN_READY,
) -> ProcessingRun:
    document = Document(
        id=document_id or uuid4(),
        origin_workspace_id=workspace_id,
        original_filename=filename,
        detected_media_type="application/pdf",
        size_bytes=2048,
        sha256=uuid4().hex * 2,
        bucket="raw-documents",
        object_key=f"workspaces/{workspace_id}/objects/{uuid4().hex}/{uuid4()}",
    )
    run = ProcessingRun(
        id=uuid4(),
        document_id=document.id,
        origin_workspace_id=workspace_id,
        job_kind=JOB_EXTRACT,
        pipeline_version="2026.1",
        status=status,
        chunk_count=5,
        embedded_chunk_count=5 if status == RUN_READY else 0,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC) if status == RUN_READY else None,
    )
    link = DocumentWorkspace(
        workspace_id=workspace_id,
        document_id=document.id,
        filename=filename,
    )
    async with session_factory() as session:
        session.add_all([document, link, run])
        await session.commit()
    return run


async def viewer_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_processing_routes_aggregate_only_the_callers_workspaces(
    client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    await seed_run(
        db_session_factory,
        workspace_id=entities.finance.id,
        filename="finance.pdf",
        status=RUN_EMBEDDING,
    )
    await seed_run(
        db_session_factory,
        workspace_id=entities.legal.id,
        filename="legal.pdf",
    )

    active = await client.get("/v1/processing-runs/active", headers=auth_headers)
    recent = await client.get("/v1/processing-runs/recent", headers=auth_headers)

    assert active.status_code == recent.status_code == 200
    # finance is the only workspace with a non-terminal run
    assert active.json()["item"]["workspace_id"] == str(entities.finance.id)
    assert active.json()["item"]["filename"] == "finance.pdf"
    assert recent.json()["total"] == 2
    assert {item["workspace_id"] for item in recent.json()["items"]} == {
        str(entities.finance.id),
        str(entities.legal.id),
    }
    assert {item["filename"] for item in recent.json()["items"]} == {
        "finance.pdf",
        "legal.pdf",
    }


async def test_a_viewer_sees_only_their_own_workspace(
    client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    """Bob is a viewer of legal only, so finance runs are not his to see."""
    await seed_run(
        db_session_factory, workspace_id=entities.legal.id, filename="legal.pdf"
    )
    await seed_run(
        db_session_factory,
        workspace_id=entities.finance.id,
        filename="finance.pdf",
    )

    headers = await viewer_headers(client)
    recent = await client.get("/v1/processing-runs/recent", headers=headers)

    assert recent.status_code == 200
    assert recent.json()["total"] == 1
    assert [item["workspace_id"] for item in recent.json()["items"]] == [
        str(entities.legal.id)
    ]


async def test_a_run_outside_the_callers_scope_is_never_returned(
    client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    """The filter is in the query, so there is no window where a
    cross-workspace row is fetched and only then rejected."""
    await seed_run(
        db_session_factory, workspace_id=entities.finance.id, filename="finance.pdf"
    )

    headers = await viewer_headers(client)
    active = await client.get("/v1/processing-runs/active", headers=headers)
    recent = await client.get("/v1/processing-runs/recent", headers=headers)

    assert active.status_code == recent.status_code == 200
    assert active.json()["item"] is None
    assert recent.json()["items"] == []


async def test_a_user_with_no_workspaces_sees_nothing(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        result = await session.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.user_id == entities.user.id
            )
        )
        for row in result.scalars().all():
            await session.delete(row)
        await session.commit()

    response = await client.post(
        "/v1/auth/token",
        data={"username": "alice", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    await seed_run(
        db_session_factory, workspace_id=entities.finance.id, filename="finance.pdf"
    )
    active = await client.get("/v1/processing-runs/active", headers=headers)
    recent = await client.get("/v1/processing-runs/recent", headers=headers)

    assert active.json()["item"] is None
    assert recent.json()["items"] == []


async def test_queued_runs_are_reported_as_active(
    client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    """A run is visible as in-flight from the moment it is enqueued, which
    is what lets the UI show progress straight after a 202."""
    await seed_run(
        db_session_factory,
        workspace_id=entities.finance.id,
        filename="just-uploaded.pdf",
        status=RUN_QUEUED,
    )
    response = await client.get("/v1/processing-runs/active", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["item"]["status"] == RUN_QUEUED
    assert response.json()["item"]["embedded_chunk_count"] == 0
