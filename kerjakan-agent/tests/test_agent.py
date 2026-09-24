import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.config import Settings
from agent.context_scope import SCOPE_ALGORITHM, SCOPE_AUDIENCE, SCOPE_TYPE
from agent.dependencies import get_answer_harness
from agent.harness import AnswerHarness
from agent.main import create_app
from agent.retrieval import OutOfScopeSearchResult
from agent.schemas import AgentHistoryMessage, AnswerResponse, ContextSource


class StubRetriever:
    def __init__(self, results: list[list[ContextSource] | Exception]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, list[UUID], int]] = []

    async def retrieve(
        self, query: str, workspace_ids: list[UUID], limit: int
    ) -> list[ContextSource]:
        self.calls.append((query, workspace_ids, limit))
        result = self.results.pop(0) if self.results else []
        if isinstance(result, Exception):
            raise result
        return result


class StubHarness:
    def __init__(self, result: AnswerResponse) -> None:
        self.result = result
        self.calls: list[tuple[str, list[UUID], list[dict[str, object]]]] = []

    async def answer(
        self,
        question: str,
        workspace_ids: list[UUID],
        history: list[AgentHistoryMessage],
    ) -> AnswerResponse:
        self.calls.append(
            (
                question,
                workspace_ids,
                [message.model_dump(mode="json") for message in history],
            )
        )
        return self.result


def settings() -> tuple[Settings, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    configuration = Settings(
        environment="test",
        api_token=SecretStr("agent-api-token-for-tests-0123456789"),
        scope_public_key_b64=SecretStr(base64.b64encode(public_pem).decode("ascii")),
        qdrant_api_key=SecretStr("qdrant-readonly-key-for-tests-0123456789"),
        embedding_api_key=SecretStr("embedding-api-key-for-tests-0123456789"),
        chat_api_key=SecretStr("chat-api-key-for-tests-0123456789"),
        chat_model="openai/test-chat-model",
        embedding_dimensions=2,
        max_context_chunks=3,
    )
    return configuration, private_pem


def scope_token(
    configuration: Settings,
    private_key: bytes,
    workspace_ids: list[UUID],
    *,
    expired: bool = False,
) -> str:
    now = datetime.now(UTC)
    expires = now - timedelta(seconds=1) if expired else now + timedelta(minutes=5)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "auth_version": 1,
            "workspace_ids": [str(workspace_id) for workspace_id in workspace_ids],
            "type": SCOPE_TYPE,
            "iss": configuration.scope_issuer,
            "aud": SCOPE_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": expires,
            "jti": str(uuid4()),
        },
        private_key,
        algorithm=SCOPE_ALGORITHM,
    )


def context_item(workspace_id: UUID, *, chunk_id: UUID | None = None) -> ContextSource:
    return ContextSource(
        workspace_id=workspace_id,
        document_id=uuid4(),
        processing_run_id=uuid4(),
        chunk_id=chunk_id or uuid4(),
        ordinal=3,
        score=0.9,
        content_hash="c" * 64,
        filename="report.pdf",
        section_path=["Results"],
        page_numbers=[2],
        text="Revenue increased by 20 percent.",
    )


def tool_loop_model(
    behavior: Callable[[list[ModelMessage], AgentInfo], ModelResponse],
) -> FunctionModel:
    return FunctionModel(behavior, model_name="test-tool-loop-model")


async def test_harness_builds_configured_openrouter_tool_agent() -> None:
    configuration, _ = settings()
    harness = AnswerHarness(configuration, StubRetriever([]))
    await harness.aclose()


async def test_harness_registers_scoped_search_tool_and_runs_bounded_loop() -> None:
    configuration, _ = settings()
    workspace_id = uuid4()
    initial = context_item(workspace_id)
    followup = context_item(workspace_id)
    retriever = StubRetriever([[initial], [followup]])
    model_calls = 0

    def model_response(_: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        tool_schema = json.dumps(info.function_tools[0].parameters_json_schema)
        assert "workspace_ids" not in tool_schema
        if model_calls == 1:
            assert info.model_settings is not None
            assert info.model_settings.get("tool_choice") == "required"
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_context",
                        {"query": "LLM-selected initial query"},
                        tool_call_id="initial-search",
                    )
                ]
            )
        if model_calls == 2:
            assert info.model_settings is not None
            assert info.model_settings.get("tool_choice") == "auto"
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_context",
                        {"query": "breakdown by quarter"},
                        tool_call_id="followup-search",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Revenue increased 20%. [1] [2]")])

    harness = AnswerHarness(
        configuration,
        retriever,
        model=tool_loop_model(model_response),
    )
    try:
        result = await harness.answer("What happened to revenue?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.answer == "Revenue increased 20%. [1] [2]"
    assert {source.chunk_id for source in result.sources} == {
        initial.chunk_id,
        followup.chunk_id,
    }
    assert retriever.calls == [
        ("LLM-selected initial query", [workspace_id], 3),
        ("breakdown by quarter", [workspace_id], 3),
    ]
    assert model_calls == 3


async def test_harness_stops_after_tool_call_limit() -> None:
    configuration, _ = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[], []])
    model_calls = 0

    def model_response(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        return ModelResponse(
            parts=[ToolCallPart("search_context", {"query": f"follow up {model_calls}"})]
        )

    harness = AnswerHarness(
        configuration,
        retriever,
        model=tool_loop_model(model_response),
    )
    try:
        result = await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.sources == []
    assert "could not find relevant information" in result.answer
    assert len(retriever.calls) == 2
    assert model_calls <= 3


async def test_out_of_scope_followup_aborts_agent_run() -> None:
    configuration, _ = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([OutOfScopeSearchResult("outside scope")])

    def model_response(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("search_context", {"query": "follow up"})]
        )

    harness = AnswerHarness(
        configuration,
        retriever,
        model=tool_loop_model(model_response),
    )
    try:
        with pytest.raises(OutOfScopeSearchResult):
            await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()


async def test_answer_without_valid_source_citation_is_not_returned() -> None:
    configuration, _ = settings()
    workspace_id = uuid4()
    model_calls = 0

    def model_response(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "revenue"})]
            )
        return ModelResponse(parts=[TextPart("Revenue rose by 20 percent.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([[context_item(workspace_id)]]),
        model=tool_loop_model(model_response),
    )
    try:
        result = await harness.answer("Revenue?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.sources == []
    assert "could not produce an answer grounded" in result.answer


async def test_no_context_allows_bounded_tool_retries_then_returns_no_context() -> None:
    configuration, _ = settings()
    retriever = StubRetriever([[], []])
    model_calls = 0

    def model_response(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls <= 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_context",
                        {"query": f"alternate wording {model_calls}"},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("No relevant evidence found.")])

    harness = AnswerHarness(
        configuration,
        retriever,
        model=tool_loop_model(model_response),
    )
    try:
        result = await harness.answer("Question", [uuid4()], [])
    finally:
        await harness.aclose()

    assert result.sources == []
    assert "could not find relevant information" in result.answer
    assert model_calls <= 3
    assert len(retriever.calls) == 2


async def test_harness_redacts_assistant_history_even_without_provenance() -> None:
    configuration, _ = settings()
    workspace_id = uuid4()
    current_source = context_item(workspace_id)
    observed_history = False
    model_calls = 0

    def model_response(
        messages: list[ModelMessage], _: AgentInfo
    ) -> ModelResponse:
        nonlocal observed_history, model_calls
        model_calls += 1
        observed_history = any(
            isinstance(message, ModelResponse)
            and any(
                isinstance(part, TextPart)
                and "prior assistant response is saved in the private transcript" in part.content
                and "Previous answer [1]" not in part.content
                for part in message.parts
            )
            for message in messages
        )
        if model_calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "current evidence"})]
            )
        return ModelResponse(parts=[TextPart("The current report says revenue increased. [1]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([[current_source]]),
        model=tool_loop_model(model_response),
    )
    try:
        result = await harness.answer(
            "Explain that",
            [workspace_id],
            [
                AgentHistoryMessage(role="user", content="What did the report say?"),
                AgentHistoryMessage(
                    role="assistant",
                    content="Previous answer [1]",
                ),
            ],
        )
    finally:
        await harness.aclose()

    assert observed_history
    assert result.answer == "The current report says revenue increased. [1]"
    assert result.sources[0].chunk_id == current_source.chunk_id
    assert result.sources[0].text == current_source.text


async def test_citation_must_match_a_fresh_current_turn_source() -> None:
    configuration, _ = settings()
    workspace_id = uuid4()
    current_source = context_item(workspace_id)
    model_calls = 0

    def model_response(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "fresh evidence"})]
            )
        return ModelResponse(parts=[TextPart("Revenue increased. [2]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([[current_source]]),
        model=tool_loop_model(model_response),
    )
    try:
        result = await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.answer == "I could not produce an answer grounded in the authorized sources."
    assert result.sources == []


async def test_agent_forwards_history_to_harness() -> None:
    configuration, private_key = settings()
    workspace_id = uuid4()
    source = context_item(workspace_id)
    harness = StubHarness(AnswerResponse(answer="Answer [1]", sources=[source]))
    application = create_app(configuration)
    application.dependency_overrides[get_answer_harness] = lambda: harness
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            response = await client.post(
                "/v1/answer",
                headers={"Authorization": f"Bearer {configuration.api_token.get_secret_value()}"},
                json={
                    "question": "Continue from earlier",
                    "scope_token": scope_token(configuration, private_key, [workspace_id]),
                    "history": [
                        {"role": "user", "content": "What did the report say?"},
                        {
                            "role": "assistant",
                            "content": "Revenue increased. [1]",
                            "source_workspace_ids": [str(workspace_id)],
                        },
                    ],
                },
            )
    finally:
        application.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert harness.calls[0][0] == "Continue from earlier"
    assert len(harness.calls[0][2]) == 2
    assert response.json()["answer"] == "Answer [1]"


async def test_history_outside_scope_is_rejected_before_harness() -> None:
    configuration, private_key = settings()
    allowed_workspace = uuid4()
    denied_workspace = uuid4()
    harness = StubHarness(AnswerResponse(answer="must not run", sources=[]))
    application = create_app(configuration)
    application.dependency_overrides[get_answer_harness] = lambda: harness
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            response = await client.post(
                "/v1/answer",
                headers={"Authorization": f"Bearer {configuration.api_token.get_secret_value()}"},
                json={
                    "question": "Continue",
                    "scope_token": scope_token(configuration, private_key, [allowed_workspace]),
                    "history": [
                        {
                            "role": "assistant",
                            "content": "private answer",
                            "source_workspace_ids": [str(denied_workspace)],
                        }
                    ],
                },
            )
    finally:
        application.dependency_overrides.clear()

    assert response.status_code == 403
    assert harness.calls == []


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-agent-token"])
async def test_agent_endpoint_requires_kerjakan_m2m_token(
    authorization: str | None,
) -> None:
    configuration, private_key = settings()
    workspace_id = uuid4()
    harness = StubHarness(AnswerResponse(answer="Answer", sources=[]))
    application = create_app(configuration)
    application.dependency_overrides[get_answer_harness] = lambda: harness
    headers = {} if authorization is None else {"Authorization": authorization}
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            response = await client.post(
                "/v1/answer",
                headers=headers,
                json={
                    "question": "Question",
                    "scope_token": scope_token(configuration, private_key, [workspace_id]),
                },
            )
    finally:
        application.dependency_overrides.clear()
    assert response.status_code == 401
    assert harness.calls == []


async def test_malformed_and_expired_scope_rejected() -> None:
    configuration, private_key = settings()
    workspace_id = uuid4()
    harness = StubHarness(AnswerResponse(answer="Answer", sources=[]))
    application = create_app(configuration)
    application.dependency_overrides[get_answer_harness] = lambda: harness
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            for token in (
                "invalid-scope-token-long-enough",
                scope_token(configuration, private_key, [workspace_id], expired=True),
            ):
                response = await client.post(
                    "/v1/answer",
                    headers={
                        "Authorization": f"Bearer {configuration.api_token.get_secret_value()}"
                    },
                    json={"question": "Question", "scope_token": token},
                )
                assert response.status_code == 401
    finally:
        application.dependency_overrides.clear()
    assert harness.calls == []
