"""The harness's HTTP face: one chat endpoint that streams, scoped by membership.

The conversation itself is the client's business -- the request carries the
history it wants continued and nothing is stored here. What the server owns
is the loop: the model configured by an admin, the tools scoped to every
workspace the caller can read, and the progress events rendered by the
dashboard.
"""

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.harness import run_agent
from app.agents.model_settings import (
    fresh_session_id,
    load_agent_model_config,
    provider_for,
)
from app.agents.toolkit import AgentToolKit
from app.api.dependencies import CurrentUser
from app.api.schemas import AgentChatRequest
from app.core.errors import AppError
from app.db.session import get_session
from app.mcp.auth import McpPrincipal
from app.mcp.runtime import McpNotReadyError, McpRuntime
from app.retrieval.scope import searchable_workspace_ids

router = APIRouter(prefix="/v1/agent", tags=["agent"])


async def _runtime(request: Request) -> McpRuntime:
    try:
        return request.app.state.mcp.runtime
    except McpNotReadyError as exc:
        raise AppError(
            status_code=503,
            code="mcp_not_ready",
            title="Retrieval not ready",
            detail="The retrieval layer is not accepting requests yet.",
        ) from exc


@router.post("/chat")
async def agent_chat(
    payload: AgentChatRequest,
    current_user: CurrentUser,
    runtime: Annotated[McpRuntime, Depends(_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    """Ask one question, with the conversation so far, and stream the work.

    The reply is a stream of JSON events, one per ``data:`` line: the
    assistant's turns, the tool calls it made with a preview of each result,
    and finally ``final`` with the answer or ``error`` with the reason there
    is none. Nothing about the conversation is stored server-side.

    The agent sees every workspace the caller's roles let it search -- the
    same resolution an API key gets -- and the document tools re-check the
    caller's permission on the named workspace at every call, so a workspace
    joined later appears and a lost role bites immediately.

    ``session_id`` is the client's stable id for the conversation, reused
    across requests so providers that key routing or caching on one (OpenCode
    Go rejects calls without it) see the same conversation they saw before;
    when it is absent one is generated for this request alone.
    """
    workspace_ids = await searchable_workspace_ids(session, current_user.id)
    if not workspace_ids:
        raise AppError(
            status_code=403,
            code="no_searchable_workspaces",
            title="No searchable workspaces",
            detail="The caller is not a member of any workspace the agent could search.",
        )
    config = await load_agent_model_config(session)
    if config is None:
        raise AppError(
            status_code=409,
            code="agent_not_configured",
            title="Agent not configured",
            detail="An administrator has not configured the chat model for the harness yet.",
        )
    kit = AgentToolKit(
        runtime,
        McpPrincipal(user=current_user, workspace_ids=workspace_ids),
    )
    history = [
        {"role": message.role, "content": message.content} for message in payload.history
    ]
    session_id = payload.session_id or fresh_session_id()

    async def events() -> AsyncIterator[bytes]:
        provider = provider_for(config, session_id)
        try:
            async for event in run_agent(
                provider=provider,
                kit=kit,
                history=history,
                message=payload.message,
            ):
                yield f"data: {json.dumps(event)}\n\n".encode()
        finally:
            await provider.aclose()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
