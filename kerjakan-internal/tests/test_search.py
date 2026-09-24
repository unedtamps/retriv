from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.db.models import ProcessingRun
from tests.conftest import ServiceContext, ready_event, send_event


async def test_semantic_search_filters_qdrant_and_postgres_by_workspace(
    client: httpx.AsyncClient,
    context: ServiceContext,
    internal_api_headers: dict[str, str],
) -> None:
    first_workspace = uuid4()
    other_workspace = uuid4()
    first_event = ready_event(
        b"first workspace document", workspace_id=first_workspace
    )
    other_event = ready_event(b"other workspace document", workspace_id=other_workspace)
    for event in (first_event, other_event):
        data = event["data"]
        context.storage.objects[(data["bucket"], data["object_key"])] = (
            b"first workspace document"
            if data["workspace_id"] == str(first_workspace)
            else b"other workspace document"
        )
        assert (await send_event(client, context.settings, event)).status_code == 202

    worker = context.worker()
    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert len(context.vector_store.points) == 4

    response = await client.post(
        "/internal/context/search",
        headers=internal_api_headers,
        json={
            "workspace_ids": [str(first_workspace)],
            "query": "what is in this workspace?",
            "limit": 8,
        },
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 2, {
        "items": items,
        "search_calls": context.vector_store.search_calls,
        "points": [point.payload for point in context.vector_store.points.values()],
    }
    assert {item["workspace_id"] for item in items} == {str(first_workspace)}
    assert all(
        item["text"].startswith("Annual report")
        or "Operating costs" in item["text"]
        for item in items
    )
    assert context.vector_store.search_calls[-1][0] == [first_workspace]

    async with context.session_factory() as session:
        active_runs = (
            await session.scalars(
                select(ProcessingRun).where(
                    ProcessingRun.workspace_id == first_workspace
                )
            )
        ).all()
    assert len(active_runs) == 1
    assert active_runs[0].embedded_chunk_count == active_runs[0].chunk_count


async def test_semantic_search_requires_internal_service_authentication(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/internal/context/search",
        json={"workspace_ids": [str(uuid4())], "query": "sensitive context"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_service_credentials"


@pytest.mark.parametrize(
    "workspace_ids,query",
    [([], "context"), ([str(uuid4())], "  ")],
)
async def test_semantic_search_rejects_empty_scope_or_query(
    client: httpx.AsyncClient,
    internal_api_headers: dict[str, str],
    workspace_ids: list[str],
    query: str,
) -> None:
    response = await client.post(
        "/internal/context/search",
        headers=internal_api_headers,
        json={"workspace_ids": workspace_ids, "query": query},
    )

    assert response.status_code == 422
