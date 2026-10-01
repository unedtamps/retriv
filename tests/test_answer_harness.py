import json
import logging
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agent.harness import (
    MAX_MODEL_REQUESTS,
    MAX_OUTPUT_TOKENS,
    MAX_TOOL_CALLS,
    NO_EVIDENCE_ANSWER,
    UNCITED_ANSWER,
    AnswerGenerationUnavailable,
    AnswerHarness,
    format_synthesis_prompt,
    synthesis_prompt,
    system_prompt,
)
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

    # Retrieval never produced a chunk, so the run degrades to the
    # no-evidence answer. The bound is asserted against the limit rather
    # than a literal: the limit carries headroom because one model
    # response can emit several tool calls at once.
    assert result.sources == []
    assert "could not find relevant information" in result.answer
    assert len(retriever.calls) == MAX_TOOL_CALLS
    assert model_calls <= MAX_MODEL_REQUESTS


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



# --- exhausted tool budget with evidence in hand -------------------------
#
# This model answers with two search_context calls in a single response.
# That spent a budget of 2 in one turn, the run then hit
# UsageLimitExceeded, and because retrieval had already succeeded the
# no-evidence fallback was skipped -- so a 503. It is stochastic: the
# same question succeeded two runs in three.

NO_EVIDENCE_MARKER = "No relevant workspace evidence has been retrieved yet."


def budget_exhausting_model(answer_text: str) -> FunctionModel:
    """A model that never answers until the fallback asks it to.

    The harness's first prompt embeds that no-evidence marker because it
    starts with zero sources; the fallback prompt instead embeds the real
    formatted context. Keying on that is what makes these tests reach the
    recovery path: simply counting calls lets the run finish inside the
    request limit, which is the normal path and proves nothing.
    """

    def behavior(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        first = messages[0]
        text = "".join(
            part.content
            for part in getattr(first, "parts", [])
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        if NO_EVIDENCE_MARKER in text:
            # Never answers: the tool budget is guaranteed to run out.
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "keep searching"})]
            )
        return ModelResponse(parts=[TextPart(answer_text)])

    return tool_loop_model(behavior)


async def test_exhausted_budget_with_evidence_answers_from_retrieval() -> None:
    """The regression that produced the 503.

    Retrieval succeeded, so the evidence is real and in scope. Losing it
    because the model wanted one more search is worse than answering
    partially from what is already in hand.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[context_item(workspace_id)]])

    harness = AnswerHarness(
        configuration,
        retriever,
        model=budget_exhausting_model("Revenue increased by 20 percent. [1]"),
    )
    try:
        result = await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.sources != [], "recovered evidence should be returned"
    assert "20 percent" in result.answer
    assert "[1]" in result.answer


async def test_exhausted_budget_still_enforces_the_workspace_scope() -> None:
    """The recovery path must not become a way around the scope check.

    A scope violation on the follow-up search has to abort even though
    earlier searches produced citable sources, and even though the
    failure surfaces as a usage limit.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever(
        [[context_item(workspace_id)], OutOfScopeSearchResult("point outside scope")]
    )

    harness = AnswerHarness(
        configuration, retriever, model=budget_exhausting_model("Should not be used. [1]")
    )
    try:
        with pytest.raises(OutOfScopeSearchResult):
            await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()


async def test_recovered_answer_without_a_citation_is_refused() -> None:
    """A recovered answer is held to the same citation rule as a normal one.

    Otherwise the fallback becomes the easy path for returning an
    unsourced claim: exhaust the budget and get away with more than the
    normal path allows.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[context_item(workspace_id)]])

    harness = AnswerHarness(
        configuration, retriever, model=budget_exhausting_model("Revenue rose sharply.")
    )
    try:
        result = await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.sources == []
    assert result.answer == UNCITED_ANSWER


async def test_recovered_answer_cannot_cite_a_chunk_that_was_not_retrieved() -> None:
    """Citation 99 does not exist in this run, so the answer is refused."""
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[context_item(workspace_id)]])

    harness = AnswerHarness(
        configuration, retriever, model=budget_exhausting_model("Revenue rose sharply. [99]")
    )
    try:
        result = await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.sources == []
    assert result.answer == UNCITED_ANSWER


async def test_exhausted_budget_still_reports_no_evidence_when_nothing_retrieved() -> None:
    """No sources means there is nothing to synthesize from.

    NO_EVIDENCE_ANSWER is the proof the recovery branch was never
    reached: a synthesis attempt answers with either a citation or
    UNCITED_ANSWER, never this.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[]])

    harness = AnswerHarness(
        configuration, retriever, model=budget_exhausting_model("Should not be used. [1]")
    )
    try:
        result = await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.sources == []
    assert result.answer == NO_EVIDENCE_ANSWER
    assert len(retriever.calls) == MAX_TOOL_CALLS


async def test_unrecoverable_failure_is_logged_with_its_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The routes answer every remaining failure with one generic 503.

    Without a log record the cause is unrecoverable from the outside: the
    exception chain is intact here and discarded one frame later. This
    is what made a provider-side usage-limit regression look like an
    unexplained outage.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[context_item(workspace_id)]])

    def behavior(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider returned a malformed completion")

    harness = AnswerHarness(configuration, retriever, model=tool_loop_model(behavior))
    try:
        with (
            caplog.at_level(logging.ERROR, logger="app.agent.harness"),
            pytest.raises(AnswerGenerationUnavailable),
        ):
            await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "Answer generation failed" in caplog.text
    # The underlying reason has to be in the record, not just the wrapper.
    assert "malformed completion" in caplog.text


async def test_fallback_offers_no_tools_while_the_normal_run_offers_one() -> None:
    """Assert what the agent *offers*, not what the request asks for.

    The previous fix set tool_choice="none" on the fallback and these
    tests still passed, because a FunctionModel never enforces it. The
    live model ignores that directive, kept emitting search_context, and
    a zero tool-call limit turned each call into a failure -- so the
    fallback never ran and the 503 survived a green suite.

    Counting offered tools is the check that cannot be faked by a stub.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[context_item(workspace_id)]])
    offered: list[int] = []

    def behavior(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append(len(info.function_tools))
        first = messages[0]
        text = "".join(
            part.content
            for part in getattr(first, "parts", [])
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        if NO_EVIDENCE_MARKER in text:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "keep searching"})]
            )
        return ModelResponse(parts=[TextPart("Revenue increased by 20 percent. [1]")])

    harness = AnswerHarness(
        configuration, retriever, model=tool_loop_model(behavior)
    )
    try:
        result = await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.sources != []
    assert offered[0] == 1, "the normal run must offer search_context"
    assert offered[-1] == 0, "the fallback must offer no tool to call"
    # Every fallback request, not just the last one.
    assert set(offered) == {0, 1}


def test_synthesis_prompts_never_name_a_tool() -> None:
    """The fallback agent is offered no tools, so naming one invites the
    model to emit a call it cannot satisfy, which pydantic-ai reports as
    unexpected model behaviour rather than a tool result."""
    source = context_item(uuid4())
    indices = {source.chunk_id: 1}
    assert "search_context" not in synthesis_prompt()
    assert "search_context" not in format_synthesis_prompt("q", [source], indices)
    assert "search_context" not in format_synthesis_prompt("q", [], {})
    # The normal path is the opposite case and must keep naming its tool.
    assert "search_context" in system_prompt()


async def test_both_agents_share_one_output_token_budget() -> None:
    """The fallback and the main run must not drift apart on max_tokens.

    The fallback carries a prompt holding every retrieved source, so it
    needs at least as much room as the main run to produce an answer at
    all -- it once inherited a 1600-token allowance, spent it on
    reasoning, and returned no text. Raising one and not the other is
    how that recurs, so the shared constant is asserted rather than
    trusted.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[context_item(workspace_id)]])
    seen: list[int | None] = []

    def behavior(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        settings_seen = info.model_settings or {}
        seen.append(settings_seen.get("max_tokens"))
        first = messages[0]
        text = "".join(
            part.content
            for part in getattr(first, "parts", [])
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        if NO_EVIDENCE_MARKER in text:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "keep searching"})]
            )
        return ModelResponse(parts=[TextPart("Revenue increased by 20 percent. [1]")])

    harness = AnswerHarness(
        configuration, retriever, model=tool_loop_model(behavior)
    )
    try:
        await harness.answer("Question", [workspace_id], [])
    finally:
        await harness.aclose()

    assert seen, "the model was never called"
    assert set(seen) == {MAX_OUTPUT_TOKENS}, f"agents disagree: {seen}"
    # A budget this small is a run total, not a per-answer cap.
    assert MAX_OUTPUT_TOKENS >= 4000
