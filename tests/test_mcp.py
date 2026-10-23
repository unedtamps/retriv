"""The MCP endpoint: authentication, scope, and the four read tools.

The tests drive the real streamable-HTTP transport over ASGI rather than a
mocked server, because the two properties worth protecting here are
transport-level: an unauthenticated call must be refused before it reaches a
handler, and the workspace scope must follow the key of the individual
request rather than the session it happens to ride in.
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import ApiKey, Document, DocumentWorkspace
from app.db.session import get_session
from app.infrastructure.scanner import get_malware_scanner
from app.infrastructure.storage import get_object_storage
from app.main import create_app
from app.mcp.runtime import McpRuntime
from app.mcp.server import _instructions
from app.retrieval.context import ContextRetriever
from app.retrieval.navigation import DocumentNavigator
from app.retrieval.schemas import ContextSource
from app.retrieval.sql import (
    Catalogue,
    CatalogueEntry,
    CatalogueReader,
    RowQuery,
    RowQueryError,
    RowQueryResult,
    RowResult,
)
from app.security.api_keys import hash_api_key, mint_api_key
from tests.conftest import InMemoryObjectStorage, SeededEntities

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}

SEED_BYTES = b"name,amount\na,1\n"


class FakeRetriever(ContextRetriever):
    """Records the scope each search was given, returns what it is queued."""

    def __init__(self) -> None:
        self.calls: list[list[UUID]] = []
        self.budgets: list[int | None] = []
        self.results: list[list[ContextSource]] = []

    async def retrieve(
        self,
        query: str,
        workspace_ids: list[UUID],
        limit: int,
        *,
        budget: int | None = None,
    ) -> list[ContextSource]:
        del query, limit
        self.calls.append(list(workspace_ids))
        self.budgets.append(budget)
        return self.results.pop(0) if self.results else []


class FakeRowQuery(RowQuery):
    def __init__(self) -> None:
        self.runs: list[str] = []
        self.outcome = RowQueryResult(sources=[], values=[], columns=[])
        self.error: RowQueryError | None = None
        self.catalogue = Catalogue(entries=[], hidden=0)

    async def run(self, sql: str, *, workspace_ids: tuple[UUID, ...]) -> RowQueryResult:
        del workspace_ids
        self.runs.append(sql)
        if self.error is not None:
            raise self.error
        return self.outcome

    async def queryable_files(
        self,
        *,
        workspace_ids: tuple[UUID, ...],
        offset: int = 0,
        limit: int = 100,
        pattern: str | None = None,
    ) -> Catalogue:
        del workspace_ids, offset, limit, pattern
        return self.catalogue


class McpSession:
    """A raw JSON-RPC client, so a test can send any header it likes."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._session_id: str | None = None
        self._next_id = 0

    async def start(self, key: str) -> None:
        response = await self._post(key, INITIALIZE)
        assert response.status_code == 200, response.text
        self._session_id = response.headers.get("mcp-session-id")
        await self._client.post(
            "/mcp",
            headers=self._headers(key, include_session=False),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    async def post(self, key: str | None, payload: Any) -> httpx.Response:
        return await self._post(key, payload)

    async def request(
        self, key: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._next_id += 1
        response = await self._post(
            key,
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params},
        )
        return _payload(response)

    async def call(self, key: str, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self.request(
            key, "tools/call", {"name": name, "arguments": arguments}
        )
        assert "error" not in result, result["error"]
        return result["result"]

    def _headers(
        self, key: str | None, *, include_session: bool = True
    ) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if key is not None:
            headers["authorization"] = f"Bearer {key}"
        if include_session and self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _post(self, key: str | None, payload: Any) -> httpx.Response:
        return await self._client.post("/mcp", headers=self._headers(key), json=payload)


def _payload(response: httpx.Response) -> dict[str, Any]:
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no event data in {response.status_code} {response.text!r}")


def text_of(result: dict[str, Any]) -> str:
    return "\n".join(
        item.get("text", "")
        for item in result.get("content", [])
        if item.get("type") == "text"
    )


def make_source(workspace_id: UUID, *, text: str = "hello") -> ContextSource:
    return ContextSource(
        workspace_id=workspace_id,
        document_id=uuid4(),
        processing_run_id=uuid4(),
        chunk_id=uuid4(),
        ordinal=1,
        score=0.9,
        content_hash="",
        filename="notes.txt",
        section_path=[],
        page_numbers=[],
        text=text,
    )


def make_row(document_id: UUID, workspace_id: UUID) -> RowResult:
    return RowResult(
        chunk_id=uuid4(),
        workspace_id=workspace_id,
        document_id=document_id,
        processing_run_id=uuid4(),
        row_number=1,
        data={"name": "a"},
        columns=["name"],
    )


def make_entry(document_id: UUID, workspace_id: UUID, filename: str) -> CatalogueEntry:
    return CatalogueEntry(
        document_id=document_id,
        workspace_id=workspace_id,
        processing_run_id=uuid4(),
        filename=filename,
        row_count=10,
        columns=["name", "amount"],
    )


@dataclass
class McpEnv:
    session: McpSession
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    storage: InMemoryObjectStorage
    entities: SeededEntities
    retriever: FakeRetriever
    row_query: FakeRowQuery
    alice_key: str
    bob_key: str
    admin_key: str


@asynccontextmanager
async def running_mcp(
    settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    storage: InMemoryObjectStorage,
    scanner: Any,
    entities: SeededEntities,
) -> AsyncIterator[McpEnv]:
    application = create_app(settings)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_object_storage] = lambda: storage
    application.dependency_overrides[get_malware_scanner] = lambda: scanner

    retriever = FakeRetriever()
    row_query = FakeRowQuery()
    application.state.mcp.set(
        McpRuntime(
            settings=settings,
            session_factory=db_session_factory,
            retriever=retriever,
            navigator=DocumentNavigator(settings, db_session_factory),
            row_query=row_query,
            catalogue_reader=CatalogueReader(db_session_factory),
            storage=storage,
        )
    )

    # The session manager is run in a task of its own rather than with an
    # `async with` across the yield: pytest-asyncio drives the setup and the
    # teardown of an async fixture in two different tasks, and anyio refuses
    # to leave a cancel scope it entered in a different one. Enter and exit
    # therefore both happen inside `supervise`.
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def supervise() -> None:
        async with application.state.mcp_server.session_manager.run():
            if not started.done():
                started.set_result(None)
            await stop.wait()

    # httpx's ASGI transport never runs a lifespan, so the test drives the
    # manager that actually accepts requests itself.
    supervisor = asyncio.create_task(supervise())
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            await started
            async with db_session_factory() as database:
                alice = await mint_api_key(
                    database, user_id=entities.user.id, name="alice"
                )
                bob = await mint_api_key(
                    database, user_id=entities.second_user.id, name="bob"
                )
                admin = await mint_api_key(
                    database, user_id=entities.admin_user.id, name="admin"
                )
                await database.commit()
            mcp = McpSession(client)
            await mcp.start(alice.plaintext)
            yield McpEnv(
                session=mcp,
                settings=settings,
                session_factory=db_session_factory,
                storage=storage,
                entities=entities,
                retriever=retriever,
                row_query=row_query,
                alice_key=alice.plaintext,
                bob_key=bob.plaintext,
                admin_key=admin.plaintext,
            )
    finally:
        stop.set()
        await supervisor
        application.state.mcp.set(None)
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def mcp(
    settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    object_storage: InMemoryObjectStorage,
    scanner: Any,
    entities: SeededEntities,
) -> AsyncIterator[McpEnv]:
    mcp_settings = settings.model_copy(
        update={
            "sql_database_url": "postgresql+asyncpg://retriv:test@localhost/retriv",
        }
    )
    async with running_mcp(
        mcp_settings, db_session_factory, object_storage, scanner, entities
    ) as env:
        yield env


async def test_the_endpoint_refuses_to_start_before_the_runtime_exists(
    settings: Settings,
) -> None:
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/mcp", json=INITIALIZE)
    assert response.status_code == 503
    assert response.json()["code"] == "mcp_not_ready"
    application.dependency_overrides.clear()


async def test_the_endpoint_requires_an_api_key(mcp: McpEnv) -> None:
    response = await mcp.session.post(None, INITIALIZE)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "invalid_credentials"


async def test_the_endpoint_rejects_a_key_it_does_not_know(mcp: McpEnv) -> None:
    response = await mcp.session.post("rk_" + "a" * 43, INITIALIZE)
    assert response.status_code == 401


async def test_the_endpoint_rejects_anything_but_a_bearer_token(mcp: McpEnv) -> None:
    response = await mcp.session._client.post(
        "/mcp",
        headers={
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "authorization": f"Basic {mcp.alice_key}",
        },
        json=INITIALIZE,
    )
    assert response.status_code == 401


async def test_a_revoked_key_stops_working(
    mcp: McpEnv, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with db_session_factory() as database:
        record = await database.scalar(
            select(ApiKey).where(ApiKey.key_hash == hash_api_key(mcp.alice_key))
        )
        assert record is not None
        record.revoked_at = record.created_at
        await database.commit()

    response = await mcp.session.post(mcp.alice_key, INITIALIZE)
    assert response.status_code == 401


async def test_search_context_returns_labelled_sources(mcp: McpEnv) -> None:
    mcp.retriever.results = [[make_source(mcp.entities.finance.id, text="net revenue")]]
    result = await mcp.session.call(mcp.alice_key, "search_context", query="revenue")
    assert result["isError"] is False
    assert "<untrusted_document_text>" in text_of(result)
    assert "net revenue" in text_of(result)


async def test_search_context_grants_room_for_section_completion(mcp: McpEnv) -> None:
    """The tool must pass a budget, or section completion can never run.

    ``retrieve`` collapses its cap onto ``limit`` when no budget is given, so a search
    that filled its quota had nowhere to put a completed section. Every recorded
    baseline showed zero completed-only hits for exactly this reason. The tool, not
    the retriever default, is where the room has to come from.
    """
    await mcp.session.call(mcp.alice_key, "search_context", query="revenue")

    assert mcp.retriever.budgets == [mcp.settings.max_context_sources]
    assert mcp.settings.max_context_sources > mcp.settings.max_context_chunks


async def test_search_context_labels_each_source_with_its_section(mcp: McpEnv) -> None:
    """The model is told which section a passage came from, not just which page."""
    source = make_source(mcp.entities.finance.id, text="net revenue")
    object.__setattr__(source, "section_path", ["PMK", "Pasal 16"])
    mcp.retriever.results = [[source]]

    result = await mcp.session.call(mcp.alice_key, "search_context", query="revenue")

    assert result["isError"] is False
    body = text_of(result)
    assert 'section=["PMK", "Pasal 16"]' in body


async def test_search_context_marks_a_bare_heading_as_such(mcp: McpEnv) -> None:
    """A source that reads like a title says so, so it is not read as an empty answer."""
    source = make_source(mcp.entities.finance.id, text="Pasal 16")
    object.__setattr__(source, "section_path", ["PMK", "Pasal 16"])
    object.__setattr__(source, "is_heading_only", True)
    mcp.retriever.results = [[source]]

    result = await mcp.session.call(mcp.alice_key, "search_context", query="pasal 16")

    assert "kind=heading" in text_of(result)


async def test_search_context_omits_section_for_sources_without_one(mcp: McpEnv) -> None:
    """CSV rows have no section; the label must not claim an empty breadcrumb."""
    mcp.retriever.results = [[make_source(mcp.entities.finance.id, text="a,b,c")]]

    result = await mcp.session.call(mcp.alice_key, "search_context", query="revenue")

    assert "section=" not in text_of(result)


async def test_search_context_scope_is_derived_from_the_key(mcp: McpEnv) -> None:
    await mcp.session.call(mcp.alice_key, "search_context", query="revenue")
    await mcp.session.call(mcp.bob_key, "search_context", query="revenue")
    assert [set(call) for call in mcp.retriever.calls] == [
        {mcp.entities.finance.id, mcp.entities.legal.id},
        {mcp.entities.legal.id},
    ]


async def test_search_context_scope_follows_each_request(mcp: McpEnv) -> None:
    # One session, three calls, two keys: the later calls must not inherit a
    # workspace set from the key that happened to open the session.
    await mcp.session.call(mcp.alice_key, "search_context", query="revenue")
    await mcp.session.call(mcp.bob_key, "search_context", query="revenue")
    await mcp.session.call(mcp.alice_key, "search_context", query="revenue")
    assert len(mcp.retriever.calls) == 3
    assert set(mcp.retriever.calls[2]) == {
        mcp.entities.finance.id,
        mcp.entities.legal.id,
    }


async def test_search_context_narrows_to_one_workspace(mcp: McpEnv) -> None:
    await mcp.session.call(
        mcp.alice_key,
        "search_context",
        query="revenue",
        workspace_id=str(mcp.entities.legal.id),
    )
    assert mcp.retriever.calls == [[mcp.entities.legal.id]]


async def test_search_context_refuses_a_workspace_outside_the_key(mcp: McpEnv) -> None:
    # Bob can see legal, not finance. Naming finance must not widen his scope.
    result = await mcp.session.call(
        mcp.bob_key,
        "search_context",
        query="revenue",
        workspace_id=str(mcp.entities.finance.id),
    )
    assert result["isError"] is True
    assert "not in scope" in text_of(result)
    assert mcp.retriever.calls == []


async def test_search_context_reports_an_empty_scope(mcp: McpEnv) -> None:
    result = await mcp.session.call(mcp.admin_key, "search_context", query="revenue")
    assert result["isError"] is False
    assert "No searchable workspace" in text_of(result)
    assert mcp.retriever.calls == []


async def test_tools_are_the_documented_six(mcp: McpEnv) -> None:
    listed = await mcp.session.request(mcp.alice_key, "tools/list", {})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {
        "search_context",
        "read_document",
        "query_rows",
        "list_queryable_files",
        "download_document",
        "find_documents",
    }


async def test_query_rows_states_the_jsonb_shape_before_any_failure(
    mcp: McpEnv,
) -> None:
    """The description has to carry the contract the catalogue cannot.

    On a live run the model wrote CSV headers as if they were SQL columns and
    had every statement rejected. The description named list_queryable_files,
    which supplied the key names, but never said where those keys live, so the
    first query was wrong every time. Naming the JSONB column up front is what
    makes the first attempt correct instead of the ninth.
    """
    listed = await mcp.session.request(mcp.alice_key, "tools/list", {})
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    description = tools["query_rows"]["description"]
    assert "JSONB" in description
    assert "document_rows" in description
    # The shape, not just the vocabulary: a model that is told the keys are
    # case-sensitive and must be cast has enough to write the statement.
    assert "(data->>'Amount')::numeric" in description


def test_the_csv_note_appears_only_when_the_row_tool_does() -> None:
    """Instructions already vary by deployment, and the note has to vary too.

    ``_instructions`` warns that pointing a model at a tool the server does
    not have sends it looking for an answer it will never get. A deployment
    without the reader has no query_rows, so describing the JSONB column
    would be describing a table no tool can read.
    """
    assert "JSONB" in _instructions(row_queries=True)
    assert "JSONB" not in _instructions(row_queries=False)


async def test_no_tool_writes_to_a_workspace(mcp: McpEnv) -> None:
    # Upload, status and deletion are decisions a person takes in the
    # dashboard. Keeping them out of the MCP surface is what keeps a leaked
    # API key read-only, so the shape of the tool list is itself a security
    # property and not just a count.
    listed = await mcp.session.request(mcp.alice_key, "tools/list", {})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert not names & {"upload_document", "delete_document", "get_document_status"}


async def test_list_queryable_files_shows_ids_and_columns(mcp: McpEnv) -> None:
    document_id = uuid4()
    mcp.row_query.catalogue = Catalogue(
        entries=[make_entry(document_id, mcp.entities.legal.id, "invoices.csv")],
        hidden=0,
    )
    result = await mcp.session.call(mcp.bob_key, "list_queryable_files")
    body = text_of(result)
    assert "invoices.csv" in body
    assert str(document_id) in body
    assert "name, amount" in body


async def test_query_rows_requires_a_document_id(mcp: McpEnv) -> None:
    result = await mcp.session.call(
        mcp.bob_key, "query_rows", sql="select count(*) from rows"
    )
    assert result["isError"] is True
    assert "does not name a document_id" in text_of(result)
    assert mcp.row_query.runs == []


async def test_query_rows_returns_an_aggregate_as_a_value(mcp: McpEnv) -> None:
    document_id = uuid4()
    mcp.row_query.catalogue = Catalogue(
        entries=[make_entry(document_id, mcp.entities.legal.id, "invoices.csv")],
        hidden=0,
    )
    mcp.row_query.outcome = RowQueryResult(
        sources=[], values=[{"count": 12}], columns=["count"]
    )
    result = await mcp.session.call(
        mcp.bob_key,
        "query_rows",
        sql=f"select count(*) from rows where document_id = '{document_id}'",
    )
    assert result["isError"] is False
    assert "Result (computed, not citable)" in text_of(result)
    assert "12" in text_of(result)


async def test_query_rows_surfaces_a_rejection(mcp: McpEnv) -> None:
    mcp.row_query.error = RowQueryError("relation does not exist")
    result = await mcp.session.call(
        mcp.bob_key, "query_rows", sql="select 1 where document_id = 'x'"
    )
    assert result["isError"] is True
    assert "The query was rejected: relation does not exist" in text_of(result)


async def test_find_documents_matches_a_name_and_reports_status(
    mcp: McpEnv,
) -> None:
    await _seed_document(mcp, filename="UNDP Financial Regulations.pdf")
    await _seed_document(mcp, filename="Procurement Manual.pdf")
    result = await mcp.session.call(
        mcp.alice_key, "find_documents", pattern="financial"
    )
    assert result["isError"] is False
    body = text_of(result)
    assert "UNDP Financial Regulations.pdf" in body
    assert "Procurement Manual.pdf" not in body
    # The detail an agent needs before acting: the id to pass on, and whether
    # searching it will find anything yet.
    assert "status=ready" in body
    assert "workspace_id=" in body
    assert "bytes" in body


async def test_find_documents_shows_that_a_csv_is_queryable(
    mcp: McpEnv,
) -> None:
    await _seed_document(mcp, filename="ledger.csv")
    result = await mcp.session.call(mcp.alice_key, "find_documents")
    body = text_of(result)
    assert "queryable with query_rows" in body
    assert "0 rows" in body


async def test_find_documents_keeps_two_revisions_of_one_name_apart(
    mcp: McpEnv,
) -> None:
    """Filenames are not unique -- only content is, per workspace.

    Two revisions of a document therefore both appear, and without the size
    and date beside the name they would be two indistinguishable lines.
    """
    old = await _seed_document(mcp, filename="Peraturan.pdf")
    new = await _seed_document(mcp, filename="Peraturan.pdf")
    result = await mcp.session.call(
        mcp.alice_key, "find_documents", pattern="Peraturan.pdf"
    )
    body = text_of(result)
    listed = [line for line in body.splitlines() if line.startswith("  [")]
    assert len(listed) == 2
    assert str(old.document_id) in body
    assert str(new.document_id) in body
    # Each line carries the details that tell the two apart.
    assert all("workspace_id=" in line for line in listed)
    assert all(f"{len(SEED_BYTES)} bytes" in line for line in listed)


async def test_find_documents_ranks_the_exact_name_first(
    mcp: McpEnv,
) -> None:
    await _seed_document(mcp, filename="Peraturan 2020.pdf")
    exact = await _seed_document(mcp, filename="Peraturan")
    await _seed_document(mcp, filename="Peraturan tambahan.pdf")
    result = await mcp.session.call(
        mcp.alice_key, "find_documents", pattern="Peraturan"
    )
    listed = [
        line for line in text_of(result).splitlines() if line.startswith("  [")
    ]
    assert str(exact.document_id) in listed[0]
    assert all(str(exact.document_id) not in line for line in listed[1:])


async def test_find_documents_without_a_pattern_lists_everything(
    mcp: McpEnv,
) -> None:
    await _seed_document(mcp, filename="one.pdf")
    await _seed_document(mcp, filename="two.csv")
    result = await mcp.session.call(mcp.alice_key, "find_documents")
    body = text_of(result)
    assert "one.pdf" in body
    assert "two.csv" in body
    # It says what to do with an id, which is the reason to call this at all.
    assert "download_document" in body


async def test_find_documents_says_so_when_nothing_matches(
    mcp: McpEnv,
) -> None:
    await _seed_document(mcp, filename="one.pdf")
    result = await mcp.session.call(
        mcp.alice_key, "find_documents", pattern="nonexistent"
    )
    assert result["isError"] is False
    body = text_of(result)
    assert "No document whose name contains" in body
    assert "one.pdf" not in body


async def test_find_documents_does_not_leave_the_key_scope(
    mcp: McpEnv,
) -> None:
    """The admin key holds no membership, so it sees nothing at all.

    A name that exists somewhere must not be confirmable from outside scope.
    """
    await _seed_document(mcp, filename="secret.pdf")
    result = await mcp.session.call(
        mcp.admin_key, "find_documents", pattern="secret"
    )
    assert result["isError"] is False
    body = text_of(result)
    assert "No searchable workspace" in body
    assert "secret.pdf" not in body


async def test_find_documents_narrows_to_one_workspace(
    mcp: McpEnv,
) -> None:
    await _seed_document(mcp, filename="legal-only.pdf", workspace="legal")
    await _seed_document(mcp, filename="finance-only.pdf", workspace="finance")
    result = await mcp.session.call(
        mcp.alice_key,
        "find_documents",
        pattern="only",
        workspace_id=str(mcp.entities.legal.id),
    )
    body = text_of(result)
    assert "legal-only.pdf" in body
    assert "finance-only.pdf" not in body


async def test_find_documents_refuses_a_workspace_outside_the_key(
    mcp: McpEnv,
) -> None:
    await _seed_document(mcp, filename="ledger.csv", workspace="finance")
    result = await mcp.session.call(
        mcp.admin_key,
        "find_documents",
        workspace_id=str(mcp.entities.finance.id),
    )
    assert result["isError"] is True
    assert "not in scope" in text_of(result)


async def test_find_documents_treats_a_wildcard_as_a_literal(
    mcp: McpEnv,
) -> None:
    """A search term carrying `%` must not turn into "everything".

    The term is a bind parameter so it cannot inject SQL, but without
    escaping it would still match every document in the workspace and answer
    a question nobody asked.
    """
    await _seed_document(mcp, filename="report.pdf")
    result = await mcp.session.call(mcp.alice_key, "find_documents", pattern="%")
    assert result["isError"] is False
    assert "report.pdf" not in text_of(result)


async def test_find_documents_reports_when_the_page_is_truncated(
    mcp: McpEnv,
) -> None:
    for index in range(3):
        await _seed_document(mcp, filename=f"many-{index}.pdf")
    result = await mcp.session.call(
        mcp.alice_key, "find_documents", pattern="many", limit=1
    )
    body = text_of(result)
    assert "many-0.pdf" in body or "many-1.pdf" in body or "many-2.pdf" in body
    assert "and 2 more" in body

async def test_download_document_returns_a_time_limited_signed_link(
    mcp: McpEnv,
) -> None:
    seeded = await _seed_document(mcp, filename="ledger.csv")
    result = await mcp.session.call(
        mcp.alice_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    assert result["isError"] is False
    body = text_of(result)
    # The link is the whole point: it points at the object store and carries
    # its own expiry, so the bytes never come back through this server.
    assert f"link=https://objects.example.test/raw-documents/{seeded.object_key}" in body
    assert "X-Amz-Signature=" in body
    assert "X-Amz-Expires=300" in body
    assert "link_expires_in_seconds=300" in body
    # It really was signed for the configured window, not just printed as one.
    assert mcp.storage.presigned == [
        ("raw-documents", seeded.object_key, 300)
    ]


async def test_the_link_window_is_the_configured_one(
    mcp: McpEnv,
) -> None:
    mcp.settings.mcp_download_link_ttl_seconds = 45
    seeded = await _seed_document(mcp, filename="ledger.csv")
    result = await mcp.session.call(
        mcp.alice_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    assert "link_expires_in_seconds=45" in text_of(result)
    assert mcp.storage.presigned[-1][2] == 45


async def test_download_document_returns_the_api_url_and_metadata(
    mcp: McpEnv,
) -> None:
    seeded = await _seed_document(mcp, filename="ledger.csv")
    result = await mcp.session.call(
        mcp.alice_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    body = text_of(result)
    # Relative on purpose: the agent already knows the host it talks to, and a
    # base URL reconstructed here would be wrong behind a proxy.
    assert (
        f"url=/v1/workspaces/{mcp.entities.legal.id}"
        f"/documents/{seeded.document_id}/download" in body
    )
    assert "filename=ledger.csv" in body
    assert "media_type=text/csv" in body
    assert "size_bytes=16" in body


async def test_download_document_returns_no_bytes(mcp: McpEnv) -> None:
    # Neither address may carry the file. A tool result is what lands in the
    # model's context, and a 566 KB PDF would be roughly 189k tokens there.
    seeded = await _seed_document(mcp, filename="ledger.csv")
    result = await mcp.session.call(
        mcp.alice_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    body = text_of(result)
    assert base64.b64encode(SEED_BYTES).decode() not in body
    # The object key may appear, but only as part of an address.
    assert body.count("name,amount") == 0


async def test_download_document_falls_back_to_the_url_when_signing_fails(
    mcp: McpEnv,
) -> None:
    """A store that cannot sign must not cost the caller the file.

    The API route does not depend on signing, so the tool reports the route
    instead of failing outright.
    """
    seeded = await _seed_document(mcp, filename="ledger.csv")
    mcp.storage.presign_error = True
    result = await mcp.session.call(
        mcp.alice_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    assert result["isError"] is True
    assert f"/v1/workspaces/{mcp.entities.legal.id}" in text_of(result)
    assert "signed" in text_of(result)


async def test_download_document_refuses_a_workspace_without_download_rights(
    mcp: McpEnv,
) -> None:
    seeded = await _seed_document(mcp, filename="ledger.csv")
    # Bob is a viewer on legal: read and search yes, download no.
    result = await mcp.session.call(
        mcp.bob_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    assert result["isError"] is True
    assert "permission" in text_of(result).lower()
    # Nothing was minted: the refusal happens before either address exists.
    assert mcp.storage.presigned == []


async def test_download_document_refuses_a_workspace_outside_the_key(
    mcp: McpEnv,
) -> None:
    seeded = await _seed_document(mcp, filename="ledger.csv")
    # The admin key carries no workspace membership at all, so naming a
    # workspace it does not hold is refused with the same "not found or not
    # accessible" the HTTP layer uses, rather than a message that would confirm
    # the workspace exists.
    result = await mcp.session.call(
        mcp.admin_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    assert result["isError"] is True
    assert "does not exist or is not accessible" in text_of(result)
    assert mcp.storage.presigned == []


async def test_download_document_refuses_a_document_being_deleted(
    mcp: McpEnv,
) -> None:
    seeded = await _seed_document(mcp, filename="ledger.csv")
    async with mcp.session_factory() as session:
        document = await session.get(Document, seeded.document_id)
        assert document is not None
        document.deleted_at = datetime.now(UTC)
        await session.commit()

    result = await mcp.session.call(
        mcp.alice_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    assert result["isError"] is True
    assert "being deleted" in text_of(result)
    assert mcp.storage.presigned == []


async def test_download_document_refuses_an_unknown_document(mcp: McpEnv) -> None:
    result = await mcp.session.call(
        mcp.alice_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(uuid4()),
    )
    assert result["isError"] is True
    assert "No such document" in text_of(result)


async def test_download_document_reports_a_document_whose_object_is_gone(
    mcp: McpEnv,
) -> None:
    """Metadata can outlive the object it points at; that must be a message."""
    seeded = await _seed_document(mcp, filename="ledger.csv")
    mcp.storage.objects.pop(("raw-documents", seeded.object_key))
    result = await mcp.session.call(
        mcp.alice_key,
        "download_document",
        workspace_id=str(mcp.entities.legal.id),
        document_id=str(seeded.document_id),
    )
    assert result["isError"] is True
    assert "signed" in text_of(result)

async def test_the_row_tool_is_absent_when_no_reader_is_configured(
    settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    object_storage: InMemoryObjectStorage,
    scanner: Any,
    entities: SeededEntities,
) -> None:
    # The development .env supplies a reader DSN, so it is turned off here on
    # purpose: the absence of the tool has to come from configuration.
    no_reader = settings.model_copy(update={"sql_database_url": None})
    async with running_mcp(
        no_reader, db_session_factory, object_storage, scanner, entities
    ) as env:
        listed = await env.session.request(env.alice_key, "tools/list", {})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        listing = await env.session.call(env.alice_key, "list_queryable_files")
    assert "query_rows" not in names
    assert names == {
        "search_context",
        "read_document",
        "list_queryable_files",
        "download_document",
        "find_documents",
    }
    assert listing["isError"] is False


@dataclass(frozen=True)
class SeededDocument:
    document_id: UUID
    object_key: str


async def _seed_document(
    mcp: McpEnv, *, filename: str, workspace: str = "legal"
) -> SeededDocument:
    """A document row and its stored object, as the upload path leaves both."""
    workspace_id = getattr(mcp.entities, workspace).id
    object_key = f"raw-documents/{workspace_id}/{uuid4()}"
    async with mcp.session_factory() as session:
        document = Document(
            origin_workspace_id=workspace_id,
            original_filename=filename,
            detected_media_type=(
                "text/csv" if filename.lower().endswith(".csv") else "application/pdf"
            ),
            size_bytes=len(SEED_BYTES),
            sha256=uuid4().hex * 2,
            bucket="raw-documents",
            object_key=object_key,
            created_by_user_id=mcp.entities.user.id,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentWorkspace(
                workspace_id=workspace_id,
                document_id=document.id,
                filename=filename,
                created_by_user_id=mcp.entities.user.id,
            )
        )
        await session.commit()
        await session.refresh(document)
        document_id = document.id
    mcp.storage.objects[("raw-documents", object_key)] = SEED_BYTES
    return SeededDocument(document_id=document_id, object_key=object_key)