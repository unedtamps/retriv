"""The harness's HTTP face: one workspace-scoped chat endpoint that streams.

The conversation itself is the client's business -- the request carries the
history it wants continued and nothing is stored here. What the server owns
is the loop: the model configured by an admin, the tools scoped to the
request's principal, and the progress events rendered by the dashboard.
"""

import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

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
from app.api.dependencies import CurrentUser, WorkspaceContext
from app.api.schemas import AgentChatRequest
from app.core.errors import AppError
from app.db.session import get_session
from app.mcp.auth import McpPrincipal
from app.mcp.runtime import McpNotReadyError, McpRuntime

router = APIRouter(prefix="/v1/workspaces", tags=["agent"])


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


@router.post("/{workspace_id}/agent/chat")
async def agent_chat(
    workspace_id: UUID,
    payload: AgentChatRequest,
    workspace: WorkspaceContext,
    current_user: CurrentUser,
    runtime: Annotated[McpRuntime, Depends(_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    """Ask one question, with the conversation so far, and stream the work.

    The reply is a stream of JSON events, one per ``data:`` line: the
    assistant's turns, the tool calls it made with a preview of each result,
    and finally ``final`` with the answer or ``error`` with the reason there
    is none. Nothing about the conversation is stored server-side.

    ``session_id`` is the client's stable id for the conversation, reused
    across requests so providers that key routing or caching on one (OpenCode
    Go rejects calls without it) see the same conversation they saw before;
    when it is absent one is generated for this request alone.
    """
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
        McpPrincipal(user=current_user, workspace_ids=[workspace_id]),
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
                workspace_id=workspace_id,
                workspace_name=workspace.name,
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
