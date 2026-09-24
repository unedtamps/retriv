import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import get_agent_client
from app.core.config import Settings
from app.db.models import AgentConversation, AgentConversationTurn, WorkspaceMembership
from app.security.context_delegation import decode_context_delegation
from tests.conftest import SeededEntities


def answer_source(
    workspace_id: UUID, *, text: str = "Current private source text"
) -> dict[str, object]:
    return {
        "workspace_id": str(workspace_id),
        "document_id": str(uuid4()),
        "processing_run_id": str(uuid4()),
        "chunk_id": str(uuid4()),
        "ordinal": 3,
        "score": 0.91,
        "content_hash": "a" * 64,
        "filename": "report.pdf",
        "section_path": ["Summary"],
        "page_numbers": [2],
        "text": text,
    }


def agent_result(workspace_id: UUID, answer: str) -> dict[str, object]:
    return {"answer": answer, "sources": [answer_source(workspace_id)]}


async def create_conversation(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> str:
    response = await client.post(
        "/v1/agent/conversations", headers=auth_headers, json={}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_private_conversation_turn_persists_answer_and_source_metadata_only(
    client: httpx.AsyncClient,
    app: Any,
    settings: Settings,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        assert set(body) == {"question", "scope_token", "history"}
        claims = decode_context_delegation(body["scope_token"], settings)
        assert set(claims.workspace_ids) == {entities.finance.id, entities.legal.id}
        assert body["history"] == []
        return httpx.Response(
            200,
            json=agent_result(entities.finance.id, "Finance grew 20%. [1]"),
        )

    agent_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    request_id = str(uuid4())
    try:
        response = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": request_id, "question": "What changed in finance?"},
        )
    finally:
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed"
    assert result["answer"] == "Finance grew 20%. [1]"
    assert len(result["sources"]) == 1
    assert "text" not in result["sources"][0]
    assert "Current private source text" not in response.text
    assert len(captured) == 1

    async with db_session_factory() as session:
        conversation = await session.get(AgentConversation, UUID(conversation_id))
        turn = await session.scalar(
            select(AgentConversationTurn).where(
                AgentConversationTurn.conversation_id == UUID(conversation_id)
            )
        )
        assert conversation is not None
        assert conversation.owner_user_id == entities.user.id
        assert conversation.title == "What changed in finance?"
        assert turn is not None
        assert turn.status == "completed"
        assert turn.source_workspace_ids == [str(entities.finance.id)]
        assert "text" not in turn.source_refs[0]


async def test_revoked_turn_is_visible_to_owner_but_not_sent_as_history_next_turn(
    client: httpx.AsyncClient,
    app: Any,
    settings: Settings,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    requests: list[dict[str, object]] = []
    calls = 0

    class RevokingAgentTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal calls
            body = json.loads(request.content)
            claims = decode_context_delegation(body["scope_token"], settings)
            requests.append(body)
            calls += 1
            if calls == 1:
                await remove_membership(
                    db_session_factory, entities.user.id, entities.finance.id
                )
                return httpx.Response(
                    200,
                    json=agent_result(entities.finance.id, "Finance answer [1]"),
                )
            assert set(claims.workspace_ids) == {entities.legal.id}
            return httpx.Response(
                200,
                json=agent_result(entities.legal.id, "Legal answer [1]"),
            )

    agent_client = httpx.AsyncClient(transport=RevokingAgentTransport())
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    try:
        first = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Question in finance"},
        )
        second = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Question in legal"},
        )
        history_response = await client.get(
            f"/v1/agent/conversations/{conversation_id}/turns", headers=auth_headers
        )
    finally:
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert requests[0]["history"] == []
    assert requests[1]["history"] == []
    assert history_response.status_code == 200
    assert [item["answer"] for item in history_response.json()["items"]] == [
        "Finance answer [1]",
        "Legal answer [1]",
    ]
    assert history_response.json()["items"][0]["context_available"] is False
    assert history_response.json()["items"][0]["sources"] == []
    assert history_response.json()["items"][1]["context_available"] is True


async def test_history_from_still_allowed_workspace_is_forwarded(
    client: httpx.AsyncClient,
    app: Any,
    settings: Settings,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    captured_history: list[list[dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        claims = decode_context_delegation(body["scope_token"], settings)
        assert entities.legal.id in claims.workspace_ids
        captured_history.append(body["history"])
        return httpx.Response(
            200,
            json=agent_result(entities.legal.id, "Legal follow-up [1]"),
        )

    agent_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    try:
        first = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Summarize legal"},
        )
        second = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Explain that"},
        )
    finally:
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert first.status_code == second.status_code == 200
    assert len(captured_history) == 2
    history = captured_history[1]
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[0]["content"] == "Summarize legal"
    assistant_content = cast(str, history[1]["content"])
    assert "Legal follow-up [1]" not in assistant_content
    assert "prior document-grounded answer" in assistant_content
    assert history[1]["source_workspace_ids"] == [str(entities.legal.id)]
    assert "sources" not in history[1]


async def test_conversation_ids_are_private_between_users(
    client: httpx.AsyncClient,
    app: Any,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    bob_login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    assert bob_login.status_code == 200
    bob_headers = {"Authorization": f"Bearer {bob_login.json()['access_token']}"}

    response = await client.get(
        f"/v1/agent/conversations/{conversation_id}/turns", headers=bob_headers
    )
    bob_list = await client.get("/v1/agent/conversations", headers=bob_headers)

    assert response.status_code == 404
    assert response.json()["code"] == "conversation_not_found"
    assert bob_list.status_code == 200
    assert bob_list.json()["items"] == []


async def test_user_can_create_and_list_multiple_private_conversations(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    first_id = await create_conversation(client, auth_headers)
    second_id = await create_conversation(client, auth_headers)
    response = await client.get("/v1/agent/conversations", headers=auth_headers)

    assert response.status_code == 200
    listed_ids = {item["id"] for item in response.json()["items"]}
    assert listed_ids == {first_id, second_id}


async def test_only_greeting_only_messages_bypass_search_access_and_model(
    client: httpx.AsyncClient,
    app: Any,
    entities: SeededEntities,
) -> None:
    login = await client.post(
        "/v1/auth/token",
        data={"username": entities.admin_user.username, "password": "admin-correct-password"},
    )
    assert login.status_code == 200
    admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    conversation_response = await client.post(
        "/v1/agent/conversations", headers=admin_headers, json={}
    )
    assert conversation_response.status_code == 201
    conversation_id = conversation_response.json()["id"]
    agent_calls = 0

    def fail_if_called(_: httpx.Request) -> httpx.Response:
        nonlocal agent_calls
        agent_calls += 1
        return httpx.Response(500)

    agent_client = httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called))
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    try:
        greeting = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=admin_headers,
            json={"request_id": str(uuid4()), "question": "Hello!!!"},
        )
        mixed = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=admin_headers,
            json={"request_id": str(uuid4()), "question": "Hello, what is in my documents?"},
        )
    finally:
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert greeting.status_code == 200, greeting.text
    assert greeting.json()["sources"] == []
    assert greeting.json()["answer"].startswith("Hello!")
    assert mixed.status_code == 403
    assert mixed.json()["code"] == "context_access_required"
    assert agent_calls == 0


async def test_request_id_replay_returns_completed_turn_without_calling_agent_again(
    client: httpx.AsyncClient,
    app: Any,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=agent_result(entities.finance.id, "Finance answer [1]"),
        )

    agent_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    request_id = str(uuid4())
    body = {"request_id": request_id, "question": "Finance question"}
    try:
        first = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json=body,
        )
        replay = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json=body,
        )
    finally:
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert first.status_code == replay.status_code == 200
    assert first.json()["id"] == replay.json()["id"]
    assert calls == 1


async def test_concurrent_turn_in_same_conversation_is_rejected_until_first_finishes(
    client: httpx.AsyncClient,
    app: Any,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)

    # Hold the first upstream response while a concurrent turn attempts to reserve the session.
    entered = asyncio.Event()
    finish = asyncio.Event()

    class WorkspaceAgentTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["question"] == "First message":
                entered.set()
                await finish.wait()
            return httpx.Response(
                200,
                json=agent_result(entities.finance.id, "Done [1]"),
            )

    agent_client = httpx.AsyncClient(transport=WorkspaceAgentTransport())
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    first_task = asyncio.create_task(
        client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "First message"},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Second message"},
        )
        assert second.status_code == 409
        assert second.json()["code"] == "conversation_turn_in_progress"
    finally:
        finish.set()
        first = await first_task
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert first.status_code == 200, first.text


async def test_late_answer_cannot_overwrite_turn_that_took_over_stale_reservation(
    client: httpx.AsyncClient,
    app: Any,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    class StaleAgentTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["question"] == "Slow first turn":
                first_entered.set()
                await release_first.wait()
                return httpx.Response(
                    200,
                    json=agent_result(entities.finance.id, "Late finance answer [1]"),
                )
            return httpx.Response(
                200,
                json=agent_result(entities.legal.id, "New legal answer [1]"),
            )

    agent_client = httpx.AsyncClient(transport=StaleAgentTransport())
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    first_task = asyncio.create_task(
        client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Slow first turn"},
        )
    )
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        async with db_session_factory() as session:
            await session.execute(
                update(AgentConversation)
                .where(AgentConversation.id == UUID(conversation_id))
                .values(active_turn_started_at=datetime.now(UTC) - timedelta(hours=1))
            )
            await session.commit()

        takeover = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "New turn after timeout"},
        )
        release_first.set()
        late = await first_task
        transcript = await client.get(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
        )
    finally:
        release_first.set()
        if not first_task.done():
            await first_task
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert takeover.status_code == 200, takeover.text
    assert late.status_code == 409
    turns = transcript.json()["items"]
    assert [turn["status"] for turn in turns] == ["failed", "completed"]
    assert turns[0]["failure_code"] == "turn_timeout"
    assert turns[1]["answer"] == "New legal answer [1]"


async def test_late_failure_cleanup_cannot_clear_newer_turn_reservation(
    client: httpx.AsyncClient,
    app: Any,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    class FailingStaleAgentTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["question"] == "Slow failing turn":
                first_entered.set()
                await release_first.wait()
                return httpx.Response(502, json={"detail": "provider unavailable"})
            return httpx.Response(
                200,
                json=agent_result(entities.legal.id, "New turn succeeded [1]"),
            )

    agent_client = httpx.AsyncClient(transport=FailingStaleAgentTransport())
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    first_task = asyncio.create_task(
        client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Slow failing turn"},
        )
    )
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        async with db_session_factory() as session:
            await session.execute(
                update(AgentConversation)
                .where(AgentConversation.id == UUID(conversation_id))
                .values(active_turn_started_at=datetime.now(UTC) - timedelta(hours=1))
            )
            await session.commit()

        takeover = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "New turn"},
        )
        release_first.set()
        late_failure = await first_task
        transcript = await client.get(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
        )
    finally:
        release_first.set()
        if not first_task.done():
            await first_task
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert takeover.status_code == 200, takeover.text
    assert late_failure.status_code == 503
    turns = transcript.json()["items"]
    assert [turn["status"] for turn in turns] == ["failed", "completed"]
    assert turns[0]["failure_code"] == "turn_timeout"
    assert turns[1]["answer"] == "New turn succeeded [1]"


async def test_failed_turn_releases_conversation_for_retry(
    client: httpx.AsyncClient,
    app: Any,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json=agent_result(entities.finance.id, "Recovered answer [1]"),
        )

    agent_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_agent_client] = lambda: agent_client
    request_id = str(uuid4())
    body = {"request_id": request_id, "question": "Retry this question"}
    try:
        failed = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json=body,
        )
        retried = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json=body,
        )
    finally:
        await agent_client.aclose()
        app.dependency_overrides.clear()

    assert failed.status_code == 503
    assert retried.status_code == 200, retried.text
    assert retried.json()["answer"] == "Recovered answer [1]"


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
