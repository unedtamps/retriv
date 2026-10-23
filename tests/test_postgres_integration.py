"""PostgreSQL-only behaviour that SQLite cannot show.

The important one is concurrent identical uploads. SQLite serialises
writers so it can never reproduce a race, and the global unique constraint
on sha256 is what actually settles it in production.

Skipped unless TEST_POSTGRES_URL points at a real database.
"""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.dependencies import get_malware_scanner
from app.core.config import Settings, get_settings
from app.db.models import (
    JOB_EXTRACT,
    RUN_CHUNKING,
    RUN_EMBEDDING,
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentRow,
    DocumentWorkspace,
    ProcessingRun,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.db.session import get_session
from app.infrastructure.storage import (
    DownloadResult,
    ObjectTooLargeError,
    get_object_storage,
)
from app.main import create_app
from app.processing.worker import ProcessingWorker
from app.retrieval.sql import RowQuery, RowQueryError
from app.security.auth import hash_password
from tests.conftest import CleanScanner, InMemoryObjectStorage
from tests.test_worker import (
    FakeEmbeddingProvider,
    FakeExtractor,
    FakeStorage,
    FakeVectorStore,
)

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None, reason="TEST_POSTGRES_URL is not configured"
)


def database_url() -> str:
    assert POSTGRES_URL is not None
    return POSTGRES_URL


async def clear_database(session: AsyncSession) -> None:
    for model in (
        DocumentWorkspace,
        ChunkEmbedding,
        DocumentRow,
        DocumentChunk,
        ProcessingRun,
        Document,
        WorkspaceMembership,
        Workspace,
        User,
    ):
        await session.execute(delete(model))
    await session.commit()


async def seed_owner(
    session: AsyncSession, *, slug: str = "finance", username: str = "alice"
) -> tuple[UUID, dict[str, str]]:
    user = User(
        username=username, password_hash=hash_password("pw-12345678"), role="user"
    )
    workspace = Workspace(slug=slug, name=slug.title())
    session.add_all([user, workspace])
    await session.flush()
    session.add(
        WorkspaceMembership(
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
            granted_by_user_id=user.id,
        )
    )
    await session.commit()
    del username, slug
    return workspace.id, {"username": user.username, "password": "pw-12345678"}


async def test_concurrent_identical_uploads_converge_in_postgresql() -> None:
    """Two identical uploads race on the unique (workspace_id, sha256).

    One wins and returns 202; the other must not create a second document
    or a second processing run. This is what the outbox used to paper
    over; now a single constraint has to do it.
    """
    from tests.document_samples import pdf_bytes

    engine = create_async_engine(database_url(), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings().model_copy(
        update={
            "environment": "test",
            "database_url": database_url(),
            "antivirus_enabled": False,
        }
    )
    application = create_app(settings)
    storage = InMemoryObjectStorage()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_object_storage] = lambda: storage
    application.dependency_overrides[get_malware_scanner] = lambda: CleanScanner()
    try:
        async with factory() as session:
            await clear_database(session)
            workspace_id, credentials = await seed_owner(session)

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            token = await client.post(
                "/v1/auth/token",
                data=credentials,
            )
            assert token.status_code == 200
            headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
            content = pdf_bytes()
            responses = await asyncio.gather(
                *(
                    client.post(
                        f"/v1/workspaces/{workspace_id}/documents",
                        headers=headers,
                        files={"file": ("report.pdf", content, "application/pdf")},
                    )
                    for _ in range(2)
                )
            )

        assert sorted(response.status_code for response in responses) == [200, 202], [
            response.text for response in responses
        ]
        assert sorted(
            response.json().get("duplicate", False) for response in responses
        ) == [False, True]

        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Document)) == 1
            assert (
                await session.scalar(select(func.count()).select_from(ProcessingRun))
                == 1
            )
    finally:
        application.dependency_overrides.clear()
        async with factory() as session:
            await clear_database(session)
        await engine.dispose()


async def test_chunk_and_embedding_rows_commit_in_foreign_key_order(
    settings: Settings,
) -> None:
    """The parent rows have to be flushed before the children.

    There is no ORM relationship between the pipeline tables, so the
    ordering is the worker's responsibility.
    """
    engine = create_async_engine(database_url(), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_settings = settings.model_copy(update={"database_url": database_url()})
    extractor = FakeExtractor()
    worker = ProcessingWorker(
        factory,
        FakeStorage(),
        extractor,
        worker_settings,
        FakeEmbeddingProvider(),
        FakeVectorStore(),
    )
    run_id, lease_token = uuid4(), uuid4()
    try:
        async with factory() as session:
            await clear_database(session)
            user = User(
                username="chunking", password_hash=hash_password("pw-12345678")
            )
            workspace = Workspace(slug="chunking", name="Chunking")
            session.add_all([user, workspace])
            await session.flush()
            document = Document(
                id=uuid4(),
                origin_workspace_id=workspace.id,
                original_filename="report.pdf",
                detected_media_type="application/pdf",
                size_bytes=1024,
                sha256="a" * 64,
                bucket="raw-documents",
                object_key=f"workspaces/{workspace.id}/objects/{uuid4()}",
            )
            session.add(document)
            await session.flush()
            session.add(
                DocumentWorkspace(
                    workspace_id=document.origin_workspace_id,
                    document_id=document.id,
                    filename=document.original_filename,
                )
            )
            session.add(
                ProcessingRun(
                    id=run_id,
                    origin_workspace_id=workspace.id,
                    document_id=document.id,
                    job_kind=JOB_EXTRACT,
                    pipeline_version=worker_settings.pipeline_version,
                    status=RUN_CHUNKING,
                    lease_token=lease_token,
                )
            )
            document.active_processing_run_id = run_id
            await session.commit()

        persisted = await worker._persist_chunks(
            run_id,
            lease_token,
            document,
            extractor.extract(Path("unused.pdf"), media_type="application/pdf"),
            artifact_key="artifact.json.gz",
            manifest_key="manifest.json",
        )

        async with factory() as session:
            run = await session.get(ProcessingRun, run_id)
            chunks = (
                await session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.processing_run_id == run_id
                    )
                )
            ).all()
        assert persisted is True
        assert run is not None
        assert run.status == RUN_EMBEDDING
        assert [chunk.ordinal for chunk in chunks] == sorted(
            chunk.ordinal for chunk in chunks
        )
    finally:
        async with factory() as session:
            await clear_database(session)
        await engine.dispose()


# -- Row Level Security ---------------------------------------------------
#
# Isolation of CSV rows cannot be shown on SQLite, which has no RLS and where
# every connection is the owner. These run as a purpose-built reader role
# because that is the only thing RLS constrains: the application role is a
# superuser carrying BYPASSRLS and sails straight past the policy, which is
# exactly why the SQL executor refuses such a connection rather than trusting
# its DSN.


def reader_url(url: str, role: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{role}:rls-probe@{parts.hostname}:{parts.port}"))


def scope_literal(*workspace_ids: UUID) -> str:
    """Postgres array literal for the RLS setting.

    The policy casts to ``uuid[]``, so the value has to be an array literal
    rather than a bare uuid. Every workspace is rendered from a parsed UUID,
    never from model output, so the braces cannot be injected into.
    """
    return "{" + ",".join(str(workspace_id) for workspace_id in workspace_ids) + "}"


async def test_rls_scopes_csv_rows_to_the_reader_workspace() -> None:
    engine = create_async_engine(database_url())
    role = "rls_probe_reader"
    reader: AsyncEngine | None = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
            await conn.execute(text(f"create role {role} login password 'rls-probe'"))
            await conn.execute(text(f"grant usage on schema public to {role}"))
            await conn.execute(
                text(
                    "grant select on document_rows, documents, "
                    f"document_workspaces, processing_runs to {role}"
                )
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await clear_database(session)
            first, _ = await seed_owner(session, slug="alpha", username="alice")
            second, _ = await seed_owner(session, slug="beta", username="bob")
            document = Document(
                origin_workspace_id=first,
                original_filename="s.csv",
                detected_media_type="text/csv",
                size_bytes=10,
                sha256="a" * 64,
                bucket="raw",
                object_key="k",
            )
            # Flush the document before reading its id: the default is a
            # Python-side uuid4, so it is not populated until insert time.
            session.add(document)
            await session.flush()
            session.add(
                DocumentWorkspace(
                    workspace_id=document.origin_workspace_id,
                    document_id=document.id,
                    filename=document.original_filename,
                )
            )
            run = ProcessingRun(
                document_id=document.id,
                origin_workspace_id=first,
                job_kind="extract",
                pipeline_version="2026.1",
                status="ready",
            )
            session.add(run)
            other_document = Document(
                origin_workspace_id=second,
                original_filename="other.csv",
                detected_media_type="text/csv",
                size_bytes=10,
                sha256="e" * 64,
                bucket="raw",
                object_key="other",
            )
            session.add(other_document)
            await session.flush()
            session.add(
                DocumentWorkspace(
                    workspace_id=second,
                    document_id=other_document.id,
                    filename=other_document.original_filename,
                )
            )
            other_run = ProcessingRun(
                document_id=other_document.id,
                origin_workspace_id=second,
                job_kind="extract",
                pipeline_version="2026.1",
                status="ready",
            )
            session.add(other_run)
            await session.flush()
            session.add_all(
                [
                    DocumentRow(
                        origin_workspace_id=first,
                        document_id=document.id,
                        processing_run_id=run.id,
                        row_number=number,
                        data={"status": "OPEN"},
                    )
                    for number in (1, 2, 3)
                ]
                + [
                    DocumentRow(
                        origin_workspace_id=second,
                        document_id=other_document.id,
                        processing_run_id=other_run.id,
                        row_number=number,
                        data={"status": "OPEN"},
                    )
                    for number in (4, 5)
                ]
            )
            await session.commit()

        reader = create_async_engine(reader_url(database_url(), role))

        async def visible_rows(scope: str | None) -> int:
            async with reader.connect() as conn:
                await conn.execute(text("begin"))
                if scope is not None:
                    await conn.execute(
                        text("select set_config('retriv.workspace_scope', :s, true)"),
                        {"s": scope},
                    )
                count = await conn.scalar(text("select count(*) from document_rows"))
                await conn.execute(text("commit"))
                return int(count or 0)

        # An absent scope must hide everything rather than raise. That is why
        # the policy reads current_setting with missing_ok: without it the
        # expression throws, and because RLS applies the same expression to
        # INSERT as a WITH CHECK, the worker's own row insert would fail.
        assert await visible_rows(None) == 0
        assert await visible_rows(scope_literal(first)) == 3
        assert await visible_rows(scope_literal(second)) == 2
        assert await visible_rows(scope_literal(first, second)) == 5
        assert await visible_rows("{}") == 0
    finally:
        if reader is not None:
            await reader.dispose()
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    f"revoke all on document_rows, documents, document_workspaces, processing_runs "
                    f"from {role}"
                )
            )
            await conn.execute(text(f"revoke usage on schema public from {role}"))
            await conn.commit()
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await clear_database(session)
        await engine.dispose()


async def test_the_reader_role_cannot_write_or_reach_other_tables() -> None:
    """Least privilege is enforced by the grant, not by parsing the SQL."""
    engine = create_async_engine(database_url())
    role = "rls_probe_writer"
    reader: AsyncEngine | None = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
            await conn.execute(text(f"create role {role} login password 'rls-probe'"))
            await conn.execute(text(f"grant usage on schema public to {role}"))
            await conn.execute(
                text(
                    "grant select on document_rows, documents, "
                    f"document_workspaces, processing_runs to {role}"
                )
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await clear_database(session)
            workspace, _ = await seed_owner(session, slug="gamma", username="carol")
            document = Document(
                origin_workspace_id=workspace,
                original_filename="s.csv",
                detected_media_type="text/csv",
                size_bytes=10,
                sha256="b" * 64,
                bucket="raw",
                object_key="k",
            )
            # Flush the document before reading its id: the default is a
            # Python-side uuid4, so it is not populated until insert time.
            session.add(document)
            await session.flush()
            session.add(
                DocumentWorkspace(
                    workspace_id=document.origin_workspace_id,
                    document_id=document.id,
                    filename=document.original_filename,
                )
            )
            run = ProcessingRun(
                document_id=document.id,
                origin_workspace_id=workspace,
                job_kind="extract",
                pipeline_version="2026.1",
                status="ready",
            )
            session.add(run)
            await session.flush()
            # Only a run the worker has activated carries this, and the
            # catalogue joins through it.
            document.active_processing_run_id = run.id
            session.add(
                DocumentRow(
                    origin_workspace_id=workspace,
                    document_id=document.id,
                    processing_run_id=run.id,
                    row_number=1,
                    data={"status": "OPEN"},
                )
            )
            await session.commit()

        reader = create_async_engine(reader_url(database_url(), role))
        statements = [
            "insert into document_rows (id, origin_workspace_id, document_id, "
            "processing_run_id, row_number, data, created_at) values "
            "(gen_random_uuid(), null, null, null, 1, '{}', now())",
            "delete from document_rows",
            "update document_rows set row_number = 2",
            "select count(*) from workspaces",
            "select count(*) from document_chunks",
            "select count(*) from pg_shadow",
        ]
        for statement in statements:
            async with reader.connect() as conn:
                await conn.execute(text("begin"))
                await conn.execute(
                    text("select set_config('retriv.workspace_scope', :s, true)"),
                    {"s": scope_literal(workspace)},
                )
                with pytest.raises(DBAPIError):
                    await conn.execute(text(statement))
                await conn.rollback()
    finally:
        if reader is not None:
            await reader.dispose()
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    f"revoke all on document_rows, documents, document_workspaces, processing_runs "
                    f"from {role}"
                )
            )
            await conn.execute(text(f"revoke usage on schema public from {role}"))
            await conn.commit()
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await clear_database(session)
        await engine.dispose()


async def test_a_connection_that_can_bypass_rls_is_refused() -> None:
    """The guard that makes the other three layers mean anything.

    Row-level security is skipped by a superuser or a role carrying BYPASSRLS.
    If the SQL tool connected as one, the policy would be inert while every
    query still looked properly scoped. The test connection is the
    application DSN, which is a superuser here, so it is the exact case the
    check exists to catch.
    """
    engine = create_async_engine(database_url())
    try:
        role = RowQuery(engine, statement_timeout_ms=1_000, max_result_rows=10)
        async with engine.connect() as conn:
            is_superuser = await conn.scalar(
                text("select rolsuper from pg_roles where rolname = current_user")
            )
        if not is_superuser:
            pytest.skip("The application role is not a superuser on this server")
        with pytest.raises(RowQueryError, match="bypass"):
            await role.verify_reader()
    finally:
        await engine.dispose()


async def test_a_plain_reader_role_passes_the_guard() -> None:
    engine = create_async_engine(database_url())
    role = "rls_probe_ok"
    reader: AsyncEngine | None = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
            await conn.execute(text(f"create role {role} login password 'rls-probe'"))
            await conn.execute(text(f"grant usage on schema public to {role}"))
        reader = create_async_engine(reader_url(database_url(), role))
        query = RowQuery(reader, statement_timeout_ms=1_000, max_result_rows=10)
        await query.verify_reader()
    finally:
        if reader is not None:
            await reader.dispose()
        async with engine.connect() as conn:
            await conn.execute(text(f"revoke usage on schema public from {role}"))
            await conn.commit()
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
        await engine.dispose()


async def test_rows_survive_the_limit_wrapper_and_render_with_column_names() -> None:
    """The whole point of the tool, against a real database.

    The statement is wrapped to enforce a result limit, which means the
    returned columns are the model's selection *plus* whatever `select *`
    brings along. An earlier version rejected any row carrying a column it
    did not expect, so every plain `select *` was silently discarded and the
    tool returned nothing while reporting no error. SQLite cannot show this:
    the wrapper, the JSONB round trip and row-level security are all
    Postgres-only.
    """
    engine = create_async_engine(database_url())
    role = "rls_probe_run"
    reader: AsyncEngine | None = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
            await conn.execute(text(f"create role {role} login password 'rls-probe'"))
            await conn.execute(text(f"grant usage on schema public to {role}"))
            await conn.execute(
                text(
                    "grant select on document_rows, documents, "
                    f"document_workspaces, processing_runs to {role}"
                )
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await clear_database(session)
            workspace, _ = await seed_owner(session, slug="rows", username="dana")
            document = Document(
                origin_workspace_id=workspace,
                original_filename="sales.csv",
                detected_media_type="text/csv",
                size_bytes=10,
                sha256="d" * 64,
                bucket="raw",
                object_key="k",
            )
            session.add(document)
            await session.flush()
            session.add(
                DocumentWorkspace(
                    workspace_id=document.origin_workspace_id,
                    document_id=document.id,
                    filename=document.original_filename,
                )
            )
            run = ProcessingRun(
                document_id=document.id,
                origin_workspace_id=workspace,
                job_kind="extract",
                pipeline_version="2026.1",
                status="ready",
                csv_columns=["id", "status", "amount"],
            )
            session.add(run)
            await session.flush()
            # Only a run the worker has activated carries this, and the
            # catalogue joins through it.
            document.active_processing_run_id = run.id
            session.add_all(
                [
                    DocumentRow(
                        origin_workspace_id=workspace,
                        document_id=document.id,
                        processing_run_id=run.id,
                        row_number=number,
                        data=data,
                    )
                    for number, data in (
                        (1, {"id": "1", "status": "OPEN", "amount": "1500"}),
                        (2, {"id": "2", "status": "CLOSED", "amount": "250"}),
                        (3, {"id": "3", "status": "OPEN", "amount": "9800"}),
                    )
                ]
            )
            await session.commit()

        reader = create_async_engine(reader_url(database_url(), role))
        query = RowQuery(reader, statement_timeout_ms=5_000, max_result_rows=200)
        scope = (workspace,)

        outcome = await query.run(
            "select * from document_rows order by row_number", workspace_ids=scope
        )
        rows = outcome.sources
        assert outcome.values == []
        assert len(rows) == 3
        assert [row.row_number for row in rows] == [1, 2, 3]
        # Column names come from the row's own data, so the rendering is
        # self-describing without joining the run's header. jsonb does not
        # preserve key order, so the rendered order is jsonb's canonical one
        # rather than the order the CSV header listed them in.
        assert rows[0].as_source("sales.csv").text == "id=1 | amount=1500 | status=OPEN"

        filtered = await query.run(
            "select * from document_rows where data->>'status' = 'OPEN' order by row_number",
            workspace_ids=scope,
        )
        assert [row.row_number for row in filtered.sources] == [1, 3]

        # An aggregate carries the answer and nothing to cite. It used to be
        # dropped for having no id, which left the model holding only the
        # column names and a "no rows matched" -- it had computed the answer
        # and then reported the data was absent.
        aggregated = await query.run(
            "select count(*) as n, sum((data->>'amount')::numeric) as total "
            f"from document_rows where document_id = '{document.id}'",
            workspace_ids=scope,
        )
        assert aggregated.sources == []
        assert set(aggregated.columns) == {"n", "total"}
        assert aggregated.values[0]["n"] == 3
        assert aggregated.values[0]["total"] is not None

        grouped = await query.run(
            "select data->>'status' as status, count(*) as n from document_rows "
            f"where document_id = '{document.id}' group by 1 order by 1",
            workspace_ids=scope,
        )
        assert grouped.sources == []
        assert {row["status"] for row in grouped.values} == {"CLOSED", "OPEN"}
        # Still empty of values when there is genuinely nothing to compute.
        empty = await query.run(
            "select count(*) as n from document_rows "
            f"where document_id = '{document.id}' and data->>'status' = 'nope'",
            workspace_ids=scope,
        )
        assert empty.sources == []
        assert empty.values[0]["n"] == 0

        catalogue = await query.queryable_files(workspace_ids=scope)
        assert [entry.filename for entry in catalogue.entries] == ["sales.csv"]
        # jsonb does not preserve key order, so the columns arrive in jsonb's
        # canonical order rather than the order the CSV header listed.
        assert sorted(catalogue.entries[0].columns) == ["amount", "id", "status"]
        assert catalogue.hidden == 0
    finally:
        if reader is not None:
            await reader.dispose()
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    f"revoke all on document_rows, documents, document_workspaces, processing_runs "
                    f"from {role}"
                )
            )
            await conn.execute(text(f"revoke usage on schema public from {role}"))
            await conn.commit()
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await clear_database(session)
        await engine.dispose()


async def test_the_catalogue_is_scoped_and_the_model_cannot_name_processing_runs() -> None:
    """Two things the catalogue depends on, in one place.

    It reads column names out of ``processing_runs``, so that table needs the
    same policy the row table already has. And the model still must not be
    able to name it: the wrapper may read it, but a generated statement
    should not reach a lease token or an embedding profile name.
    """
    engine = create_async_engine(database_url())
    role = "rls_probe_catalogue"
    reader: AsyncEngine | None = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
            await conn.execute(text(f"create role {role} login password 'rls-probe'"))
            await conn.execute(text(f"grant usage on schema public to {role}"))
            await conn.execute(
                text(
                    "grant select on document_rows, documents, "
                    f"document_workspaces, processing_runs to {role}"
                )
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await clear_database(session)
            mine, _ = await seed_owner(session, slug="mine", username="mine")
            theirs, _ = await seed_owner(session, slug="theirs", username="theirs")
            made: list[Document] = []
            for workspace, name, columns in (
                (mine, "sales.csv", ["id", "status", "amount"]),
                (theirs, "theirs.csv", ["secret"]),
            ):
                document = Document(
                    origin_workspace_id=workspace,
                    original_filename=name,
                    detected_media_type="text/csv",
                    size_bytes=10,
                    sha256=hashlib.sha256(name.encode()).hexdigest(),
                    bucket="raw",
                    object_key=f"k-{name}",
                )
                session.add(document)
                await session.flush()
                session.add(
                    DocumentWorkspace(
                        workspace_id=document.origin_workspace_id,
                        document_id=document.id,
                        filename=document.original_filename,
                    )
                )
                run = ProcessingRun(
                    document_id=document.id,
                    origin_workspace_id=workspace,
                    job_kind="extract",
                    pipeline_version="2026.1",
                    status="ready",
                    row_count=1,
                    csv_columns=columns,
                )
                session.add(run)
                # run.id is a Python-side default, so it is only populated once
                # flushed. Assigning it earlier writes null, and the catalogue
                # joins through this column.
                await session.flush()
                document.active_processing_run_id = run.id
                made.append(document)
            await session.commit()
            mine_document = made[0]

        reader = create_async_engine(reader_url(database_url(), role))
        query = RowQuery(reader, statement_timeout_ms=5_000, max_result_rows=200)

        catalogue = await query.queryable_files(workspace_ids=(mine,))
        # Scoped: the other workspace's file is not merely hidden from the
        # model, it was never received by the connection.
        assert [entry.filename for entry in catalogue.entries] == ["sales.csv"]
        assert catalogue.entries[0].columns == ["id", "status", "amount"]
        assert catalogue.entries[0].row_count == 1
        assert catalogue.filenames == {mine_document.id: "sales.csv"}

        # A different scope sees its own file and nothing of the other's.
        other = await query.queryable_files(workspace_ids=(theirs,))
        assert [entry.filename for entry in other.entries] == ["theirs.csv"]

        # Granted, so the wrapper can read it...
        assert (
            await query.queryable_files(workspace_ids=(mine,), pattern="sales")
        ).entries
        # ...but the model may not name it in a statement, even though the
        # role could read it. The allowlist is what stops that, so the
        # rejection has to come from validation rather than from a grant.
        with pytest.raises(RowQueryError, match="may only read"):
            await query.run(
                "select lease_token from processing_runs where document_id = :d",
                workspace_ids=(mine,),
            )
    finally:
        if reader is not None:
            await reader.dispose()
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    f"revoke all on document_rows, documents, "
                    f"document_workspaces, processing_runs from {role}"
                )
            )
            await conn.execute(text(f"revoke usage on schema public from {role}"))
            await conn.commit()
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await clear_database(session)
        await engine.dispose()


async def test_the_catalogue_omits_deleted_and_unfinished_documents() -> None:
    """A file that would answer nothing must not appear as queryable."""
    engine = create_async_engine(database_url())
    role = "rls_probe_inflight"
    reader: AsyncEngine | None = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
            await conn.execute(text(f"create role {role} login password 'rls-probe'"))
            await conn.execute(text(f"grant usage on schema public to {role}"))
            await conn.execute(
                text(
                    "grant select on document_rows, documents, "
                    f"document_workspaces, processing_runs to {role}"
                )
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await clear_database(session)
            workspace, _ = await seed_owner(session, slug="mixed", username="mixed")
            now = datetime.now(UTC)
            for name, status, deleted in (
                ("ready.csv", "ready", None),
                ("gone.csv", "ready", now),
                ("inflight.csv", "embedding", None),
            ):
                document = Document(
                    origin_workspace_id=workspace,
                    original_filename=name,
                    detected_media_type="text/csv",
                    size_bytes=10,
                    sha256=hashlib.sha256(name.encode()).hexdigest(),
                    bucket="raw",
                    object_key=f"k-{name}",
                    deleted_at=deleted,
                )
                session.add(document)
                await session.flush()
                session.add(
                    DocumentWorkspace(
                        workspace_id=document.origin_workspace_id,
                        document_id=document.id,
                        filename=document.original_filename,
                    )
                )
                run = ProcessingRun(
                    document_id=document.id,
                    origin_workspace_id=workspace,
                    job_kind="extract",
                    pipeline_version="2026.1",
                    status=status,
                    row_count=5,
                    csv_columns=["a"],
                )
                session.add(run)
                # run.id is a Python-side default, populated only on flush.
                await session.flush()
                # A run is not queryable until the worker has activated it.
                document.active_processing_run_id = run.id
            await session.commit()

        reader = create_async_engine(reader_url(database_url(), role))
        query = RowQuery(reader, statement_timeout_ms=5_000, max_result_rows=200)
        catalogue = await query.queryable_files(workspace_ids=(workspace,))
        assert [entry.filename for entry in catalogue.entries] == ["ready.csv"]
    finally:
        if reader is not None:
            await reader.dispose()
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    f"revoke all on document_rows, documents, "
                    f"document_workspaces, processing_runs from {role}"
                )
            )
            await conn.execute(text(f"revoke usage on schema public from {role}"))
            await conn.commit()
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await clear_database(session)
        await engine.dispose()


async def test_the_catalogue_reports_files_it_hid_and_pages_past_them() -> None:
    engine = create_async_engine(database_url())
    role = "rls_probe_paging"
    reader: AsyncEngine | None = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
            await conn.execute(text(f"create role {role} login password 'rls-probe'"))
            await conn.execute(text(f"grant usage on schema public to {role}"))
            await conn.execute(
                text(
                    "grant select on document_rows, documents, "
                    f"document_workspaces, processing_runs to {role}"
                )
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await clear_database(session)
            workspace, _ = await seed_owner(session, slug="many", username="many")
            for index in range(5):
                name = f"file-{index:02d}.csv"
                document = Document(
                    origin_workspace_id=workspace,
                    original_filename=name,
                    detected_media_type="text/csv",
                    size_bytes=10,
                    sha256=hashlib.sha256(name.encode()).hexdigest(),
                    bucket="raw",
                    object_key=f"k-{name}",
                )
                session.add(document)
                await session.flush()
                session.add(
                    DocumentWorkspace(
                        workspace_id=document.origin_workspace_id,
                        document_id=document.id,
                        filename=document.original_filename,
                    )
                )
                run = ProcessingRun(
                    document_id=document.id,
                    origin_workspace_id=workspace,
                    job_kind="extract",
                    pipeline_version="2026.1",
                    status="ready",
                    row_count=1,
                    csv_columns=["a"],
                )
                session.add(run)
                # run.id is a Python-side default, so it is only populated once
                # flushed. Assigning it earlier writes null, and the catalogue
                # joins through this column.
                await session.flush()
                document.active_processing_run_id = run.id
            await session.commit()

        reader = create_async_engine(reader_url(database_url(), role))
        query = RowQuery(reader, statement_timeout_ms=5_000, max_result_rows=200)

        first = await query.queryable_files(workspace_ids=(workspace,), limit=2)
        assert [entry.filename for entry in first.entries] == [
            "file-00.csv",
            "file-01.csv",
        ]
        assert first.hidden == 3

        second = await query.queryable_files(
            workspace_ids=(workspace,), offset=2, limit=2
        )
        assert [entry.filename for entry in second.entries] == [
            "file-02.csv",
            "file-03.csv",
        ]
        # Hidden counts every file this page is not showing, not just those
        # after it, so the model is never told a partial page is the whole
        # set.
        assert second.hidden == 3

        # A wildcard in the search term must not match every file, or the
        # point of a lookup is lost.
        literal = await query.queryable_files(workspace_ids=(workspace,), pattern="file-0_")
        assert [entry.filename for entry in literal.entries] == []
        exact = await query.queryable_files(workspace_ids=(workspace,), pattern="file-03")
        assert [entry.filename for entry in exact.entries] == ["file-03.csv"]
    finally:
        if reader is not None:
            await reader.dispose()
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    f"revoke all on document_rows, documents, "
                    f"document_workspaces, processing_runs from {role}"
                )
            )
            await conn.execute(text(f"revoke usage on schema public from {role}"))
            await conn.commit()
        async with engine.begin() as conn:
            await conn.execute(text(f"drop role if exists {role}"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await clear_database(session)
        await engine.dispose()


class UploadAndProcessStorage(InMemoryObjectStorage):
    """Object storage that satisfies both sides of the pipeline.

    ``InMemoryObjectStorage`` covers the upload route and
    ``ArtifactStorage`` covers the worker, and this test needs the same bytes
    to go through both. Keeping one instance is the point: it is what makes
    the stored object and the parsed rows provably the same file.
    """

    def download_file(
        self, destination: Path, *, bucket: str, key: str, max_bytes: int
    ) -> DownloadResult:
        data = self.objects[(bucket, key)]
        if len(data) > max_bytes:
            raise ObjectTooLargeError("too large")
        Path(destination).write_bytes(data)
        return DownloadResult(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def put_bytes(
        self,
        data: bytes,
        *,
        bucket: str,
        key: str,
        content_type: str,
        content_encoding: str | None,
        sha256: str,
    ) -> None:
        self.objects[(bucket, key)] = data

    def delete_prefix(self, *, bucket: str, prefix: str) -> None:
        for key in [k for (b, k) in self.objects if b == bucket and k.startswith(prefix)]:
            del self.objects[(bucket, key)]


async def test_a_csv_uploaded_over_http_lands_in_storage_then_rows() -> None:
    """The whole path, which nothing else covers.

    The worker tests seed a document row directly, so they skip both the
    upload route and the object store. This goes through them: POST the file,
    see it in storage, run the worker over the real run it enqueued, and read
    the rows back. A gap between what the API accepts and what the worker can
    parse would be invisible otherwise -- which is exactly how XLSX shipped,
    accepted by the validator and refused by the extractor.

    Also the only place jsonb and the manifest round trip are exercised
    together, and the only place the catalogue is checked against a file that
    actually went through the pipeline.
    """
    from tests.document_samples import csv_bytes

    engine = create_async_engine(database_url(), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings().model_copy(
        update={
            "environment": "test",
            "database_url": database_url(),
            "processing_temp_dir": Path("/tmp"),
            # No clamd here, and a scanner failure is a 503 before anything
            # is stored. Left on, every upload test fails for a reason that
            # has nothing to do with what it is checking.
            "antivirus_enabled": False,
        }
    )
    application = create_app(settings)
    storage = UploadAndProcessStorage()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_object_storage] = lambda: storage
    # There is no clamd here. The scanner is a route dependency rather than a
    # setting read at request time, so it has to be replaced outright or
    # every upload fails as a 503 before anything is stored.
    application.dependency_overrides[get_malware_scanner] = lambda: CleanScanner()

    worker_settings = settings.model_copy(update={"processing_poll_interval_seconds": 0.0})
    vector_store = FakeVectorStore()
    worker = ProcessingWorker(
        factory,
        storage,
        FakeExtractor(),
        worker_settings,
        FakeEmbeddingProvider(),
        vector_store,
    )

    try:
        async with factory() as session:
            await clear_database(session)
            workspace_id, credentials = await seed_owner(session)

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            token = await client.post("/v1/auth/token", data=credentials)
            assert token.status_code == 200
            headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
            response = await client.post(
                f"/v1/workspaces/{workspace_id}/documents",
                headers=headers,
                files={"file": ("sales.csv", csv_bytes(), "text/csv")},
            )
            assert response.status_code == 202, response.text
            body = response.json()
            assert body["detected_media_type"] == "text/csv"
            # Queued rather than refused: the pipeline does read CSVs.
            assert body["status"] == "processing"

        # The bytes reached object storage under the document's own key.
        document_id = UUID(body["id"])
        async with factory() as session:
            document = await session.get(Document, document_id)
        assert document is not None
        assert (
            storage.objects[(document.bucket, document.object_key)]
            == csv_bytes()
        )

        assert await worker.run_once() is True

        async with factory() as session:
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.document_id == document_id)
            )
            rows = (
                await session.scalars(
                    select(DocumentRow).where(
                        DocumentRow.document_id == document_id
                    )
                )
            ).all()
        assert run is not None
        assert run.status == "ready"
        # csv_bytes carries a header and three data rows.
        assert run.row_count == 3
        assert run.chunk_count == 0
        assert sorted(run.csv_columns or []) == ["amount", "currency", "id", "status"]

        assert [row.row_number for row in rows] == [1, 2, 3]
        assert rows[0].data == {
            "id": "1001",
            "status": "OPEN",
            "amount": "1500.00",
            "currency": "USD",
        }
        # No vectors were produced for any of it.
        assert vector_store.points == {}

        # And the catalogue the model will read names this file.
        catalogue_reader = create_async_engine(database_url())
        catalogue = RowQuery(
            catalogue_reader,
            statement_timeout_ms=5_000,
            max_result_rows=100,
        )
        listed = await catalogue.queryable_files(workspace_ids=(workspace_id,))
        assert [entry.filename for entry in listed.entries] == ["sales.csv"]
        assert listed.entries[0].document_id == document_id
        assert sorted(listed.entries[0].columns) == ["amount", "currency", "id", "status"]
        await catalogue.aclose()
        await catalogue_reader.dispose()
    finally:
        async with factory() as session:
            await clear_database(session)
        await engine.dispose()


async def test_an_xlsx_upload_is_refused_by_the_api() -> None:
    """XLSX was accepted once and then failed in the extractor.

    Replacing it with CSV means the picker and the pipeline agree again, so
    the rejection belongs at the door with a clear message rather than after
    a 202 and a background failure.
    """
    from tests.document_samples import xlsx_bytes

    engine = create_async_engine(database_url(), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings().model_copy(
        update={
            "environment": "test",
            "database_url": database_url(),
            # No clamd in this environment, and a scanner that cannot be
            # reached is a 503 before anything is stored.
            "antivirus_enabled": False,
        }
    )
    application = create_app(settings)
    storage = InMemoryObjectStorage()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_object_storage] = lambda: storage
    application.dependency_overrides[get_malware_scanner] = lambda: CleanScanner()
    try:
        async with factory() as session:
            await clear_database(session)
            workspace_id, credentials = await seed_owner(session)

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            token = await client.post("/v1/auth/token", data=credentials)
            assert token.status_code == 200
            response = await client.post(
                f"/v1/workspaces/{workspace_id}/documents",
                headers={"Authorization": f"Bearer {token.json()['access_token']}"},
                files={
                    "file": (
                        "budget.xlsx",
                        xlsx_bytes(),
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
                    )
                },
            )
            assert response.status_code == 415
            assert response.json()["code"] == "unsupported_file_type"
            # Refused at the door, so nothing was stored.
            assert storage.objects == {}
    finally:
        async with factory() as session:
            await clear_database(session)
        await engine.dispose()


async def test_a_row_query_is_served_by_the_document_index() -> None:
    """A statement naming one document must not scan every row in the table.

    ``RowQuery`` rejects any statement that does not name a ``document_id``,
    so this predicate is in every statement the tool will ever run. When the
    column carried no index, each of those statements started with a
    sequential scan whose cost grew with the whole workspace; the test below
    fails if the index is dropped, because the plan goes back to a scan.

    The seeding is deliberately lopsided in the direction a real workspace
    grows in: the file being queried holds a small share of the table, and
    most rows belong to other files. That is what makes an index worth
    reaching for. An earlier version of this test did the opposite -- 2,000 of
    2,005 rows in the queried file -- and correctly failed: when one file is
    99% of the table, reading the whole table in order really is the cheaper
    plan, and the planner is right to choose it. Protecting the index means
    testing the case it exists for, not forcing it on a case it does not.

    The assertion is on the plan rather than on a timing: a timing would pass
    on a small table and fail on a loaded CI machine, and the thing being
    protected is the access path, not the clock.
    """
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await clear_database(session)
            workspace, _ = await seed_owner(
                session, slug="indexprobe", username="indexprobe-user"
            )
            target = Document(
                origin_workspace_id=workspace,
                original_filename="target.csv",
                detected_media_type="text/csv",
                size_bytes=10,
                sha256="1" * 64,
                bucket="raw",
                object_key="target",
            )
            session.add(target)
            await session.flush()
            session.add(
                DocumentWorkspace(
                    workspace_id=target.origin_workspace_id,
                    document_id=target.id,
                    filename=target.original_filename,
                )
            )
            target_run = ProcessingRun(
                document_id=target.id,
                origin_workspace_id=workspace,
                job_kind="extract",
                pipeline_version="2026.1",
                status="ready",
                csv_columns=["region", "amount"],
            )
            session.add(target_run)
            await session.flush()

            for row_number in range(1, 21):
                session.add(
                    DocumentRow(
                        origin_workspace_id=workspace,
                        document_id=target.id,
                        processing_run_id=target_run.id,
                        row_number=row_number,
                        data={"region": f"r{row_number % 7}", "amount": row_number},
                    )
                )

            # The rest of the table: "read the whole table" and "read one
            # file" have to look different for the plan to mean anything.
            for index in range(5):
                other = Document(
                    origin_workspace_id=workspace,
                    original_filename=f"other-{index}.csv",
                    detected_media_type="text/csv",
                    size_bytes=10,
                    sha256=str(index + 2) * 64,
                    bucket="raw",
                    object_key=f"other-{index}",
                )
                session.add(other)
                await session.flush()
                session.add(
                    DocumentWorkspace(
                        workspace_id=other.origin_workspace_id,
                        document_id=other.id,
                        filename=other.original_filename,
                    )
                )
                other_run = ProcessingRun(
                    document_id=other.id,
                    origin_workspace_id=workspace,
                    job_kind="extract",
                    pipeline_version="2026.1",
                    status="ready",
                    csv_columns=["region", "amount"],
                )
                session.add(other_run)
                await session.flush()
                session.add_all(
                    DocumentRow(
                        origin_workspace_id=workspace,
                        document_id=other.id,
                        processing_run_id=other_run.id,
                        row_number=row_number,
                        data={"region": "noise", "amount": row_number},
                    )
                    for row_number in range(1, 1_001)
                )
            await session.commit()

        async with engine.begin() as conn:
            # Statistics first: without them the planner has no basis for
            # choosing an index over a scan and would reach the right answer
            # for the wrong reason.
            await conn.execute(text("ANALYZE document_rows"))
            plan = await conn.execute(
                text(
                    "EXPLAIN (FORMAT JSON) SELECT count(*) FROM document_rows "
                    "WHERE document_id = :document_id AND data->>'region' = 'r1'"
                ),
                {"document_id": target.id},
            )
            node = plan.scalar_one()[0]["Plan"]
            methods = _plan_methods(node)

        assert "ix_document_rows_document" in methods, (
            "the document_id index is no longer part of the plan; a per-file "
            f"row query has fallen back to a scan ({methods})"
        )
    finally:
        async with factory() as session:
            await clear_database(session)
        await engine.dispose()


def _plan_methods(node: Any) -> set[str]:
    """Every index name and scan node mentioned anywhere in a query plan.

    The plan arrives as nested JSON whose shape depends on which plan nodes
    the planner chose, so it is walked as plain data rather than typed.
    """
    found: set[str] = set()
    pending: list[Any] = [node]
    while pending:
        current = pending.pop()
        if not isinstance(current, dict):
            continue
        for key in ("Index Name", "Node Type"):
            value = current.get(key)
            if isinstance(value, str):
                found.add(value)
        children = current.get("Plans")
        if isinstance(children, list):
            pending.extend(children)
    return found
