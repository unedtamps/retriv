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
    MAX_CURRENT_SOURCES,
    MAX_MODEL_REQUESTS,
    MAX_OUTPUT_TOKENS,
    MAX_TOOL_CALLS,
    NO_EVIDENCE_ANSWER,
    UNCITED_ANSWER,
    AnswerGenerationUnavailable,
    AnswerHarness,
    EvidenceLedger,
    format_synthesis_prompt,
    synthesis_prompt,
    system_prompt,
)
from app.agent.retrieval import OutOfScopeSearchResult
from app.agent.schemas import AgentHistoryMessage, AnswerResponse, ContextSource
from app.core.config import Settings
from app.retrieval.sql import (
    Catalogue,
    CatalogueEntry,
    RowQueryError,
    RowQueryResult,
    RowResult,
)


class StubRetriever:
    def __init__(self, results: list[list[ContextSource] | Exception]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, list[UUID], int]] = []

    async def retrieve(
        self,
        query: str,
        workspace_ids: list[UUID],
        limit: int,
        *,
        budget: int | None = None,
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


# The values shipped in .env.example. The settings() fixture deliberately
# uses a small 3 so tests stay cheap, which would not satisfy the
# invariant below, so the production numbers are stated here instead.
SHIPPED_MAX_CONTEXT_CHUNKS = 4
SHIPPED_HEADING_EXPAND_CHUNKS = 4


def test_source_budget_is_actually_reachable() -> None:
    """MAX_CURRENT_SOURCES cannot be reached on its own.

    The evidence a turn can gather is

        min(MAX_CURRENT_SOURCES,
            MAX_TOOL_CALLS * (max_context_chunks + heading_expand_chunks))

    so raising the ceiling without raising the tool-call budget produces a
    setting that is simply unreachable -- no error, no warning, and no
    change in behaviour. That is exactly what happened when the ceiling
    went to 32 while the budget still allowed only 4 x 4 = 16.

    heading_expand_chunks belongs in that sum because a search can now
    hand back more than max_context_chunks: a matched heading pulls the
    chunks after it. Leaving it out kept this assertion passing while
    describing a ceiling the code no longer had, which is the failure
    mode this test exists to prevent.
    """
    per_search = SHIPPED_MAX_CONTEXT_CHUNKS + SHIPPED_HEADING_EXPAND_CHUNKS
    reachable = MAX_TOOL_CALLS * per_search
    assert reachable >= MAX_CURRENT_SOURCES, (
        f"MAX_CURRENT_SOURCES={MAX_CURRENT_SOURCES} is unreachable: "
        f"{MAX_TOOL_CALLS} tool calls x {per_search} per search "
        f"tops out at {reachable}"
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


async def test_no_context_allows_bounded_tool_retries_then_returns_model_answer() -> None:
    """Retries stay bounded, and the model's own answer survives the gate.

    The retry bounds are what this test is for. The closing assertion used
    to require NO_EVIDENCE_ANSWER, which pinned the override the loose gate
    removed: the model had already reported that it found nothing, and the
    harness replaced that with a canned string reading like an outage.
    """
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
    assert result.answer == "No relevant evidence found."
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


def direct_answer_model(answer_text: str) -> FunctionModel:
    """A model that answers on the first turn and never calls a tool.

    Stands in for the routing system_prompt asks for: greetings and small
    talk get an answer without a search. This model ignores tool_choice in
    practice, so both this and the always-search path have to survive the
    answer gate.
    """

    def behavior(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
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


async def test_greeting_is_answered_without_retrieval() -> None:
    """A turn the prompt routes around the tool has to survive the gate.

    This is the failure the loose gate exists to fix. system_prompt tells
    the model to answer greetings without searching, and the old gate
    answered every such reply with "I could not find relevant information
    for that question". The model had followed the instruction and was
    refused for it, so no prompt wording could have worked while it stood.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([])

    harness = AnswerHarness(
        configuration, retriever, model=direct_answer_model("Hello! How can I help?")
    )
    try:
        result = await harness.answer("hi", [workspace_id], [])
    finally:
        await harness.aclose()

    assert retriever.calls == []
    assert result.sources == []
    assert result.answer == "Hello! How can I help?"


async def test_answer_without_sources_has_unresolvable_labels_stripped() -> None:
    """A citation pointing at nothing renders as a broken answer.

    With no sources there is no label to resolve, so keeping the [1] puts
    a reference in front of the user that can never be followed. Stripped,
    the answer either stands on its own or should not have been sent.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([])

    harness = AnswerHarness(
        configuration,
        retriever,
        model=direct_answer_model("Hello! How can I help? [1]"),
    )
    try:
        result = await harness.answer("hi", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.answer == "Hello! How can I help?"
    assert "[" not in result.answer


async def test_retrieval_with_no_results_keeps_the_models_own_account() -> None:
    """Searching and finding nothing is not a reason to override the model.

    The tool already returns "No additional relevant sources were found",
    and system_prompt asks the model to say plainly when the documents do
    not answer the question. Substituting NO_EVIDENCE_ANSWER here hid the
    reason behind a canned string that reads like a retrieval outage.
    """
    configuration = settings()
    workspace_id = uuid4()
    retriever = StubRetriever([[]])
    model_calls = 0

    def behavior(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "artcle 16"})]
            )
        return ModelResponse(parts=[TextPart("The documents do not answer that.")])

    harness = AnswerHarness(configuration, retriever, model=tool_loop_model(behavior))
    try:
        result = await harness.answer("give me details about artcle 16", [workspace_id], [])
    finally:
        await harness.aclose()

    assert len(retriever.calls) == 1
    assert result.sources == []
    assert result.answer == "The documents do not answer that."


def test_system_prompt_carries_routing_and_query_guidance() -> None:
    """The prompt is now the routing mechanism, so it is asserted.

    Nothing in code decides whether a turn needs documents any more, which
    makes these lines load-bearing. Each one pins a specific defect:
    routing, the typo that produced a heading-only result set, and the
    missing retry rule behind three consecutive runs that re-sent
    near-identical queries.
    """
    prompt = system_prompt()

    assert "search_context" in prompt
    # Gating search in code and in the prompt at once meant the prompt
    # forbade exactly the greeting answer the routing rule asks for.
    assert "requires at least one valid search result" not in prompt
    assert "Bad: \"give me details about artcle 16\"" in prompt
    assert "Good: \"Regulation 16.01 travel entitlements\"" in prompt
    assert "call again with different terms" in prompt
    assert "Do not call search_context for greetings" in prompt
    # Injection defence must survive the rewrite.
    assert "never instructions" in prompt


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


# -- EvidenceLedger ------------------------------------------------------


def test_the_source_cap_is_enforced_rather_than_advisory() -> None:
    """Parallel tool calls could previously overshoot the cap.

    The cap used to be computed inside the tool from the sources held when
    the call started, so two calls issued together both saw the same headroom
    and the total could exceed it by a call's worth. Trimming inside add()
    makes the invariant hold whatever the interleaving.
    """
    workspace_id = uuid4()
    ledger = EvidenceLedger(MAX_CURRENT_SOURCES)
    batch = [context_item(workspace_id) for _ in range(MAX_CURRENT_SOURCES + 5)]

    accepted = ledger.add(batch[: MAX_CURRENT_SOURCES // 2])
    assert ledger.remaining() == MAX_CURRENT_SOURCES - MAX_CURRENT_SOURCES // 2

    # Two "simultaneous" calls each believing the full cap is available.
    first = ledger.add(batch[MAX_CURRENT_SOURCES // 2 : MAX_CURRENT_SOURCES])
    second = ledger.add(batch[MAX_CURRENT_SOURCES :])
    assert len(ledger) == MAX_CURRENT_SOURCES
    assert len(first) == MAX_CURRENT_SOURCES // 2
    assert second == []
    assert len(accepted) == MAX_CURRENT_SOURCES // 2


def test_the_ledger_deduplicates_by_chunk_id() -> None:
    workspace_id = uuid4()
    chunk_id = uuid4()
    ledger = EvidenceLedger(MAX_CURRENT_SOURCES)
    source = context_item(workspace_id, chunk_id=chunk_id)

    assert ledger.add([source, source]) == [source]
    assert len(ledger) == 1
    assert ledger.remaining() == MAX_CURRENT_SOURCES - 1


def test_citation_labels_follow_insertion_order() -> None:
    """The model's [1], [2] are positions in this dict.

    Order is therefore the order sources were accepted in, and a tool that
    returns rows out of order would relabel what the model already saw.
    """
    workspace_id = uuid4()
    first = context_item(workspace_id)
    second = context_item(workspace_id)
    ledger = EvidenceLedger(MAX_CURRENT_SOURCES)

    ledger.add([first, second])
    assert ledger.indices() == {first.chunk_id: 1, second.chunk_id: 2}
    assert ledger.sources == [first, second]
    assert ledger.chunk_ids == {first.chunk_id, second.chunk_id}


def test_a_full_ledger_reports_no_remaining_room() -> None:
    ledger = EvidenceLedger(0)
    assert ledger.remaining() == 0
    assert ledger.add([context_item(uuid4())]) == []
    assert len(ledger) == 0


# -- query_rows ----------------------------------------------------------


class StubRows:
    """Stands in for the SQL executor.

    Records what it was asked and hands back prepared rows, so the harness's
    half of the contract is testable without a database. The isolation half
    is row-level security and lives in test_postgres_integration.py.
    """

    def __init__(
        self,
        rows: list[RowResult],
        columns: list[str] | None = None,
        catalogue: Catalogue | None = None,
    ) -> None:
        self._rows = rows
        self._columns = columns or ["id", "amount"]
        self.calls: list[tuple[str, tuple[UUID, ...]]] = []
        self.catalogue_calls: list[tuple[tuple[UUID, ...], int, int, str | None]] = []
        self._values: list[dict[str, object]] = []
        # Derived from the rows so a returned row can be matched to the file
        # the catalogue would have listed, which is how the harness now finds
        # a filename.
        self.catalogue = catalogue or Catalogue(
            entries=[
                CatalogueEntry(
                    document_id=row.document_id,
                    workspace_id=uuid4(),
                    processing_run_id=uuid4(),
                    filename="sales.csv",
                    row_count=len(rows),
                    columns=row.columns,
                )
                for row in rows
            ],
            hidden=0,
        )
        self.error: Exception | None = None

    async def run(
        self, sql: str, *, workspace_ids: tuple[UUID, ...]
    ) -> RowQueryResult:
        self.calls.append((sql, workspace_ids))
        if self.error is not None:
            raise self.error
        return RowQueryResult(
            sources=self._rows, values=self._values, columns=self._columns
        )

    async def queryable_files(
        self,
        *,
        workspace_ids: tuple[UUID, ...],
        offset: int = 0,
        limit: int = 100,
        pattern: str | None = None,
    ) -> Catalogue:
        self.catalogue_calls.append((workspace_ids, offset, limit, pattern))
        return self.catalogue


def row_result(
    workspace_id: UUID, *, row_number: int = 1, document_id: UUID | None = None
) -> RowResult:
    return RowResult(
        chunk_id=uuid4(),
        workspace_id=workspace_id,
        document_id=document_id or uuid4(),
        processing_run_id=uuid4(),
        row_number=row_number,
        data={"id": "1001", "amount": "1500.00"},
        columns=["id", "amount"],
    )


async def test_a_sql_row_becomes_citable_evidence() -> None:
    """A returned row uses the same citation path as a retrieved chunk.

    The row's own id is the chunk_id, which is what lets `cited_response`
    validate a label without knowing anything about SQL. If that stopped
    holding, a cited answer would be refused.
    """
    configuration = settings()
    workspace_id = uuid4()
    result = row_result(workspace_id)
    rows = StubRows([result])
    retriever = StubRetriever([])
    model_calls = 0

    def behavior(_: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            tool_schema = json.dumps(info.function_tools[0].parameters_json_schema)
            assert "workspace_ids" not in tool_schema
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select * from document_rows where document_id = 'abc'"},
                        tool_call_id="rows-1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("There are 1500.00 in row 1. [1]")])

    harness = AnswerHarness(
        configuration, retriever, model=tool_loop_model(behavior), rows=rows  # type: ignore[arg-type]
    )
    try:
        answer = await harness.answer("how much in row 1?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert answer.sources != []
    assert answer.answer.endswith("[1]")
    assert rows.calls == [
        ("select * from document_rows where document_id = 'abc'", (workspace_id,))
    ]
    # The rendered row is what the model reads, so the column names have to be
    # in it: a row of bare values carries no meaning on its own.
    assert answer.sources[0].text == "id=1001 | amount=1500.00"
    assert answer.sources[0].filename == "sales.csv"


async def test_a_rejected_query_is_reported_without_echoing_the_statement() -> None:
    """The rejection reason is safe to show; the model's own SQL is not.

    Echoing the statement back would put table names and structure into the
    conversation, and the model would tend to simply retry it corrected --
    which turns a closed door into a guessing game against the schema.
    """
    configuration = settings()
    workspace_id = uuid4()
    rows = StubRows([])
    rows.error = RowQueryError("Queries may only read document_rows, documents")
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select * from pg_authid where document_id = 'abc'"},
                        tool_call_id="rows-1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("I cannot read that.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        result = await harness.answer("who are the users?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "may only read" in seen
    assert "pg_authid" not in seen
    # No evidence, so the model's own answer stands rather than a canned
    # no-evidence string.
    assert result.sources == []
    assert result.answer == "I cannot read that."


async def test_the_tool_is_absent_without_a_reader() -> None:
    """An unset DSN must mean no access, not unrestricted access."""
    configuration = settings()
    tools_seen: list[str] = []

    def behavior(_: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools_seen.extend(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart("Hello! How can I help?")])

    harness = AnswerHarness(
        configuration, StubRetriever([]), model=tool_loop_model(behavior)
    )
    try:
        await harness.answer("hi", [uuid4()], [])
    finally:
        await harness.aclose()

    assert "search_context" in tools_seen
    assert "query_rows" not in tools_seen


async def test_rows_and_searches_share_one_source_budget() -> None:
    """Two tools drawing on one pool cannot each spend what is left.

    The cap moved out of search_context into the ledger precisely so a row
    result could not be admitted past a limit the search tool had already
    accounted for.
    """
    configuration = settings()
    workspace_id = uuid4()
    # Three rows of one file, not three files: query_rows refuses a result
    # spanning more than one document, which is the point of the second guard.
    document_id = uuid4()
    rows = StubRows(
        [
            row_result(workspace_id, row_number=number, document_id=document_id)
            for number in (1, 2, 3)
        ]
    )
    model_calls = 0

    def behavior(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select * from document_rows where document_id = 'abc'"},
                        "1",
                    )
                ]
            )
        if model_calls == 2:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "policy"}, "2")]
            )
        return ModelResponse(parts=[TextPart("Three rows. [1] [2] [3]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([[]]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        # The search returns nothing, so the turn ends with the rows held.
        result = await harness.answer("count them", [workspace_id], [])
    finally:
        await harness.aclose()

    assert len(result.sources) == 3


# -- catalogue -----------------------------------------------------------


def catalogue_of(*filenames: str, columns: list[str] | None = None) -> Catalogue:
    return Catalogue(
        entries=[
            CatalogueEntry(
                document_id=uuid4(),
                workspace_id=uuid4(),
                processing_run_id=uuid4(),
                filename=name,
                row_count=42,
                columns=columns if columns is not None else ["id", "status"],
            )
            for name in filenames
        ],
        hidden=0,
    )


async def test_the_catalogue_travels_with_every_query_result() -> None:
    """Column names differ per file, so the model must be told, not guess.

    It used to be handed the table's own columns under a "Columns available"
    heading, which is useless when the question was what a CSV is called.
    A misspelled key then returned nothing and read as missing data.
    """
    configuration = settings()
    workspace_id = uuid4()
    document_id = uuid4()
    rows = StubRows(
        [row_result(workspace_id, document_id=document_id)],
        catalogue=Catalogue(
            entries=[
                CatalogueEntry(
                    document_id=document_id,
                    workspace_id=uuid4(),
                    processing_run_id=uuid4(),
                    filename="inventory.csv",
                    row_count=1,
                    columns=["sku", "warehouse", "qty"],
                )
            ],
            hidden=0,
        ),
    )
    model_calls = 0

    def behavior(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select * from document_rows where document_id = 'x'"},
                        "1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Five in WH1. [1]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        result = await harness.answer("how many in WH1?", [workspace_id], [])
    finally:
        await harness.aclose()

    # The catalogue is attached on the tool result the model reads, so the
    # assertions below look at what it saw rather than at the answer.
    assert result.sources[0].filename == "inventory.csv"
    assert result.sources[0].text == "id=1001 | amount=1500.00"


async def test_the_catalogue_reaches_search_context_too() -> None:
    """Routing needs it: the model cannot choose query_rows for a file it
    does not know exists, and search_context is the call it makes first."""
    configuration = settings()
    workspace_id = uuid4()
    rows = StubRows([], catalogue=catalogue_of("inventory.csv"))
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "policy"}, "1")]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("Nothing. [1]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([[context_item(workspace_id)]]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("what do we have?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "inventory.csv" in seen
    assert "id, status" in seen


async def test_the_catalogue_is_fetched_once_per_turn() -> None:
    """The file list cannot change mid-turn, and it rides along for free.

    Repeating it on every call was chosen over fetching once, because this
    model already ignores tool_choice and retries near-identical queries --
    but the fetch itself still happens once, so a turn of ten tool calls
    does not become ten catalogue queries.
    """
    configuration = settings()
    workspace_id = uuid4()
    document_id = uuid4()
    rows = StubRows(
        [row_result(workspace_id, document_id=document_id)],
        catalogue=catalogue_of("inventory.csv"),
    )
    model_calls = 0

    def behavior(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls <= 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select * from document_rows where document_id = 'x'"},
                        str(model_calls),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("One row. [1]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("how many?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert len(rows.catalogue_calls) == 1


async def test_a_hidden_file_count_is_reported_rather_than_silently_dropped() -> None:
    """A truncated list that looks complete invites a confident wrong answer."""
    configuration = settings()
    workspace_id = uuid4()
    catalogue = catalogue_of("a.csv", "b.csv")
    rows = StubRows([], catalogue=Catalogue(entries=catalogue.entries, hidden=47))
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select count(*) from document_rows where document_id = 'x'"},
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("None.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("count", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "and 47 more file(s)" in seen
    assert "list_queryable_files" in seen


async def test_a_query_naming_no_document_is_refused_before_it_runs() -> None:
    """A count across unrelated tables is not an answer to any question."""
    configuration = settings()
    workspace_id = uuid4()
    rows = StubRows([], catalogue=catalogue_of("sales.csv"))
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select count(*) from document_rows"},
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("Many.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("count all", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "does not name a document_id" in seen
    # Refused before execution, so the retriever was never asked.
    assert rows.calls == []


async def test_rows_from_two_files_are_refused_even_when_document_id_is_present() -> None:
    """The presence test alone is not enough.

    A statement can name a document_id without narrowing anything, so the
    result is checked too. Two files' rows reaching one answer is the case
    that produces a confident number about the wrong data.
    """
    configuration = settings()
    workspace_id = uuid4()
    first = uuid4()
    second = uuid4()
    rows = StubRows(
        [
            row_result(workspace_id, row_number=1, document_id=first),
            row_result(workspace_id, row_number=1, document_id=second),
        ],
        catalogue=Catalogue(
            entries=[
                CatalogueEntry(
                    document_id=first,
                    workspace_id=uuid4(),
                    processing_run_id=uuid4(),
                    filename="inventory.csv",
                    row_count=1,
                    columns=["sku"],
                ),
                CatalogueEntry(
                    document_id=second,
                    workspace_id=uuid4(),
                    processing_run_id=uuid4(),
                    filename="sales.csv",
                    row_count=1,
                    columns=["id"],
                ),
            ],
            hidden=0,
        ),
    )
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select * from document_rows where document_id is not null"},
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("Two rows.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        result = await harness.answer("show me rows", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "rows came from 2 files" in seen
    assert "inventory.csv" in seen
    assert "sales.csv" in seen
    # Refused rows are not admitted as evidence.
    assert result.sources == []


async def test_list_queryable_files_pages_beyond_the_catalogue_cap() -> None:
    configuration = settings()
    workspace_id = uuid4()
    rows = StubRows([], catalogue=catalogue_of("later.csv", columns=["a", "b"]))
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "list_queryable_files",
                        {"offset": 100, "limit": 50, "pattern": "later"},
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("Found it.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("what else is there?", [workspace_id], [])
    finally:
        await harness.aclose()

    # Paging deliberately bypasses the per-turn cache: it is asking for a
    # different page, not the same list again.
    assert rows.catalogue_calls[-1][1:] == (100, 50, "later")
    assert "later.csv" in seen
    assert "a, b" in seen


async def test_no_catalogue_is_stated_plainly_when_no_csv_exists() -> None:
    """An empty list that reads like a failure gets a clarification instead."""
    configuration = settings()
    workspace_id = uuid4()
    rows = StubRows([])
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select count(*) from document_rows where document_id = 'x'"},
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("There are none.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("any csv?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "none yet" in seen


async def test_an_aggregate_answer_reaches_the_model() -> None:
    """A count or a total is the answer, and it has no row to cite.

    This path used to report "no rows matched" to a question it had just
    computed the answer for. The values were dropped for having no id, only
    the column names came back, and the model read an aggregate that returned
    a number as an empty result -- so it told the user the data did not exist.
    """
    configuration = settings()
    workspace_id = uuid4()
    rows = StubRows([], catalogue=catalogue_of("techcorp.csv"))
    rows._values = [{"rows_matched": 4, "total_revenue_usd": 12120733.41}]
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {
                            "sql": "select count(*) as rows_matched, "
                            "sum((data->>'Revenue_USD')::numeric) as total_revenue_usd "
                            "from document_rows where document_id = 'x'"
                        },
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("The total is 12,120,733.41.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        result = await harness.answer(
            "total revenue for cloud infrastructure in north america 2023?",
            [workspace_id],
            [],
        )
    finally:
        await harness.aclose()

    assert "12120733.41" in seen
    assert "rows_matched" in seen
    assert "not citable" in seen
    # Nothing to cite, so no evidence and the model's own answer stands.
    assert result.sources == []


async def test_a_zero_count_is_a_result_not_a_miss() -> None:
    """Zero is an answer. Reporting it as "no rows matched" invents absence."""
    configuration = settings()
    workspace_id = uuid4()
    rows = StubRows([], catalogue=catalogue_of("sales.csv"))
    rows._values = [{"n": 0}]
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select count(*) as n from document_rows where document_id = 'x'"},
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("None match.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("any?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "No rows matched" not in seen
    assert "12120733.41" not in seen


class StubCatalogueReader:
    """Lists files without the restricted reader, as when row queries are off."""

    def __init__(self, catalogue: Catalogue) -> None:
        self._catalogue = catalogue

    async def queryable_files(self, *, workspace_ids: tuple[UUID, ...]) -> Catalogue:
        del workspace_ids
        return self._catalogue


async def test_a_csv_is_still_reported_when_row_queries_are_switched_off() -> None:
    """The failure this prevents was a confidently wrong answer.

    With no reader DSN the row tool is not registered, and the model had
    nothing at all pointing at the CSVs. It searched the prose documents,
    found none matching, and told the user the workspace held no such data --
    while a 320-row CSV sat there holding the answer. Saying "the data is
    there but I cannot query it" and saying "the data does not exist" are
    both refusals, but only one of them is true.
    """
    configuration = settings()
    workspace_id = uuid4()
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_context", {"query": "cloud revenue"}, "1")]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("The data is not available.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([[context_item(workspace_id)]]),
        model=tool_loop_model(behavior),
        rows=None,
        catalogue_reader=StubCatalogueReader(  # type: ignore[arg-type]
            catalogue_of("techcorp.csv", columns=["Year", "Business_Segment"])
        ),
    )
    try:
        await harness.answer("total cloud revenue?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "techcorp.csv" in seen
    assert "Business_Segment" in seen
    # Told plainly that this is a limit on the tool, not a fact about the data.
    assert "not queryable" in seen
    assert "never as absent" in seen


async def test_a_listing_is_labelled_queryable_when_the_tool_is_available() -> None:
    """The warning is only true when row queries are actually off."""
    configuration = settings()
    workspace_id = uuid4()
    rows = StubRows([], catalogue=catalogue_of("sales.csv"))
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": "select count(*) as n from document_rows where document_id = 'x'"},
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("None.")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("any?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "not queryable" not in seen


async def test_repeated_aggregates_on_one_file_collapse_to_a_single_citation() -> None:
    """A group-by is one piece of evidence, not one per computed cell.

    Emitting a source per computed row turned a single answer into a citation
    list of twenty-one entries for the same file, most of them from the model
    sanity-checking its own arithmetic. The file is the evidence, so it is
    cited once.
    """
    configuration = settings()
    workspace_id = uuid4()
    document_id = uuid4()
    rows = StubRows(
        [],
        catalogue=Catalogue(
            entries=[
                CatalogueEntry(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    processing_run_id=uuid4(),
                    filename="techcorp.csv",
                    row_count=320,
                    columns=["Year", "Business_Segment"],
                )
            ],
            hidden=0,
        ),
    )
    model_calls = 0
    queries = [
        "select data->>'Quarter' as q, count(*) as n from document_rows "
        f"where document_id = '{document_id}' group by 1",
        "select min((data->>'Revenue_USD')::numeric) as lo, "
        "max((data->>'Revenue_USD')::numeric) as hi from document_rows "
        f"where document_id = '{document_id}'",
        "select count(*) as total from document_rows "
        f"where document_id = '{document_id}'",
    ]
    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls <= 3:
            rows._values = [
                {"quarter": f"Q{index}", "n": 4 * index} for index in range(1, 8)
            ]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {"sql": queries[model_calls - 1]},
                        str(model_calls - 1),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Total 12,120,733.41. [1]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        result = await harness.answer("total by quarter", [workspace_id], [])
    finally:
        await harness.aclose()

    assert len(result.sources) == 1
    assert result.sources[0].filename == "techcorp.csv"
    # Not a line of the file, so no row number is invented for it.
    assert result.sources[0].ordinal == 0
    # The values are in the block, so the model can read them.
    assert "quarter" in result.sources[0].text


async def test_aggregates_over_two_files_stay_two_citations() -> None:
    """Collapsing is per file, not global: different evidence stays separate."""
    configuration = settings()
    workspace_id = uuid4()
    first = uuid4()
    second = uuid4()
    entries = [
        CatalogueEntry(
            document_id=value,
            workspace_id=workspace_id,
            processing_run_id=uuid4(),
            filename=name,
            row_count=10,
            columns=["a"],
        )
        for value, name in ((first, "one.csv"), (second, "two.csv"))
    ]
    rows = StubRows([], catalogue=Catalogue(entries=entries, hidden=0))
    rows._values = [{"n": 3}]
    model_calls = 0

    def behavior(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {
                            "sql": "select count(*) as n from document_rows "
                            f"where document_id = '{first}'"
                        },
                        "1",
                    )
                ]
            )
        if model_calls == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {
                            "sql": "select count(*) as n from document_rows "
                            f"where document_id = '{second}'"
                        },
                        "2",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Three in each. [1][2]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        result = await harness.answer("count both", [workspace_id], [])
    finally:
        await harness.aclose()

    assert {source.filename for source in result.sources} == {"one.csv", "two.csv"}


async def test_csv_sources_carry_no_section_path() -> None:
    """The column names are already in the text.

    In the citation list this field renders as a breadcrumb, so filling it
    with a CSV's columns produced "id / status / amount" on every entry, and
    with a computed query's aliases produced "below_25 / negative_check /
    total_rows" -- internal scratch work presented as if it were a location.
    """
    configuration = settings()
    workspace_id = uuid4()
    document_id = uuid4()
    rows = StubRows(
        [row_result(workspace_id, document_id=document_id)],
        catalogue=Catalogue(
            entries=[
                CatalogueEntry(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    processing_run_id=uuid4(),
                    filename="inventory.csv",
                    row_count=1,
                    columns=["sku", "warehouse", "qty"],
                )
            ],
            hidden=0,
        ),
    )
    model_calls = 0

    def behavior(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {
                            "sql": "select * from document_rows "
                            f"where document_id = '{document_id}'"
                        },
                        "1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Five in WH1. [1]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        result = await harness.answer("how many in WH1?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert result.sources[0].section_path == []
    # The cells are still readable; only the redundant field is gone.
    assert result.sources[0].text == "id=1001 | amount=1500.00"


async def test_aggregate_values_are_not_rendered_twice() -> None:
    """The source block carries them, so the summary table would duplicate."""
    configuration = settings()
    workspace_id = uuid4()
    document_id = uuid4()
    rows = StubRows(
        [],
        catalogue=Catalogue(
            entries=[
                CatalogueEntry(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    processing_run_id=uuid4(),
                    filename="techcorp.csv",
                    row_count=320,
                    columns=["Year"],
                )
            ],
            hidden=0,
        ),
    )
    rows._values = [{"total_revenue_usd": 12120733.41}]
    model_calls = 0
    seen = ""

    def behavior(messages: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        nonlocal model_calls, seen
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_rows",
                        {
                            "sql": "select sum((data->>'Revenue_USD')::numeric) "
                            "as total_revenue_usd from document_rows "
                            f"where document_id = '{document_id}'"
                        },
                        "1",
                    )
                ]
            )
        seen = "".join(
            part.content
            for part in getattr(messages[-1], "parts", [])
            if getattr(part, "content", None)
        )
        return ModelResponse(parts=[TextPart("12,120,733.41. [1]")])

    harness = AnswerHarness(
        configuration,
        StubRetriever([]),
        model=tool_loop_model(behavior),
        rows=rows,  # type: ignore[arg-type]
    )
    try:
        await harness.answer("total?", [workspace_id], [])
    finally:
        await harness.aclose()

    assert "12120733.41" in seen
    # The summary table is the uncitable fallback only; with a source present
    # it would repeat the same numbers a second time.
    assert "Result (computed, not citable)" not in seen
