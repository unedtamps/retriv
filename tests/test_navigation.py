"""Reading a document in order, as opposed to searching it.

These drive ``DocumentNavigator`` over the real ORM models on an in-memory
SQLite, the same database ``tests/conftest.py`` builds. Hand-written table
definitions would drift from the models the navigator actually queries, so the
schema under test is the shipping one.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.models import (
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentWorkspace,
    ProcessingRun,
)
from app.db.session import Base
from app.retrieval.navigation import (
    DocumentNavigator,
    DocumentNotFound,
    DocumentNotNavigable,
)
from app.sections import encode_section_key

PENDING = "pending"


def make_settings() -> Settings:
    # The embedding profile has to match the one the seed writes into the
    # ledger, or every row is correctly filtered out as a superseded profile.
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        embedding_api_key=SecretStr("embedding-api-key-for-tests-0123456789"),
        embedding_dimensions=2,
        embedding_profile="test-profile",
        embedding_model="test-embedding-model",
    )


@dataclass(frozen=True)
class Seeded:
    workspace_id: UUID
    document_id: UUID


async def seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 5,
    tokens: int = 10,
    media_type: str = "application/pdf",
    filename: str = "regulations.pdf",
    deleted_at: datetime | None = None,
    indexed: bool = True,
    mismatched_embedding_hash_at: int | None = None,
    corrupt_text_at: int | None = None,
) -> Seeded:
    """A document with ``count`` chunks in reading order, indexed by default.

    Insert order follows the foreign keys: the document before its run, chunks
    before the embedding rows keyed by chunk id.
    """
    workspace_id = uuid4()
    document_id = uuid4()
    async with factory() as session:
        document = Document(
            id=document_id,
            origin_workspace_id=workspace_id,
            original_filename=filename,
            detected_media_type=media_type,
            size_bytes=10,
            sha256=uuid4().hex * 2,
            bucket="raw-documents",
            object_key=f"raw-documents/{uuid4()}",
            created_by_user_id=uuid4(),
            deleted_at=deleted_at,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentWorkspace(
                workspace_id=workspace_id,
                document_id=document.id,
                filename=filename,
                created_by_user_id=uuid4(),
                deleted_at=deleted_at,
            )
        )

        run = ProcessingRun(
            document_id=document_id,
            origin_workspace_id=workspace_id,
            job_kind="extract",
            pipeline_version="test",
            status="ready",
        )
        session.add(run)
        await session.flush()
        # Assigned once the run exists rather than passed in, so the two ids
        # cannot disagree.
        document.id = document_id
        document.active_processing_run_id = run.id

        for ordinal in range(count):
            text = f"Provision {ordinal} of the article."
            digest = hashlib.sha256(text.encode()).hexdigest()
            # Corrupting the stored text after hashing is the case the
            # recompute guard exists for: the ledger still agrees with the
            # stored hash, so only recomputing the text catches it.
            if corrupt_text_at == ordinal:
                text = f"Provision {ordinal}, edited in place."
            path = [f"Article {ordinal // 3}:"]
            chunk = DocumentChunk(
                origin_workspace_id=workspace_id,
                document_id=document_id,
                processing_run_id=run.id,
                ordinal=ordinal,
                text=text,
                token_count=tokens,
                section_path=path,
                section_key=encode_section_key(path),
                is_heading_only=ordinal % 3 == 2,
                page_numbers=[ordinal + 1],
                content_hash=digest,
            )
            session.add(chunk)
            await session.flush()
            session.add(
                ChunkEmbedding(
                    chunk_id=chunk.id,
                    origin_workspace_id=workspace_id,
                    document_id=document_id,
                    processing_run_id=run.id,
                    embedding_profile="test-profile",
                    model="test-embedding-model",
                    dimensions=2,
                    status="indexed" if indexed else PENDING,
                    content_hash=(
                        digest
                        if mismatched_embedding_hash_at != ordinal
                        else hashlib.sha256(b"different").hexdigest()
                    ),
                    qdrant_point_id=uuid4(),
                )
            )
        await session.commit()
    return Seeded(workspace_id=workspace_id, document_id=document_id)


@dataclass(frozen=True)
class Env:
    navigator: DocumentNavigator
    seeded: Seeded
    factory: async_sessionmaker[AsyncSession]
    engine: AsyncEngine

    async def read(self, **kwargs: Any) -> Any:
        """Read with the seeded document's own scope, unless one is given."""
        options: dict[str, Any] = {
            "document_id": self.seeded.document_id,
            "workspace_ids": [self.seeded.workspace_id],
        }
        options.update(kwargs)
        return await self.navigator.read_window(**options)


async def seeded_navigator(
    settings: Settings, **seed_kwargs: Any
) -> Env:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    seeded = await seed(factory, **seed_kwargs)
    return Env(
        navigator=DocumentNavigator(settings, factory),
        seeded=seeded,
        factory=factory,
        engine=engine,
    )


async def test_a_window_reads_forward_from_the_requested_ordinal() -> None:
    env = await seeded_navigator(make_settings(), count=10)

    window = await env.read(start_ordinal=3)

    assert [source.ordinal for source in window.sources] == [3, 4, 5, 6, 7, 8, 9]
    assert window.total == 10
    assert window.next_ordinal is None, "the window reached the end of the document"
    assert window.sources[0].text == "Provision 3 of the article."


async def test_a_truncated_window_reports_where_to_resume() -> None:
    env = await seeded_navigator(make_settings(), count=20)

    window = await env.read(limit=5)

    assert len(window.sources) == 5
    assert window.total == 20
    assert window.next_ordinal == 5


async def test_the_token_budget_bounds_the_window_not_the_count() -> None:
    """Chunk sizes vary far more than the count, so tokens are the right unit."""
    settings = make_settings().model_copy(update={"navigation_token_budget": 100})
    env = await seeded_navigator(settings, count=20, tokens=30)

    window = await env.read(limit=50)

    assert len(window.sources) == 3, "100 tokens at 30 each admits three"
    assert sum(source.token_count for source in window.sources) <= 100
    assert window.next_ordinal == 3


async def test_a_window_always_advances_even_over_budget() -> None:
    """Trimming the only chunk would return nothing, and the caller would loop."""
    settings = make_settings().model_copy(update={"navigation_token_budget": 500})
    env = await seeded_navigator(settings, count=4, tokens=900)

    window = await env.read()

    assert len(window.sources) == 1, "one chunk over budget is still returned"
    assert window.sources[0].token_count > settings.navigation_token_budget
    assert window.next_ordinal == 1, "the caller can still make progress"


async def test_a_document_outside_the_scope_is_not_readable() -> None:
    env = await seeded_navigator(make_settings())

    with pytest.raises(DocumentNotFound):
        await env.read(workspace_ids=[uuid4()])


async def test_a_deleted_document_is_not_readable() -> None:
    env = await seeded_navigator(
        make_settings(), deleted_at=datetime(2026, 1, 1, tzinfo=UTC)
    )

    with pytest.raises(DocumentNotFound):
        await env.read()


async def test_a_csv_is_refused_with_the_tool_that_can_read_it() -> None:
    """CSV rows are not chunks, so the failure has to name query_rows."""
    env = await seeded_navigator(
        make_settings(), media_type="text/csv", filename="ledger.csv"
    )

    with pytest.raises(DocumentNotNavigable, match="query_rows"):
        await env.read()


async def test_an_unindexed_embedding_is_not_returned_as_text() -> None:
    """Rows here carry no Qdrant match, so the embedding ledger is the only guard."""
    env = await seeded_navigator(make_settings(), count=4, indexed=False)

    window = await env.read()

    assert window.sources == []


async def test_a_chunk_whose_text_was_edited_is_refused() -> None:
    """The ledger can agree with a stored hash that no longer describes the text."""
    env = await seeded_navigator(make_settings(), count=4, corrupt_text_at=1)

    with pytest.raises(DocumentNotFound):
        await env.read()


async def test_a_stale_embedding_hash_filters_the_chunk_out() -> None:
    """A ledger that disagrees with the chunk is not an error, it is just not current."""
    env = await seeded_navigator(
        make_settings(), count=4, mismatched_embedding_hash_at=1
    )

    window = await env.read()

    assert [source.ordinal for source in window.sources] == [0, 2, 3]


async def test_a_superseded_run_is_not_read() -> None:
    """Chunks from a run the document no longer points at are not its text."""
    env = await seeded_navigator(make_settings(), count=4)
    async with env.factory() as session:
        document = await session.get(Document, env.seeded.document_id)
        assert document is not None
        document.active_processing_run_id = uuid4()
        await session.commit()

    window = await env.read()

    assert window.sources == []
    assert window.total == 0


async def test_an_empty_scope_is_refused() -> None:
    env = await seeded_navigator(make_settings())

    with pytest.raises(DocumentNotFound):
        await env.read(workspace_ids=[])


async def test_a_duplicated_scope_is_refused() -> None:
    env = await seeded_navigator(make_settings())
    duplicated = [env.seeded.workspace_id, env.seeded.workspace_id]

    with pytest.raises(DocumentNotFound):
        await env.read(workspace_ids=duplicated)


async def test_a_window_preserves_heading_flags_and_section_paths() -> None:
    """The flags are what tell a reader that a chunk is a title, not text."""
    env = await seeded_navigator(make_settings(), count=6)

    window = await env.read()

    assert [source.ordinal for source in window.sources] == [0, 1, 2, 3, 4, 5]
    assert [source.is_heading_only for source in window.sources] == [
        False,
        False,
        True,
        False,
        False,
        True,
    ]
    assert window.sources[0].section_path == ["Article 0:"]
    assert window.sources[0].section_key == encode_section_key(["Article 0:"])
    assert all(source.score == 1.0 for source in window.sources), "positioned, not ranked"


async def test_a_document_with_no_active_run_is_not_readable() -> None:
    env = await seeded_navigator(make_settings(), count=2)
    async with env.factory() as session:
        document = await session.get(Document, env.seeded.document_id)
        assert document is not None
        document.active_processing_run_id = None
        await session.commit()

    with pytest.raises(DocumentNotFound):
        await env.read()