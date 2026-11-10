"""The agent harness: the configuration gate, the workspace scope, the loop.

The loop is tested through the HTTP endpoint with a scripted model behind
``provider_for``, because what matters is the wiring: a configured model
drives the toolkit inside the caller's scope, tool results flow back to the
model, and the dashboard sees the same events the model produced.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.chat import ChatTurn
from app.agents.harness import MAX_SEARCH_LIMIT, _execute_tool
from app.agents.toolkit import ToolRejection
from app.db.models import (
    JOB_EXTRACT,
    AgentModelSetting,
    Document,
    DocumentWorkspace,
    ProcessingRun,
)
from app.mcp.runtime import McpRuntime
from app.retrieval.context import ContextRetriever, RetrievalUnavailable
from app.retrieval.navigation import DocumentNavigator
from app.retrieval.schemas import ContextSource
from app.retrieval.sql import CatalogueReader
from tests.conftest import InMemoryObjectStorage

OWNER_PASSWORD = "correct-horse-battery-staple"
OUTSIDER_PASSWORD = "another-correct-password"


class FakeRetriever(ContextRetriever):
    """Returns one queued result, recording the scope it was handed."""

    def __init__(self) -> None:
        self.scopes: list[list[Any]] = []
        self.results: list[list[ContextSource]] = []
        self.error: Exception | None = None

    async def retrieve(
        self,
        query: str,
        workspace_ids: list[Any],
        limit: int,
        *,
        budget: int | None = None,
        min_score: float | None = None,
        document_id: Any = None,
    ) -> list[ContextSource]:
        del query, limit, budget, min_score, document_id
        self.scopes.append(list(workspace_ids))
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else []


class FakeChatProvider:
    """Plays the model: a scripted list of turns, then it asserts it stopped."""

    def __init__(self, turns: list[ChatTurn]) -> None:
        self.turns = list(turns)
        self.prompts: list[list[dict[str, Any]]] = []
        self.closed = False

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatTurn:
        del tools
        self.prompts.append([dict(message) for message in messages])
        if not self.turns:
            raise AssertionError("the model was asked for more turns than scripted")
        return self.turns.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def a_source(workspace_id: Any) -> ContextSource:
    document_id = uuid4()
    return ContextSource(
        workspace_id=workspace_id,
        document_id=document_id,
        processing_run_id=uuid4(),
        chunk_id=uuid4(),
        ordinal=3,
        score=0.9,
        content_hash=hashlib.sha256(b"source").hexdigest(),
        filename="report.pdf",
        section_path=["Revenue"],
        page_numbers=[2],
        text="Net revenue rose by a tenth.",
    )


async def login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/token",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def sse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in body.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :]))
    return events


@pytest_asyncio.fixture
async def runtime_env(
    app: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
) -> AsyncIterator[FakeRetriever]:
    """A live retrieval runtime in the box, with a fake retriever inside."""
    retriever = FakeRetriever()
    app.state.mcp.set(
        McpRuntime(
            settings=settings,
            session_factory=db_session_factory,
            retriever=retriever,
            navigator=DocumentNavigator(settings, db_session_factory),
            row_query=None,
            catalogue_reader=CatalogueReader(db_session_factory),
            storage=InMemoryObjectStorage(),
            artifact_storage=InMemoryObjectStorage(),
        )
    )
    yield retriever
    app.state.mcp.set(None)


@pytest.fixture
def scripted_model(monkeypatch: pytest.MonkeyPatch) -> FakeChatProvider:
    provider = FakeChatProvider([])
    monkeypatch.setattr(
        "app.routes.agent.provider_for", lambda config, session_id=None: provider
    )
    return provider


async def seed_model_config(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session, session.begin():
        session.add(
            AgentModelSetting(
                id=1,
                base_url="https://gateway.test/v1",
                api_key="test-model-key",
                model="test-model",
            )
        )


async def seed_retrieval_result(
    runtime_env: FakeRetriever, workspace_id: Any
) -> None:
    runtime_env.results.append([a_source(workspace_id)])


async def seed_queryable_csv(
    db_session_factory: async_sessionmaker[AsyncSession],
    workspace_id: Any,
    *,
    filename: str = "invoices.csv",
) -> None:
    async with db_session_factory() as session, session.begin():
        document = Document(
            origin_workspace_id=workspace_id,
            original_filename=filename,
            detected_media_type="text/csv",
            size_bytes=20,
            sha256=uuid4().hex * 2,
            bucket="raw-documents",
            object_key=f"raw-documents/{workspace_id}/{uuid4()}",
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentWorkspace(
                workspace_id=workspace_id,
                document_id=document.id,
                filename=filename,
            )
        )
        run = ProcessingRun(
            document_id=document.id,
            origin_workspace_id=workspace_id,
            job_kind=JOB_EXTRACT,
            pipeline_version="2026.1",
            status="ready",
            row_count=2,
            csv_columns=[{"sheet": "", "columns": ["name", "amount"], "rows": 2}],
        )
        session.add(run)
        await session.flush()
        document.active_processing_run_id = run.id


async def test_chat_without_a_configured_model_is_refused(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
) -> None:
    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "What is in these documents?"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "agent_not_configured"


async def test_chat_refuses_a_user_with_no_memberships(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scope is the membership list: an admin of no workspaces has none."""
    del entities
    await seed_model_config(db_session_factory)
    headers = await login(client, "admin", "admin-correct-password")
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "What is in these documents?"},
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "no_searchable_workspaces"


async def test_chat_streams_tool_calls_to_an_answer(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    await seed_retrieval_result(runtime_env, entities.finance.id)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(id="call-1", name="search_context", arguments={"query": "revenue"})
            ],
        ),
        ChatTurn(content="Net revenue rose by a tenth [1], per report.pdf.", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "How did revenue move?"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")

    events = sse_events(response.text)
    kinds = [event["type"] for event in events]
    assert kinds == ["turn_start", "tool_call", "tool_result", "final"]
    assert events[1]["name"] == "search_context"
    assert "Net revenue rose by a tenth." in events[2]["preview"]
    assert events[3]["content"].startswith("Net revenue rose")
    # The labels in the prose are the ones the payload names; the client must
    # not have to reconstruct them from a possibly-truncated preview.
    assert [source["index"] for source in events[3]["sources"]] == [1]
    assert events[3]["sources"][0]["filename"] == "report.pdf"
    assert events[3]["sources"][0]["ordinal"] == 3
    assert events[3]["sources"][0]["page_numbers"] == [2]

    assert len(runtime_env.scopes) == 1
    assert set(runtime_env.scopes[0]) == {entities.finance.id, entities.legal.id}
    tool_message = next(
        message
        for message in scripted_model.prompts[1]
        if message.get("role") == "tool"
    )
    assert "untrusted_document_text" in tool_message["content"]
    assert scripted_model.closed


async def test_chat_can_list_queryable_files_without_a_row_reader(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    """The catalogue is metadata, so the chat agent can read it with no SQL reader."""
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    await seed_queryable_csv(db_session_factory, entities.finance.id)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="list_queryable_files",
                    arguments={"pattern": "invoices"},
                )
            ],
        ),
        ChatTurn(content="Finance has invoices.csv.", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "Which invoice files can I query?"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = sse_events(response.text)
    assert [event["type"] for event in events] == [
        "turn_start",
        "tool_call",
        "tool_result",
        "final",
    ]
    assert events[1]["name"] == "list_queryable_files"
    assert "invoices.csv" in events[2]["preview"]
    assert "name, amount" in events[2]["preview"]
    assert events[3]["content"] == "Finance has invoices.csv."


async def test_a_tool_rejection_is_fed_back_to_the_model(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[ToolCall(id="call-1", name="explode", arguments={})],
        ),
        ChatTurn(content="Understood; I cannot do that.", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "explode the database"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = sse_events(response.text)
    tool_message = next(
        message
        for message in scripted_model.prompts[1]
        if message.get("role") == "tool"
    )
    assert tool_message["content"].startswith("Rejected: Unknown tool")
    assert events[-1]["type"] == "final"


async def test_chat_scope_is_the_caller_memberships(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    """A viewer on one workspace searches that workspace, and nothing else.

    Bob is a viewer on Legal and has no standing in Finance: the scope the
    retriever receives names Legal alone, so Finance's documents are not
    merely filtered out of answers -- they are never reached at all.
    """
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    await seed_retrieval_result(runtime_env, entities.legal.id)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(id="call-1", name="search_context", arguments={"query": "holdings"})
            ],
        ),
        ChatTurn(content="Here is what Legal holds.", tool_calls=[]),
    ]
    headers = await login(client, "bob", OUTSIDER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "What do we have?"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert runtime_env.scopes == [[entities.legal.id]]


async def test_a_document_tool_outside_the_scope_is_rejected(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    """Naming a workspace the caller cannot read is a correction, not a leak.

    The model hands read_document Finance's ids while scoped to Legal. The
    toolkit refuses before anything is read, the refusal is fed back as the
    tool result, and the run finishes with an answer instead of an error.
    """
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="read_document",
                    arguments={
                        "document_id": str(uuid4()),
                        "workspace_id": str(entities.finance.id),
                    },
                )
            ],
        ),
        ChatTurn(content="I cannot read that workspace's documents.", tool_calls=[]),
    ]

    headers = await login(client, "bob", OUTSIDER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "Read the finance report."},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = sse_events(response.text)
    tool_message = next(
        message for message in scripted_model.prompts[1] if message.get("role") == "tool"
    )
    assert tool_message["content"].startswith("Rejected: That workspace is not in scope")
    assert events[-1]["type"] == "final"


async def seed_document(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: Any,
    user_id: Any,
) -> Any:
    """A document linked into one workspace, as the upload path leaves it."""
    async with session_factory() as session:
        document = Document(
            origin_workspace_id=workspace_id,
            original_filename="legal-deck.pdf",
            detected_media_type="application/pdf",
            size_bytes=1024,
            sha256=uuid4().hex * 2,
            bucket="raw-documents",
            object_key=f"raw-documents/{workspace_id}/{uuid4()}",
            created_by_user_id=user_id,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentWorkspace(
                workspace_id=workspace_id,
                document_id=document.id,
                filename="legal-deck.pdf",
                created_by_user_id=user_id,
            )
        )
        await session.commit()
        return document.id


async def test_download_refuses_a_role_without_the_permission(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    """A viewer may read a workspace but not hand out its file.

    The download tool re-checks the named workspace's permission at call
    time; the 403 comes back as a tool rejection the model can act on, and
    the stream survives instead of dying mid-run.
    """
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    document_id = await seed_document(db_session_factory, entities.legal.id, entities.user.id)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="get_document_link",
                    arguments={
                        "document_id": str(document_id),
                        "workspace_id": str(entities.legal.id),
                    },
                )
            ],
        ),
        ChatTurn(content="I cannot download that file for you.", tool_calls=[]),
    ]

    headers = await login(client, "bob", OUTSIDER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "Download the legal deck."},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = sse_events(response.text)
    tool_message = next(
        message for message in scripted_model.prompts[1] if message.get("role") == "tool"
    )
    assert tool_message["content"].startswith("Rejected: ")
    assert "permission" in tool_message["content"]
    assert events[-1]["type"] == "final"


async def test_a_retrieval_outage_is_reported_not_fatal(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    """The embedding service being down is an answer, not a crash.

    The retriever raises -- in production this is LM Studio being
    unreachable -- and the failure flows back as the tool's result, so the
    model can say the documents are unreachable and the stream completes
    with a final event instead of dying mid-run.
    """
    from app.agents.chat import ToolCall

    runtime_env.error = RetrievalUnavailable(
        "Scoped context retrieval failed validation"
    )
    await seed_model_config(db_session_factory)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(id="call-1", name="search_context", arguments={"query": "revenue"})
            ],
        ),
        ChatTurn(content="I cannot reach the documents right now.", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "How did revenue move?"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = sse_events(response.text)
    tool_message = next(
        message for message in scripted_model.prompts[1] if message.get("role") == "tool"
    )
    assert tool_message["content"].startswith("Unavailable: ")
    assert events[-1]["type"] == "final"
    assert "cannot reach" in events[-1]["content"]


class FakeStreamingProvider:
    """A model endpoint that streams: scripted delta pieces per turn."""

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.turns = list(turns)
        self.prompts: list[list[dict[str, Any]]] = []
        self.closed = False

    async def stream_turn(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        del tools
        self.prompts.append([dict(message) for message in messages])
        for piece in self.turns.pop(0):
            yield piece

    async def aclose(self) -> None:
        self.closed = True


async def test_a_streaming_answer_forwards_deltas_then_final(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token deltas stream to the browser while the model thinks.

    The pieces arrive as three delta frames and one assembled turn; the
    browser sees every delta in order and then the same text as the
    authoritative `final`.
    """
    from app.agents.chat import ToolCall

    provider = FakeStreamingProvider(
        [
            [
                {
                    "kind": "turn",
                    "turn": ChatTurn(
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="call-1",
                                name="search_context",
                                arguments={"query": "revenue"},
                            )
                        ],
                    ),
                }
            ],
            [
                {"kind": "delta", "text": "Net "},
                {"kind": "delta", "text": "revenue rose "},
                {"kind": "delta", "text": "by a tenth [1]."},
                {"kind": "turn", "turn": ChatTurn(content="Net revenue rose by a tenth [1].")},
            ],
        ]
    )
    monkeypatch.setattr("app.routes.agent.provider_for", lambda config, session_id=None: provider)
    await seed_model_config(db_session_factory)
    await seed_retrieval_result(runtime_env, entities.finance.id)

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "How did revenue move?"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = sse_events(response.text)
    kinds = [event["type"] for event in events]
    assert kinds == [
        "turn_start",
        "tool_call",
        "tool_result",
        "message_delta",
        "message_delta",
        "message_delta",
        "final",
    ]
    assert "".join(e.get("content", "") for e in events if e["type"] == "message_delta") == (
        "Net revenue rose by a tenth [1]."
    )
    assert events[-1]["content"] == "Net revenue rose by a tenth [1]."


async def test_stream_turn_assembles_fragmented_tool_calls() -> None:
    """Tool calls in streaming mode are spread across frames.

    The arguments JSON arrives in pieces keyed by index; parsing before the
    stream finishes would crash. The assembled turn must carry the merged,
    parsed arguments exactly as the non-streaming path would have produced.
    """
    import httpx as httpx_module

    from app.agents.chat import OpenAICompatibleChatProvider

    sse_body = (
        'data: {"model":"test-model","choices":[{"index":0,"delta":'
        '{"role":"assistant","content":"Let me look. "}}]}\n\n'
        'data: {"model":"test-model","choices":[{"index":0,"delta":'
        '{"tool_calls":[{"index":0,"id":"call-9","type":"function",'
        '"function":{"name":"search_context","arguments":"{\\\"query\\\": \\"\"}}]'
        '}}]}\n\n'
        'data: {"model":"test-model","choices":[{"index":0,"delta":'
        '{"tool_calls":[{"index":0,'
        '"function":{"arguments":"revenue\\\"}"}}]}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx_module.Request) -> httpx_module.Response:
        return httpx_module.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body,
        )

    provider = OpenAICompatibleChatProvider(
        base_url="https://gateway.test/v1",
        api_key="k",
        model="test-model",
        transport=httpx_module.MockTransport(handler),
    )
    deltas: list[str] = []
    turn = None
    async for piece in provider.stream_turn(
        messages=[{"role": "user", "content": "revenue?"}], tools=[{"type": "function"}]
    ):
        if piece["kind"] == "delta":
            deltas.append(piece["text"])
        else:
            turn = piece["turn"]
    await provider.aclose()

    assert "".join(deltas) == "Let me look. "
    assert turn is not None
    assert turn.content == "Let me look. "
    assert [(call.id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("call-9", "search_context", {"query": "revenue"})
    ]


async def test_complete_sends_temperature_only_when_asked() -> None:
    """An eval pins temperature; production must keep sending none at all."""
    import httpx as httpx_module

    from app.agents.chat import OpenAICompatibleChatProvider

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx_module.Request) -> httpx_module.Response:
        bodies.append(json.loads(request.content))
        return httpx_module.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    provider = OpenAICompatibleChatProvider(
        base_url="https://gateway.test/v1",
        api_key="k",
        model="test-model",
        transport=httpx_module.MockTransport(handler),
    )
    try:
        await provider.complete(
            messages=[{"role": "user", "content": "hi"}], tools=[], temperature=0.0
        )
        await provider.complete(messages=[{"role": "user", "content": "hi"}], tools=[])
    finally:
        await provider.aclose()

    assert bodies[0]["temperature"] == 0.0
    assert "temperature" not in bodies[1]


class FakeAdminProvider:
    """Stands in for the real chat client during admin saves: no network."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def test_connection(self) -> str:
        return "deepseek-v4.1-flash"

    async def aclose(self) -> None:
        return None


async def _admin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    return await login(client, "admin", "admin-correct-password")


async def test_a_preset_provider_fills_the_url_and_default_model(
    client: httpx.AsyncClient,
    entities: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin names a provider and a key; the server owns the rest."""
    from app.db.models import AgentModelSetting as Row

    monkeypatch.setattr(
        "app.routes.admin.OpenAICompatibleChatProvider", FakeAdminProvider
    )
    headers = await _admin_headers(client)
    response = await client.put(
        "/v1/admin/agent-model",
        json={"provider": "opencode-go", "api_key": "sk-test-opencode-1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "opencode-go"
    assert body["base_url"] == "https://opencode.ai/zen/go/v1"
    assert body["model"] == "deepseek-v4.1-flash"
    assert body["last_test_ok"] is True
    assert body["last_test_model"] == "deepseek-v4.1-flash"
    assert body["key_hint"].startswith("…")
    provider_keys = {entry["key"] for entry in body["available_providers"]}
    assert provider_keys == {"custom", "opencode-go", "deepseek"}

    async with db_session_factory() as session:
        row = await session.get(Row, 1)
    assert row is not None
    assert row.provider == "opencode-go"
    assert row.base_url == "https://opencode.ai/zen/go/v1"


async def test_a_custom_provider_without_a_base_url_is_refused(
    client: httpx.AsyncClient,
    entities: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.routes.admin.OpenAICompatibleChatProvider", FakeAdminProvider
    )
    headers = await _admin_headers(client)
    response = await client.put(
        "/v1/admin/agent-model",
        json={"provider": "custom", "api_key": "sk-something"},
        headers=headers,
    )
    assert response.status_code == 422, response.text


async def test_the_opencode_preset_carries_the_session_header() -> None:
    """OpenCode Go rejects calls without a stable per-conversation id.

    The preset knows that; the provider builder attaches it, and a preset
    without such a rule sends nothing.
    """
    from app.agents.model_settings import AgentModelConfig, provider_for

    session = "conversation-12345678"
    opencode = provider_for(
        AgentModelConfig(
            provider="opencode-go",
            base_url="https://opencode.ai/zen/go/v1",
            api_key="k",
            model="deepseek-v4.1-flash",
        ),
        session,
    )
    assert opencode._client.headers["x-opencode-session"] == session
    await opencode.aclose()

    deepseek = provider_for(
        AgentModelConfig(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            api_key="k",
            model="deepseek-chat",
        ),
        session,
    )
    assert "x-opencode-session" not in deepseek._client.headers
    await deepseek.aclose()


async def test_the_client_session_id_reaches_the_provider(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stable conversation id rides from the request to the model client."""
    from uuid import UUID

    await seed_model_config(db_session_factory)
    captured: dict[str, Any] = {}
    provider = FakeChatProvider(
        [ChatTurn(content="Done.", tool_calls=[]), ChatTurn(content="Again.", tool_calls=[])]
    )

    def capturing_provider_for(config: Any, session_id: str | None = None) -> Any:
        captured["session_id"] = session_id
        return provider

    monkeypatch.setattr("app.routes.agent.provider_for", capturing_provider_for)
    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "hello", "session_id": "my-conversation-0001"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert captured["session_id"] == "my-conversation-0001"

    response = await client.post(
        "/v1/agent/chat",
        json={"message": "hello again"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    generated = captured["session_id"]
    assert isinstance(generated, str)
    UUID(generated)


class _LimitRecordingKit:
    """A toolkit stub that records the arguments the harness dispatches."""

    default_search_limit = 3

    def __init__(self) -> None:
        self.limits: list[int] = []
        self.min_scores: list[float | None] = []
        self.document_ids: list[Any] = []
        self.text_queries: list[tuple[str, int, Any]] = []
        self.outlines: list[tuple[Any, Any, int]] = []
        self.described: list[tuple[Any, Any]] = []
        self.workspaces_called = False

    async def search_context(
        self,
        *,
        query: str,
        limit: int,
        workspace_id: Any = None,
        document_id: Any = None,
        min_score: float | None = None,
        start_index: int = 1,
    ) -> str:
        del query, workspace_id, start_index
        self.limits.append(limit)
        self.min_scores.append(min_score)
        self.document_ids.append(document_id)
        return "ok"

    async def search_text(
        self,
        *,
        query: str,
        limit: int,
        workspace_id: Any = None,
        document_id: Any = None,
        start_index: int = 1,
    ) -> str:
        del workspace_id, start_index
        self.text_queries.append((query, limit, document_id))
        return "ok"

    async def list_workspaces(self) -> str:
        self.workspaces_called = True
        return "ok"

    async def get_document_outline(
        self, *, document_id: Any, workspace_id: Any, limit: int = 200
    ) -> str:
        self.outlines.append((document_id, workspace_id, limit))
        return "ok"

    async def describe_document(self, *, document_id: Any, workspace_id: Any) -> str:
        self.described.append((document_id, workspace_id))
        return "ok"


async def test_search_limit_defaults_to_the_configured_value_and_is_clamped() -> None:
    """The env default applies, and a model cannot ask for an unbounded page."""
    kit: Any = _LimitRecordingKit()
    await _execute_tool(kit, "search_context", {"query": "revenue"})
    await _execute_tool(kit, "search_context", {"query": "revenue", "limit": 500})
    await _execute_tool(kit, "search_context", {"query": "revenue", "limit": 0})
    assert kit.limits == [3, MAX_SEARCH_LIMIT, 1]


async def test_search_min_score_is_optional_and_clamped() -> None:
    """Omitted means the server default; out-of-range values are clamped."""
    kit: Any = _LimitRecordingKit()
    await _execute_tool(kit, "search_context", {"query": "revenue"})
    await _execute_tool(kit, "search_context", {"query": "revenue", "min_score": 0.8})
    await _execute_tool(kit, "search_context", {"query": "revenue", "min_score": 5})
    await _execute_tool(kit, "search_context", {"query": "revenue", "min_score": -5})
    assert kit.min_scores == [None, 0.8, 1.0, -1.0]


async def test_search_document_id_is_passed_through() -> None:
    """A document_id narrows the search; omitting it leaves the whole scope."""
    kit: Any = _LimitRecordingKit()
    document_id = uuid4()
    await _execute_tool(
        kit,
        "search_context",
        {"query": "revenue", "document_id": str(document_id)},
    )
    await _execute_tool(kit, "search_context", {"query": "revenue"})
    assert kit.document_ids == [document_id, None]


async def test_the_new_tools_dispatch_with_clamped_arguments() -> None:
    """The schema is a promise the loop still has to keep at dispatch time."""
    kit: Any = _LimitRecordingKit()
    document_id = uuid4()
    workspace_id = uuid4()

    await _execute_tool(kit, "search_text", {"query": "Pasal 7", "limit": 500})
    await _execute_tool(kit, "search_text", {"query": "Pasal 7"})
    await _execute_tool(kit, "list_workspaces", {})
    await _execute_tool(
        kit,
        "get_document_outline",
        {
            "document_id": str(document_id),
            "workspace_id": str(workspace_id),
            "limit": 900,
        },
    )
    await _execute_tool(
        kit,
        "describe_document",
        {"document_id": str(document_id), "workspace_id": str(workspace_id)},
    )

    assert [entry[1] for entry in kit.text_queries] == [MAX_SEARCH_LIMIT, 3]
    assert kit.workspaces_called is True
    assert kit.outlines == [(document_id, workspace_id, 500)]
    assert kit.described == [(document_id, workspace_id)]


async def test_an_outline_without_a_workspace_is_rejected_before_the_call() -> None:
    kit: Any = _LimitRecordingKit()

    with pytest.raises(ToolRejection):
        await _execute_tool(
            kit,
            "get_document_outline",
            {"document_id": str(uuid4())},
        )

    assert kit.outlines == []


async def test_an_answer_without_a_citation_is_replaced(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    """A search served a source; an answer that cites none is not shown."""
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    await seed_retrieval_result(runtime_env, entities.finance.id)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(id="call-1", name="search_context", arguments={"query": "revenue"})
            ],
        ),
        ChatTurn(content="Net revenue rose by a tenth.", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "How did revenue move?"},
        headers=headers,
    )
    events = sse_events(response.text)
    final = events[-1]
    assert final["type"] == "final"
    assert final["replaced"] is True
    assert final["reason"] == "no_citation"
    assert "Net revenue" not in final["content"]
    assert "could not verify" in final["content"]


async def test_an_answer_without_any_tool_is_replaced(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    """A content question answered from general knowledge is not shown."""
    await seed_model_config(db_session_factory)
    scripted_model.turns = [
        ChatTurn(content="Paris is the capital of France.", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "What is the capital of France?"},
        headers=headers,
    )
    events = sse_events(response.text)
    final = events[-1]
    assert final["type"] == "final"
    assert final["replaced"] is True
    assert final["reason"] == "no_tool"
    assert "Paris" not in final["content"]


async def test_a_citation_to_an_unserved_source_is_replaced(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    await seed_retrieval_result(runtime_env, entities.finance.id)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(id="call-1", name="search_context", arguments={"query": "revenue"})
            ],
        ),
        ChatTurn(content="Net revenue rose by a tenth [9].", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "How did revenue move?"},
        headers=headers,
    )
    final = sse_events(response.text)[-1]
    assert final["replaced"] is True
    assert final["reason"] == "invalid_citation"


async def test_a_not_found_answer_after_a_search_is_allowed(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    """Sources were served, but the answer honestly says they do not cover it."""
    from app.agents.chat import ToolCall

    await seed_model_config(db_session_factory)
    await seed_retrieval_result(runtime_env, entities.finance.id)
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(id="call-1", name="search_context", arguments={"query": "revenue"})
            ],
        ),
        ChatTurn(content="The documents do not mention that.", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "What about revenue?"},
        headers=headers,
    )
    final = sse_events(response.text)[-1]
    assert final["type"] == "final"
    assert "replaced" not in final
    assert final["content"] == "The documents do not mention that."


async def test_a_meta_message_is_answered_without_a_tool(
    client: httpx.AsyncClient,
    entities: Any,
    runtime_env: FakeRetriever,
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_model: FakeChatProvider,
) -> None:
    await seed_model_config(db_session_factory)
    scripted_model.turns = [
        ChatTurn(content="Halo! Saya bisa mencari di dokumen workspace.", tool_calls=[]),
    ]

    headers = await login(client, "alice", OWNER_PASSWORD)
    response = await client.post(
        "/v1/agent/chat",
        json={"message": "Halo"},
        headers=headers,
    )
    final = sse_events(response.text)[-1]
    assert final["type"] == "final"
    assert "replaced" not in final
    assert final["content"].startswith("Halo!")
