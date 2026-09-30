import json
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import get_agent_client
from app.core.config import Settings
from app.db.models import WorkspaceMembership
from app.security.context_delegation import decode_context_delegation
from tests.conftest import SeededEntities


def context_source(workspace_id: str) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "document_id": str(uuid4()),
        "processing_run_id": str(uuid4()),
        "chunk_id": str(uuid4()),
        "ordinal": 0,
        "score": 0.91,
        "content_hash": "a" * 64,
        "filename": "report.pdf",
        "section_path": ["Summary"],
        "page_numbers": [1],
        "text": "Revenue increased.",
    }


async def test_ask_delegates_request_time_workspace_scope(
    client: httpx.AsyncClient,
    app: Any,
    settings: Settings,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/answer"
        assert request.headers["authorization"] == (
            f"Bearer {settings.agent_api_token.get_secret_value()}"
        )
        return httpx.Response(
            200,
            json={
                "answer": "Revenue increased. [1]",
                "sources": [context_source(str(entities.finance.id))],
            },
        )

    agent_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    try:
        response = await client.post(
            "/v1/agent/ask",
            headers=auth_headers,
            json={"question": "What changed in revenue?"},
        )
    finally:
        await agent_client.aclose()

    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "Revenue increased. [1]"
    assert len(response.json()["sources"]) == 1
    assert "scope_token" not in response.text
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert set(body) == {"question", "scope_token"}
    claims = decode_context_delegation(body["scope_token"], settings)
    assert claims.sub == entities.user.id
    assert claims.auth_version == entities.user.auth_version
    assert set(claims.workspace_ids) == {entities.finance.id, entities.legal.id}


async def test_ask_rejects_agent_response_outside_issued_scope(
    client: httpx.AsyncClient,
    app: Any,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    leaked_text = "unauthorized-secret-paragraph"
    outside_scope = context_source(str(uuid4()))
    outside_scope["text"] = leaked_text
    agent_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"answer": "leaked [1]", "sources": [outside_scope]},
            )
        )
    )
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    try:
        response = await client.post(
            "/v1/agent/ask",
            headers=auth_headers,
            json={"question": "Tell me"},
        )
    finally:
        await agent_client.aclose()

    assert response.status_code == 503
    assert response.json()["code"] == "answer_service_unavailable"
    assert "leaked" not in response.text
    assert leaked_text not in response.text


async def test_revocation_applies_to_next_request_not_in_flight_request(
    client: httpx.AsyncClient,
    app: Any,
    settings: Settings,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    delegated_scopes: list[set[UUID]] = []
    calls = 0

    class RevokingAgentTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal calls
            body = json.loads(request.content)
            claims = decode_context_delegation(body["scope_token"], settings)
            delegated_scopes.append(set(claims.workspace_ids))
            calls += 1
            if calls == 1:
                await remove_membership(
                    db_session_factory, entities.user.id, entities.finance.id
                )
                return httpx.Response(
                    200,
                    json={
                        "answer": "Finance answer from the request-time scope [1]",
                        "sources": [context_source(str(entities.finance.id))],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "answer": "Legal remains available [1]",
                    "sources": [context_source(str(entities.legal.id))],
                },
            )

    agent_client = httpx.AsyncClient(transport=RevokingAgentTransport())
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    try:
        first = await client.post(
            "/v1/agent/ask",
            headers=auth_headers,
            json={"question": "Question before revocation"},
        )
        second = await client.post(
            "/v1/agent/ask",
            headers=auth_headers,
            json={"question": "Question after revocation"},
        )
    finally:
        await agent_client.aclose()

    assert first.status_code == 200, first.text
    assert first.json()["answer"] == "Finance answer from the request-time scope [1]"
    assert second.status_code == 200, second.text
    assert second.json()["answer"] == "Legal remains available [1]"
    assert delegated_scopes == [
        {entities.finance.id, entities.legal.id},
        {entities.legal.id},
    ]


async def test_platform_admin_without_workspace_membership_cannot_ask_context(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    login = await client.post(
        "/v1/auth/token",
        data={"username": entities.admin_user.username, "password": "admin-correct-password"},
    )
    assert login.status_code == 200

    response = await client.post(
        "/v1/agent/ask",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={"question": "Read finance documents"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "context_access_required"


async def remove_membership(
    factory: async_sessionmaker[AsyncSession], user_id: UUID, workspace_id: UUID
) -> None:
    async with factory() as session:
        await session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.workspace_id == workspace_id,
            )
        )
        await session.commit()
