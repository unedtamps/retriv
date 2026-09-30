"""Conversation turns.

The agent is called in-process now, so these tests assert two things the
old suite could not check directly: that the route derived the workspace
scope from the caller's own memberships before calling the harness, and
that no source outside that scope reaches the transcript.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.schemas import AnswerResponse, ContextSource
from app.db.models import AgentConversation, AgentConversationTurn, WorkspaceMembership
from tests.conftest import FakeAnswerHarness, SeededEntities

pytestmark = pytest.mark.asyncio


def answer_source(
    workspace_id: UUID, *, text: str = "Current private source text"
) -> ContextSource:
    return ContextSource(
        workspace_id=workspace_id,
        document_id=uuid4(),
        processing_run_id=uuid4(),
        chunk_id=uuid4(),
        ordinal=3,
        score=0.91,
        content_hash="a" * 64,
        filename="report.pdf",
        section_path=["Summary"],
        page_numbers=[2],
        text=text,
    )


def agent_result(workspace_id: UUID, answer: str) -> AnswerResponse:
    return AnswerResponse(answer=answer, sources=[answer_source(workspace_id)])


async def create_conversation(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> str:
    response = await client.post(
        "/v1/agent/conversations", headers=auth_headers, json={}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_turn_persists_answer_and_source_metadata_only(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    answer_harness.results = [
        agent_result(entities.finance.id, "Finance grew 20%. [1]")
    ]

    response = await client.post(
        f"/v1/agent/conversations/{conversation_id}/turns",
        headers=auth_headers,
        json={"request_id": str(uuid4()), "question": "What changed in finance?"},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed"
    assert result["answer"] == "Finance grew 20%. [1]"
    assert len(result["sources"]) == 1
    # The chunk text stays on the server; only provenance crosses out.
    assert "text" not in result["sources"][0]
    assert "Current private source text" not in response.text

    # The scope handed to the harness is derived from the caller's own
    # memberships: alice owns finance and legal, so both are in scope.
    assert len(answer_harness.calls) == 1
    question, workspace_ids, history = answer_harness.calls[0]
    assert question == "What changed in finance?"
    assert set(workspace_ids) == {entities.finance.id, entities.legal.id}
    assert history == []

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


async def test_a_source_outside_the_derived_scope_is_refused(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    """Egress check. The retriever already rejects an out-of-scope point,
    so getting one here means the harness itself drifted: refuse the
    whole answer rather than return part of it."""
    conversation_id = await create_conversation(client, auth_headers)
    stranger = uuid4()
    answer_harness.results = [agent_result(stranger, "Someone else's data [1]")]

    response = await client.post(
        f"/v1/agent/conversations/{conversation_id}/turns",
        headers=auth_headers,
        json={"request_id": str(uuid4()), "question": "What changed in finance?"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "context_scope_violation"


async def test_a_revoked_workspace_hides_the_prior_answer_but_keeps_the_turn(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Losing access to a workspace must retroactively blank the answers
    that cited it, without deleting the conversation."""
    conversation_id = await create_conversation(client, auth_headers)
    answer_harness.results = [
        agent_result(entities.finance.id, "Finance answer [1]"),
        agent_result(entities.legal.id, "Legal answer [1]"),
    ]

    first = await client.post(
        f"/v1/agent/conversations/{conversation_id}/turns",
        headers=auth_headers,
        json={"request_id": str(uuid4()), "question": "Question in finance"},
    )
    await remove_membership(
        db_session_factory, entities.user.id, entities.finance.id
    )
    second = await client.post(
        f"/v1/agent/conversations/{conversation_id}/turns",
        headers=auth_headers,
        json={"request_id": str(uuid4()), "question": "Question in legal"},
    )
    history = await client.get(
        f"/v1/agent/conversations/{conversation_id}/turns", headers=auth_headers
    )

    assert first.status_code == second.status_code == 200
    # The second call saw only legal: the scope is re-derived per turn.
    assert set(answer_harness.calls[1][1]) == {entities.legal.id}
    # The finance turn must not be replayed as history now it is revoked.
    assert answer_harness.calls[1][2] == []

    assert history.status_code == 200
    items = history.json()["items"]
    assert [item["answer"] for item in items] == ["Finance answer [1]", "Legal answer [1]"]
    assert items[0]["context_available"] is False
    assert items[0]["sources"] == []
    assert items[1]["context_available"] is True


async def test_history_from_a_still_allowed_workspace_is_forwarded(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    answer_harness.results = [
        agent_result(entities.legal.id, "Legal follow-up [1]")
    ]

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

    assert first.status_code == second.status_code == 200
    assert len(answer_harness.calls) == 2
    history = answer_harness.calls[1][2]
    assert [message.role for message in history] == ["user", "assistant"]
    assert history[0].content == "Summarize legal"
    # The prior assistant text is replaced: history is continuity, not
    # evidence, so the model must not cite it.
    assert "Legal follow-up [1]" not in history[1].content
    assert "prior document-grounded answer" in history[1].content
    assert history[1].source_workspace_ids == [entities.legal.id]


async def test_conversation_ids_are_private_between_users(
    client: httpx.AsyncClient,
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


async def test_only_greeting_only_messages_bypass_the_model(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
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

    assert greeting.status_code == 200, greeting.text
    assert greeting.json()["sources"] == []
    assert greeting.json()["answer"].startswith("Hello!")
    # The admin is not a member of any workspace, so the real question
    # has nowhere to search and the harness is never reached.
    assert mixed.status_code == 403
    assert mixed.json()["code"] == "context_access_required"
    assert answer_harness.calls == []


async def test_request_id_replay_returns_completed_turn_without_recalling(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    answer_harness.results = [
        agent_result(entities.finance.id, "Finance answer [1]")
    ]
    request_id = str(uuid4())
    body = {"request_id": request_id, "question": "Finance question"}

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

    assert first.status_code == replay.status_code == 200
    assert first.json()["id"] == replay.json()["id"]
    assert len(answer_harness.calls) == 1


async def test_concurrent_turn_in_same_conversation_is_rejected(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    answer_harness.gate = asyncio.Event()
    answer_harness.gate_question = "First message"
    answer_harness.results = [
        agent_result(entities.finance.id, "Done [1]")
    ]

    first_task = asyncio.create_task(
        client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "First message"},
        )
    )
    try:
        await asyncio.wait_for(answer_harness.entered.wait(), timeout=5)
        second = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Second message"},
        )
        assert second.status_code == 409
        assert second.json()["code"] == "conversation_turn_in_progress"
    finally:
        answer_harness.gate.set()
        first = await first_task

    assert first.status_code == 200, first.text


async def test_late_answer_cannot_overwrite_a_turn_that_took_over(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    answer_harness.gate = asyncio.Event()
    answer_harness.gate_question = "Slow first turn"
    answer_harness.results = [
        agent_result(entities.finance.id, "Late finance answer [1]"),
        agent_result(entities.legal.id, "New legal answer [1]"),
    ]

    first_task = asyncio.create_task(
        client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Slow first turn"},
        )
    )
    try:
        await asyncio.wait_for(answer_harness.entered.wait(), timeout=5)
        async with db_session_factory() as session:
            await session.execute(
                update(AgentConversation)
                .where(AgentConversation.id == UUID(conversation_id))
                .values(
                    active_turn_started_at=datetime.now(UTC) - timedelta(hours=1)
                )
            )
            await session.commit()

        takeover = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "New turn after timeout"},
        )
        answer_harness.gate.set()
        late = await first_task
        transcript = await client.get(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
        )
    finally:
        answer_harness.gate.set()
        if not first_task.done():
            await first_task

    assert takeover.status_code == 200, takeover.text
    assert late.status_code == 409
    turns = transcript.json()["items"]
    assert [turn["status"] for turn in turns] == ["failed", "completed"]
    assert turns[0]["failure_code"] == "turn_timeout"
    assert turns[1]["answer"] == "New legal answer [1]"


async def test_late_failure_cannot_clear_a_newer_reservation(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    answer_harness.gate = asyncio.Event()
    answer_harness.gate_question = "Slow failing turn"
    answer_harness.results = [
        RuntimeError("provider unavailable"),
        agent_result(entities.legal.id, "New turn succeeded [1]"),
    ]

    first_task = asyncio.create_task(
        client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "Slow failing turn"},
        )
    )
    try:
        await asyncio.wait_for(answer_harness.entered.wait(), timeout=5)
        async with db_session_factory() as session:
            await session.execute(
                update(AgentConversation)
                .where(AgentConversation.id == UUID(conversation_id))
                .values(
                    active_turn_started_at=datetime.now(UTC) - timedelta(hours=1)
                )
            )
            await session.commit()

        takeover = await client.post(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
            json={"request_id": str(uuid4()), "question": "New turn"},
        )
        answer_harness.gate.set()
        late_failure = await first_task
        transcript = await client.get(
            f"/v1/agent/conversations/{conversation_id}/turns",
            headers=auth_headers,
        )
    finally:
        answer_harness.gate.set()
        if not first_task.done():
            await first_task

    assert takeover.status_code == 200, takeover.text
    assert late_failure.status_code == 503
    turns = transcript.json()["items"]
    assert [turn["status"] for turn in turns] == ["failed", "completed"]
    assert turns[0]["failure_code"] == "turn_timeout"
    assert turns[1]["answer"] == "New turn succeeded [1]"


async def test_failed_turn_releases_the_conversation_for_retry(
    client: httpx.AsyncClient,
    answer_harness: FakeAnswerHarness,
    entities: SeededEntities,
    auth_headers: dict[str, str],
) -> None:
    conversation_id = await create_conversation(client, auth_headers)
    answer_harness.results = [
        RuntimeError("provider unavailable"),
        agent_result(entities.finance.id, "Recovered answer [1]"),
    ]
    request_id = str(uuid4())
    body = {"request_id": request_id, "question": "Retry this question"}

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
