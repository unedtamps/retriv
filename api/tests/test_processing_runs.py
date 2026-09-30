import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from app.api.dependencies import get_processing_api_client
from tests.conftest import SeededEntities


def processing_run(workspace_id: str, status: str = "ready") -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": str(uuid4()),
        "workspace_id": workspace_id,
        "document_id": str(uuid4()),
        "filename": "report.pdf",
        "job_kind": "extract",
        "status": status,
        "attempts": 1,
        "chunk_count": 5,
        "embedded_chunk_count": 5,
        "created_at": now.isoformat(),
        "started_at": now.isoformat(),
        "completed_at": now.isoformat() if status == "ready" else None,
    }


async def install_internal_client(
    app: Any,
    handler: httpx.AsyncBaseTransport,
) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=handler, base_url="http://internal.test")

    async def override_client() -> AsyncIterator[httpx.AsyncClient]:
        yield client

    app.dependency_overrides[get_processing_api_client] = override_client
    return client


async def viewer_headers(
    client: httpx.AsyncClient,
) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_processing_routes_aggregate_only_signed_in_user_workspaces(
    app: Any,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    settings: Any,
) -> None:
    requests: list[httpx.Request] = []
    runs = [
        processing_run(str(entities.finance.id), "embedding"),
        processing_run(str(entities.legal.id), "ready"),
    ]

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/active"):
            return httpx.Response(200, json={"item": runs[0]})
        return httpx.Response(
            200,
            json={
                "items": runs,
                "total": len(runs),
                "since": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            },
        )

    internal_client = await install_internal_client(app, httpx.MockTransport(respond))
    try:
        active = await client.get("/v1/processing-runs/active", headers=auth_headers)
        recent = await client.get("/v1/processing-runs/recent", headers=auth_headers)
    finally:
        await internal_client.aclose()

    assert active.status_code == recent.status_code == 200
    assert active.json()["item"]["workspace_id"] == str(entities.finance.id)
    assert recent.json()["total"] == 2
    assert {item["workspace_id"] for item in recent.json()["items"]} == {
        str(entities.finance.id),
        str(entities.legal.id),
    }
    assert len(requests) == 2
    for request in requests:
        assert request.headers["authorization"] == (
            f"Bearer {settings.processing_api_token.get_secret_value()}"
        )
        assert set(json.loads(request.content)["workspace_ids"]) == {
            str(entities.finance.id),
            str(entities.legal.id),
        }


async def test_viewer_processing_scope_contains_only_their_readable_workspaces(
    app: Any,
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/active"):
            return httpx.Response(200, json={"item": None})
        return httpx.Response(
            200,
            json={
                "items": [],
                "total": 0,
                "since": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            },
        )

    internal_client = await install_internal_client(app, httpx.MockTransport(respond))
    try:
        headers = await viewer_headers(client)
        active = await client.get("/v1/processing-runs/active", headers=headers)
        recent = await client.get("/v1/processing-runs/recent", headers=headers)
    finally:
        await internal_client.aclose()

    assert active.status_code == recent.status_code == 200
    assert len(requests) == 2
    for request in requests:
        assert json.loads(request.content)["workspace_ids"] == [str(entities.legal.id)]


async def test_processing_feed_rejects_an_internal_run_outside_user_scope(
    app: Any,
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [processing_run(str(entities.finance.id))],
                "total": 1,
                "since": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            },
        )

    internal_client = await install_internal_client(app, httpx.MockTransport(respond))
    try:
        headers = await viewer_headers(client)
        response = await client.get("/v1/processing-runs/recent", headers=headers)
    finally:
        await internal_client.aclose()

    assert response.status_code == 503
    assert response.json()["code"] == "processing_service_unavailable"
