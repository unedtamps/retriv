import json
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agent.harness import AnswerHarness
from app.agent.retrieval import OutOfScopeSearchResult
from app.agent.schemas import AgentHistoryMessage, AnswerResponse, ContextSource
from app.core.config import Settings


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


def settings() -> Settings:
    """The harness needs no M2M token and no scope keypair.

    It is called directly now, so there is no inbound request to
    authenticate and no signature to verify. The scope arrives as a
    plain list of workspace IDs, and it is the caller's job to have
    derived that list from the user's memberships.
    """
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        qdrant_read_only_api_key=SecretStr("qdrant-readonly-key-for-tests-0123456789"),
        embedding_api_key=SecretStr("embedding-api-key-for-tests-0123456789"),
        chat_api_key=SecretStr("chat-api-key-for-tests-0123456789"),
        chat_model="openai/test-chat-model",
        embedding_dimensions=2,
        max_context_chunks=3,
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


async def test_harness_builds_an_answer_agent_without_any_network_call() -> None:
    """The harness constructs a model client eagerly, but it never dials
    OpenRouter until a run starts. There is no M2M token to configure and
    no inbound request to authenticate."""
    configuration = settings()
    harness = AnswerHarness(configuration, StubRetriever([]))
    await harness.aclose()


async def test_harness_registers_scoped_search_tool_and_runs_bounded_loop() -> None:
    configuration = settings()
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
    configuration = settings()
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
    configuration = settings()
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
    configuration = settings()
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
    configuration = settings()
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
    configuration = settings()
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
    configuration = settings()
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


async def test_history_outside_the_scope_is_rejected_before_any_search() -> None:
    """A prior turn that cited a workspace the caller can no longer reach
    must abort the run, not become model context.

    This was the agent service's job to do when the scope arrived in a
    signed token. It is the harness's job now, and it is still the check
    that stops a stale conversation from leaking another workspace.
    """
    configuration = settings()
    allowed_workspace = uuid4()
    denied_workspace = uuid4()
    retriever = StubRetriever([[context_item(allowed_workspace)]])
    harness = AnswerHarness(configuration, retriever)
    try:
        with pytest.raises(OutOfScopeSearchResult):
            await harness.answer(
                "Continue",
                [allowed_workspace],
                [
                    AgentHistoryMessage(
                        role="assistant",
                        content="a private answer",
                        source_workspace_ids=[denied_workspace],
                    )
                ],
            )
    finally:
        await harness.aclose()

    assert retriever.calls == []


async def test_history_inside_the_scope_is_accepted_and_redacted() -> None:
    """In-scope history is allowed through, but the assistant text is
    replaced: the model may use it for continuity, never as evidence."""
    configuration = settings()
    workspace_id = uuid4()
    source = context_item(workspace_id)
    retriever = StubRetriever([[source]])
    seen: list[ModelMessage] = []
    step = 0

    def model_response(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        nonlocal step
        seen.extend(messages)
        step += 1
        if step == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "evidence"})]
            )
        return ModelResponse(parts=[TextPart("Revenue increased. [1]")])

    harness = AnswerHarness(
        configuration,
        retriever,
        model=tool_loop_model(model_response),
    )
    try:
        result = await harness.answer(
            "What did it say?",
            [workspace_id],
            [
                AgentHistoryMessage(role="user", content="earlier question"),
                AgentHistoryMessage(
                    role="assistant",
                    content="Revenue increased. [1]",
                    source_workspace_ids=[workspace_id],
                ),
            ],
        )
    finally:
        await harness.aclose()

    rendered = "\n".join(
        str(getattr(part, "content", ""))
        for message in seen
        for part in message.parts
        if hasattr(part, "content")
    )
    assert "Revenue increased. [1]" not in rendered
    assert "earlier question" in rendered
    assert result.sources == [source]


async def test_the_scope_cannot_be_widened_by_a_tool_call() -> None:
    """The search tool takes a query and nothing else.

    The workspace scope is frozen into the dependencies when the run
    starts, so there is no argument a model could supply to reach a
    workspace the caller was not given.
    """
    configuration = settings()
    workspace_id = uuid4()
    source = context_item(workspace_id)
    retriever = StubRetriever([[source]])
    tool_schemas: list[object] = []

    step = 0

    def model_response(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal step
        tool_schemas.extend(
            tool.parameters_json_schema
            for tool in info.function_tools
            if tool.name == "search_context"
        )
        step += 1
        if step == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "evidence"})]
            )
        return ModelResponse(parts=[TextPart("Revenue increased. [1]")])

    harness = AnswerHarness(
        configuration,
        retriever,
        model=tool_loop_model(model_response),
    )
    try:
        await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert tool_schemas, "the search tool was not offered to the model"
    for schema in tool_schemas:
        properties = json.loads(json.dumps(schema)).get("properties", {})
        assert set(properties) <= {"query"}, (
            "the search tool must accept nothing but a query, or a model "
            f"could argue for a wider scope; got {sorted(properties)}"
        )

    # every search the run performed stayed inside the given scope
    for _, workspace_ids, _ in retriever.calls:
        assert workspace_ids == [workspace_id]
