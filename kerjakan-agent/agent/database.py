import ssl
from typing import Any

from agent.config import Settings


def database_connect_args(settings: Settings) -> dict[str, Any]:
    if settings.environment == "production":
        return {"ssl": ssl.create_default_context()}
    return {}
