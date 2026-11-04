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
from app.db.models import AgentModelSetting
from app.mcp.runtime import McpRuntime
from app.retrieval.context import ContextRetriever
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

    async def retrieve(
        self,
        query: str,
        workspace_ids: list[Any],
        limit: int,
        *,
        budget: int | None = None,
    ) -> list[ContextSource]:
        del query, limit, budget
        self.scopes.append(list(workspace_ids))
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
        ChatTurn(content="Net revenue rose by a tenth, per report.pdf.", tool_calls=[]),
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

    assert len(runtime_env.scopes) == 1
    assert set(runtime_env.scopes[0]) == {entities.finance.id, entities.legal.id}
    tool_message = next(
        message
        for message in scripted_model.prompts[1]
        if message.get("role") == "tool"
    )
    assert "untrusted_document_text" in tool_message["content"]
    assert scripted_model.closed


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
    scripted_model.turns = [
        ChatTurn(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="download_document",
                    arguments={
                        "document_id": str(uuid4()),
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
