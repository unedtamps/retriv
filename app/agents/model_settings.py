"""Reading the harness's one chat-model setting and building its provider.

The setting is a singleton row an admin edits at runtime, so the provider is
built per request rather than at startup: a model the admin swaps in is used
by the next agent session, and an unconfigured deployment reports a clear
absence instead of failing somewhere inside a loop.

The provider choice also decides the extra headers: a preset that demands a
stable per-conversation id (OpenCode Go rejects requests without one) gets
it attached here, so the loop and the routes never handle provider quirks
themselves.
"""

import uuid as uuid_module
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.chat import OpenAICompatibleChatProvider
from app.agents.providers import session_header_for
from app.db.models import AgentModelSetting


@dataclass(frozen=True)
class AgentModelConfig:
    provider: str
    base_url: str
    api_key: str
    model: str


async def load_agent_model_config(session: AsyncSession) -> AgentModelConfig | None:
    row = await session.get(AgentModelSetting, 1)
    if row is None:
        return None
    return AgentModelConfig(
        provider=row.provider,
        base_url=row.base_url,
        api_key=row.api_key,
        model=row.model,
    )


def provider_for(
    config: AgentModelConfig, session_id: str | None = None
) -> OpenAICompatibleChatProvider:
    header = session_header_for(config.provider)
    return OpenAICompatibleChatProvider(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        session_header=header,
        session_id=session_id,
    )


def fresh_session_id() -> str:
    """A conversation id for callers that did not bring one."""
    return str(uuid_module.uuid4())


def key_preview(api_key: str) -> str:
    """The last four characters, so an admin can tell which key is set."""
    if len(api_key) < 4:
        return "…"
    return f"…{api_key[-4:]}"
