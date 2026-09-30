import re
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AgentClient
from app.api.routes.agent import agent_unavailable_error, searchable_workspace_ids
from app.api.schemas import (
    AgentAnswerResponse,
    AgentConversationCreateRequest,
    AgentConversationListResponse,
    AgentConversationSummary,
    AgentConversationTurnListResponse,
    AgentConversationTurnRequest,
    AgentConversationTurnResponse,
    AgentHistoryMessage,
    ContextSource,
    ConversationSourceReference,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import AgentConversation, AgentConversationTurn
from app.db.session import get_session
from app.security.auth import CurrentUser
from app.security.context_delegation import MAX_SCOPE_WORKSPACES, create_context_delegation

router = APIRouter(prefix="/v1/agent/conversations", tags=["agent-conversations"])
HISTORY_TURN_LIMIT = 12
HISTORY_CHARACTER_LIMIT = 48_000
GREETING_ONLY_MESSAGES = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "hi there",
        "hello there",
        "hey there",
        "good morning",
        "good afternoon",
        "good evening",
        "halo",
        "hai",
        "selamat pagi",
        "selamat siang",
        "selamat sore",
        "selamat malam",
        "assalamualaikum",
        "salam",
    }
)
GREETING_RESPONSE = (
    "Hello! I can help answer questions using information in your authorized workspaces."
)


@router.post("", response_model=AgentConversationSummary, status_code=201)
async def create_conversation(
    payload: AgentConversationCreateRequest,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> AgentConversationSummary:
    conversation = AgentConversation(owner_user_id=current_user.id, title=payload.title)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    response.headers["Cache-Control"] = "no-store"
    return conversation_summary(conversation)


@router.get("", response_model=AgentConversationListResponse)
async def list_conversations(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> AgentConversationListResponse:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(AgentConversation)
            .where(AgentConversation.owner_user_id == current_user.id)
        )
        or 0
    )
    result = await session.scalars(
        select(AgentConversation)
        .where(AgentConversation.owner_user_id == current_user.id)
        .order_by(AgentConversation.updated_at.desc(), AgentConversation.id)
        .offset(offset)
        .limit(limit)
    )
    response.headers["Cache-Control"] = "no-store"
    return AgentConversationListResponse(
        items=[conversation_summary(conversation) for conversation in result.all()],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{conversation_id}/turns", response_model=AgentConversationTurnListResponse
)
async def list_conversation_turns(
    conversation_id: UUID,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AgentConversationTurnListResponse:
    conversation = await owned_conversation(session, conversation_id, current_user.id)
    workspace_ids = await searchable_workspace_ids(session, current_user.id)
    allowed = set(workspace_ids)
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(AgentConversationTurn)
            .where(AgentConversationTurn.conversation_id == conversation.id)
        )
        or 0
    )
    result = await session.scalars(
        select(AgentConversationTurn)
        .where(AgentConversationTurn.conversation_id == conversation.id)
        .order_by(AgentConversationTurn.sequence)
        .offset(offset)
        .limit(limit)
    )
    items = [
        turn_response(
            turn,
            context_available=set(turn_workspace_ids(turn)).issubset(allowed),
        )
        for turn in result.all()
    ]
    response.headers["Cache-Control"] = "no-store"
    return AgentConversationTurnListResponse(
        items=items, total=total, offset=offset, limit=limit
    )


@router.post(
    "/{conversation_id}/turns", response_model=AgentConversationTurnResponse
)
async def create_conversation_turn(
    conversation_id: UUID,
    payload: AgentConversationTurnRequest,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: AgentClient,
    response: Response,
) -> AgentConversationTurnResponse:
    workspace_ids = await searchable_workspace_ids(session, current_user.id)
    greeting_only = is_greeting_only(payload.question)
    if not greeting_only:
        require_search_scope(workspace_ids)
    await session.commit()

    now = datetime.now(UTC)
    turn, conversation, history_turns, replay, reservation_token = await reserve_turn(
        session,
        conversation_id=conversation_id,
        owner_user_id=current_user.id,
        request_id=payload.request_id,
        question=payload.question,
        allowed_workspace_ids=set(workspace_ids),
        stale_after_seconds=settings.agent_timeout_seconds + 60,
        now=now,
    )
    if replay:
        response.headers["Cache-Control"] = "no-store"
        return turn_response(turn, context_available=True)

    turn_id = turn.id
    history = (
        build_history(history_turns, set(workspace_ids)) if not greeting_only else []
    )
    completed = False
    try:
        if greeting_only:
            agent_response = AgentAnswerResponse(answer=GREETING_RESPONSE, sources=[])
        else:
            delegation = create_context_delegation(current_user, workspace_ids, settings)
            try:
                upstream = await client.post(
                    f"{settings.agent_base_url}/v1/answer",
                    headers={
                        "Authorization": f"Bearer {settings.agent_api_token.get_secret_value()}"
                    },
                    json={
                        "question": payload.question,
                        "scope_token": delegation,
                        "history": [message.model_dump(mode="json") for message in history],
                    },
                )
            except httpx.TimeoutException as exc:
                raise agent_unavailable_error("The answer service timed out.") from exc
            except httpx.HTTPError as exc:
                raise agent_unavailable_error("The answer service is unavailable.") from exc
            if upstream.status_code != 200:
                raise agent_unavailable_error(
                    "The answer service could not complete the request."
                )
            try:
                agent_response = AgentAnswerResponse.model_validate(upstream.json())
            except (ValueError, TypeError) as exc:
                raise agent_unavailable_error(
                    "The answer service returned an invalid response."
                ) from exc

        if any(source.workspace_id not in workspace_ids for source in agent_response.sources):
            raise agent_unavailable_error("The answer service returned an out-of-scope source.")

        source_refs = [source_reference(source) for source in agent_response.sources]
        dependency_workspace_ids = {
            workspace_id
            for history_message in history
            for workspace_id in history_message.source_workspace_ids
        }
        dependency_workspace_ids.update(source.workspace_id for source in agent_response.sources)

        turn = await complete_turn(
            session,
            conversation_id=conversation.id,
            turn_id=turn_id,
            reservation_token=reservation_token,
            answer=agent_response.answer,
            source_refs=[source.model_dump(mode="json") for source in source_refs],
            source_workspace_ids=dependency_workspace_ids,
        )
        completed = True
        response.headers["Cache-Control"] = "no-store"
        return turn_response(turn, context_available=True)
    finally:
        if not completed:
            await release_turn(
                session, conversation_id, turn_id, reservation_token
            )


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    conversation = await session.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.id == conversation_id,
            AgentConversation.owner_user_id == current_user.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise conversation_not_found_error()
    if conversation.active_turn_id is not None:
        raise AppError(
            status_code=409,
            code="conversation_turn_in_progress",
            title="Conversation turn in progress",
            detail="Wait for the current answer to finish before deleting this conversation.",
        )
    await session.delete(conversation)
    await session.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


async def reserve_turn(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    owner_user_id: UUID,
    request_id: UUID,
    question: str,
    allowed_workspace_ids: set[UUID],
    stale_after_seconds: float,
    now: datetime,
) -> tuple[
    AgentConversationTurn,
    AgentConversation,
    list[AgentConversationTurn],
    bool,
    UUID,
]:
    conversation = await session.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.id == conversation_id,
            AgentConversation.owner_user_id == owner_user_id,
        )
        .with_for_update()
    )
    if conversation is None:
        raise conversation_not_found_error()

    existing = await session.scalar(
        select(AgentConversationTurn).where(
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.request_id == request_id,
        )
    )
    if existing is not None and existing.question != question:
        raise AppError(
            status_code=409,
            code="idempotency_key_reused",
            title="Request ID already used",
            detail="Use a new request ID when sending a different question.",
        )
    if existing is not None and existing.status == "completed":
        if not set(turn_workspace_ids(existing)).issubset(allowed_workspace_ids):
            raise context_access_changed_error()
        await session.commit()
        return existing, conversation, [], True, existing.reservation_token
    if (
        existing is not None
        and existing.status == "processing"
        and conversation.active_turn_id != existing.id
    ):
        raise conversation_busy_error()

    if conversation.active_turn_id is not None:
        active_turn = await session.get(AgentConversationTurn, conversation.active_turn_id)
        active_started_at = conversation.active_turn_started_at
        if active_started_at is not None and active_started_at.tzinfo is None:
            active_started_at = active_started_at.replace(tzinfo=UTC)
        is_stale = (
            active_turn is None
            or active_turn.status != "processing"
            or active_started_at is None
            or (now - active_started_at).total_seconds() > stale_after_seconds
        )
        if not is_stale:
            raise conversation_busy_error()
        if active_turn is not None and active_turn.status == "processing":
            active_turn.status = "failed"
            active_turn.failure_code = "turn_timeout"
            active_turn.completed_at = now
        elif (
            existing is not None
            and existing.status == "processing"
            and existing.id == conversation.active_turn_id
        ):
            existing.status = "failed"
            existing.failure_code = "turn_timeout"
            existing.completed_at = now
        conversation.active_turn_id = None
        conversation.active_turn_started_at = None

    if existing is not None and existing.status == "processing":
        raise conversation_busy_error()

    if existing is None:
        reservation_token = uuid4()
        turn = AgentConversationTurn(
            id=uuid4(),
            reservation_token=reservation_token,
            conversation_id=conversation.id,
            request_id=request_id,
            sequence=conversation.next_sequence,
            question=question,
            source_refs=[],
            source_workspace_ids=[],
            status="processing",
        )
        conversation.next_sequence += 1
        session.add(turn)
        if conversation.title is None:
            conversation.title = question[:160]
    else:
        turn = existing
        reservation_token = uuid4()
        turn.reservation_token = reservation_token
        turn.status = "processing"
        turn.answer = None
        turn.source_refs = []
        turn.source_workspace_ids = []
        turn.failure_code = None
        turn.completed_at = None

    conversation.active_turn_id = turn.id
    conversation.active_turn_started_at = now
    conversation.updated_at = now
    await session.flush()

    previous = await session.scalars(
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.conversation_id == conversation.id,
            AgentConversationTurn.status == "completed",
            AgentConversationTurn.sequence < turn.sequence,
        )
        .order_by(AgentConversationTurn.sequence.desc())
        .limit(HISTORY_TURN_LIMIT)
    )
    previous_turns = list(reversed(previous.all()))
    await session.commit()
    return turn, conversation, previous_turns, False, reservation_token


def build_history(
    turns: list[AgentConversationTurn], allowed_workspace_ids: set[UUID]
) -> list[AgentHistoryMessage]:
    included_pairs: list[tuple[AgentHistoryMessage, AgentHistoryMessage]] = []
    remaining_characters = HISTORY_CHARACTER_LIMIT
    for turn in reversed(turns):
        workspace_ids = set(turn_workspace_ids(turn))
        if not workspace_ids.issubset(allowed_workspace_ids):
            continue
        if turn.answer is None:
            continue
        pair_size = len(turn.question) + len(turn.answer)
        if pair_size > remaining_characters:
            continue
        included_pairs.append(
            (
                AgentHistoryMessage(role="user", content=turn.question),
                AgentHistoryMessage(
                    role="assistant",
                    content=history_answer_content(turn),
                    source_workspace_ids=sorted(workspace_ids),
                ),
            )
        )
        remaining_characters -= pair_size
    return [message for pair in reversed(included_pairs) for message in pair]


def history_answer_content(turn: AgentConversationTurn) -> str:
    if turn_workspace_ids(turn):
        return (
            "A prior document-grounded answer is available in this user's transcript. "
            "Search current authorized evidence before making factual claims in this turn."
        )
    return turn.answer or ""


def turn_workspace_ids(turn: AgentConversationTurn) -> list[UUID]:
    workspace_ids = {UUID(str(item)) for item in (turn.source_workspace_ids or [])}
    workspace_ids.update(UUID(str(source["workspace_id"])) for source in turn.source_refs or [])
    return sorted(workspace_ids)


async def release_turn(
    session: AsyncSession,
    conversation_id: UUID,
    turn_id: UUID,
    reservation_token: UUID,
) -> None:
    try:
        conversation = await session.scalar(
            select(AgentConversation)
            .where(AgentConversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        turn = await session.scalar(
            select(AgentConversationTurn)
            .where(AgentConversationTurn.id == turn_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        owns_reservation = (
            conversation is not None
            and turn is not None
            and conversation.active_turn_id == turn_id
            and turn.reservation_token == reservation_token
            and turn.status == "processing"
        )
        if owns_reservation and turn is not None and conversation is not None:
            turn.status = "failed"
            turn.failure_code = "answer_unavailable"
            turn.completed_at = datetime.now(UTC)
            conversation.active_turn_id = None
            conversation.active_turn_started_at = None
            conversation.updated_at = datetime.now(UTC)
        await session.commit()
    except Exception:
        await session.rollback()


async def complete_turn(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    turn_id: UUID,
    reservation_token: UUID,
    answer: str,
    source_refs: list[dict[str, object]],
    source_workspace_ids: set[UUID],
) -> AgentConversationTurn:
    conversation = await session.scalar(
        select(AgentConversation)
        .where(AgentConversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    turn = await session.scalar(
        select(AgentConversationTurn)
        .where(AgentConversationTurn.id == turn_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        conversation is None
        or turn is None
        or conversation.active_turn_id != turn_id
        or turn.status != "processing"
        or turn.reservation_token != reservation_token
    ):
        await session.rollback()
        raise conversation_turn_superseded_error()

    now = datetime.now(UTC)
    turn.answer = answer
    turn.source_refs = source_refs
    turn.source_workspace_ids = sorted(str(item) for item in source_workspace_ids)
    turn.status = "completed"
    turn.failure_code = None
    turn.completed_at = now
    conversation.active_turn_id = None
    conversation.active_turn_started_at = None
    conversation.updated_at = now
    await session.commit()
    return turn


async def owned_conversation(
    session: AsyncSession, conversation_id: UUID, owner_user_id: UUID
) -> AgentConversation:
    conversation = await session.scalar(
        select(AgentConversation).where(
            AgentConversation.id == conversation_id,
            AgentConversation.owner_user_id == owner_user_id,
        )
    )
    if conversation is None:
        raise conversation_not_found_error()
    return conversation


def conversation_summary(conversation: AgentConversation) -> AgentConversationSummary:
    return AgentConversationSummary(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def turn_response(
    turn: AgentConversationTurn, *, context_available: bool
) -> AgentConversationTurnResponse:
    return AgentConversationTurnResponse(
        id=turn.id,
        request_id=turn.request_id,
        sequence=turn.sequence,
        question=turn.question,
        answer=turn.answer,
        status=cast(Literal["processing", "completed", "failed"], turn.status),
        failure_code=turn.failure_code,
        created_at=turn.created_at,
        completed_at=turn.completed_at,
        sources=(
            [
                ConversationSourceReference.model_validate(source)
                for source in turn.source_refs or []
            ]
            if context_available
            else []
        ),
        context_available=context_available,
    )


def source_reference(source: ContextSource) -> ConversationSourceReference:
    return ConversationSourceReference(
        workspace_id=source.workspace_id,
        document_id=source.document_id,
        processing_run_id=source.processing_run_id,
        chunk_id=source.chunk_id,
        ordinal=source.ordinal,
        score=source.score,
        content_hash=source.content_hash,
        filename=source.filename,
        section_path=source.section_path,
        page_numbers=source.page_numbers,
    )


def require_search_scope(workspace_ids: list[UUID]) -> None:
    if not workspace_ids:
        raise AppError(
            status_code=403,
            code="context_access_required",
            title="Workspace context access required",
            detail=(
                "Join a workspace with permission to search its context before asking a question."
            ),
        )
    if len(workspace_ids) > MAX_SCOPE_WORKSPACES:
        raise AppError(
            status_code=422,
            code="context_scope_too_large",
            title="Workspace context scope is too large",
            detail="Ask an administrator to reduce searchable workspace memberships before asking.",
        )


def conversation_not_found_error() -> AppError:
    return AppError(
        status_code=404,
        code="conversation_not_found",
        title="Conversation not found",
        detail="The conversation does not exist or is not available to this user.",
    )


def conversation_busy_error() -> AppError:
    return AppError(
        status_code=409,
        code="conversation_turn_in_progress",
        title="Conversation turn in progress",
        detail="Wait for the current response to finish before sending another message.",
    )


def conversation_turn_superseded_error() -> AppError:
    return AppError(
        status_code=409,
        code="conversation_turn_superseded",
        title="Conversation turn was superseded",
        detail="A newer retry took over this turn. Its late answer was discarded.",
    )


def context_access_changed_error() -> AppError:
    return AppError(
        status_code=403,
        code="conversation_context_access_changed",
        title="Conversation context access changed",
        detail=(
            "This request cannot be replayed because its sources are no longer "
            "in your current scope."
        ),
    )


def is_greeting_only(question: str) -> bool:
    normalized = re.sub(r"[^\w\s]", " ", question.casefold(), flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    return normalized in GREETING_ONLY_MESSAGES
