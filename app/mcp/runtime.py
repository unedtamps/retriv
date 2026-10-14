"""The process-wide objects the MCP tools need.

The tools do not run inside a FastAPI request, so they cannot reach the
container through ``Depends``. Everything they need is assembled once by the
application lifespan and handed over through a box the tool closures hold.

The box also gives the tests a seam: the ASGI test transport does not run
lifespans, so a test fills the box itself instead of waiting for startup.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.retrieval.context import ContextRetriever
from app.retrieval.sql import CatalogueReader, RowQuery

if TYPE_CHECKING:
    from app.infrastructure.storage import ObjectStorage


class McpNotReadyError(RuntimeError):
    """The server was asked for work before the application finished starting."""


@dataclass
class McpRuntime:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    retriever: ContextRetriever
    row_query: RowQuery | None
    catalogue_reader: CatalogueReader
    # Only for signing download links. The bytes themselves never travel
    # through a tool: the link points at the object store.
    storage: "ObjectStorage"


class McpRuntimeBox:
    """Holds the runtime, unset until startup completes."""

    def __init__(self) -> None:
        self._runtime: McpRuntime | None = None

    def set(self, runtime: McpRuntime | None) -> None:
        self._runtime = runtime

    @property
    def runtime(self) -> McpRuntime:
        if self._runtime is None:
            raise McpNotReadyError("The MCP server is not accepting calls yet")
        return self._runtime
