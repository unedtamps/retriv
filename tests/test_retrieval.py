"""The retrieval pipeline, against SQLite with the SQL fetch stubbed.

The pgvector statement itself only runs on Postgres -- it is proven in
tests/test_postgres_integration.py. Everything downstream of the fetch
(threshold, hash validation, section completion and its scope check) runs
for real here against seeded tables.
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    insert,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.retrieval.context import ContextRetriever, RetrievalUnavailable
from app.sections import encode_section_key


def make_settings() -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        embedding_api_key=SecretStr("embedding-api-key-for-tests-0123456789"),
        embedding_dimensions=2,
    )


def tables() -> tuple[MetaData, Table, Table, Table, Table]:
    metadata = MetaData()
    documents = Table(
        "documents",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("origin_workspace_id", Uuid(as_uuid=True)),
        Column("active_processing_run_id", Uuid(as_uuid=True)),
        Column("deleted_at", DateTime(timezone=True)),
        Column("original_filename", String(255)),
    )
    chunks = Table(
        "document_chunks",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("origin_workspace_id", Uuid(as_uuid=True)),
        Column("document_id", Uuid(as_uuid=True)),
        Column("processing_run_id", Uuid(as_uuid=True)),
        Column("ordinal", Integer),
        Column("text", Text),
        Column("token_count", Integer),
        Column("content_hash", String(64)),
        Column("section_path", JSON),
        Column("section_key", Text),
        Column("is_heading_only", Boolean),
        Column("page_numbers", JSON),
    )
    links = Table(
        "document_workspaces",
        metadata,
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("document_id", Uuid(as_uuid=True), primary_key=True),
        Column("filename", String(255)),
        Column("deleted_at", DateTime(timezone=True)),
    )
    embeddings = Table(
        "chunk_embeddings",
        metadata,
        Column("chunk_id", Uuid(as_uuid=True), primary_key=True),
        Column("origin_workspace_id", Uuid(as_uuid=True)),
        Column("document_id", Uuid(as_uuid=True)),
        Column("processing_run_id", Uuid(as_uuid=True)),
        Column("embedding_profile", String(255)),
        Column("model", String(255)),
        Column("dimensions", Integer),
        Column("status", String(32)),
        Column("content_hash", String(64)),
    )
    return metadata, documents, chunks, embeddings, links


class StubbedFetch:
    """Stands in for the pgvector statement the retriever runs.

    The rows it returns are shaped exactly like the statement's result, so
    everything downstream -- the relevance floor, the hash check, section
    completion -- runs for real against the seeded SQLite tables.
    """

    def __init__(self, rows: Sequence[dict[str, object]]) -> None:
        self.rows = list(rows)
        self.calls = 0

    async def __call__(
        self,
        vector: list[float],
        workspace_ids: list[UUID],
        limit: int,
        *,
        document_id: UUID | None = None,
    ) -> list[dict[str, object]]:
        del document_id
        self.calls += 1
        assert workspace_ids, "the scope must reach the fetch"
        return self.rows[:limit]


async def make_retriever(
    settings: Settings,
    engine: AsyncEngine,
    rows: Sequence[dict[str, object]],
) -> tuple[ContextRetriever, list[httpx.Request], StubbedFetch]:
    embedding_requests: list[httpx.Request] = []

    def embedding_handler(request: httpx.Request) -> httpx.Response:
        embedding_requests.append(request)
        assert request.headers["authorization"] == (
            f"Bearer {settings.embedding_api_key.get_secret_value()}"
        )
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        )

    embedding_client = httpx.AsyncClient(
        base_url="http://embedding.test/v1/",
        headers={"Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"},
        transport=httpx.MockTransport(embedding_handler),
    )
    retriever = ContextRetriever(settings, engine, embedding_client=embedding_client)
    fetch = StubbedFetch(rows)
    retriever._fetch_vector_rows = fetch  # type: ignore[assignment]
    return retriever, embedding_requests, fetch


def match_row(seed: Mapping[str, object], score: float) -> dict[str, object]:
    """One vector-search result row, shaped like SEARCH_VECTORS returns it."""
    return {
        "score": score,
        "workspace_id": seed["workspace_id"],
        "document_id": seed["document_id"],
        "processing_run_id": seed["processing_run_id"],
        "chunk_id": seed["chunk_id"],
        "ordinal": seed["ordinal"],
        "text": seed["text"],
        "token_count": seed["token_count"],
        "content_hash": seed["content_hash"],
        "section_path": seed["section_path"],
        "section_key": seed["section_key"],
        "is_heading_only": seed["is_heading_only"],
        "page_numbers": seed["page_numbers"],
        "filename": seed["filename"],
    }


async def seed_section(
    engine: AsyncEngine,
    documents: Table,
    chunks: Table,
    embeddings: Table,
    links: Table,
    workspace_id: UUID,
    entries: Sequence[tuple[int, str, Sequence[str]]],
    *,
    first_ordinal: int = 10,
    deleted_at: object = None,
    filename: str = "regulations.pdf",
) -> list[dict[str, object]]:
    """Seed one document holding an ordered run of chunks.

    ``entries`` is ``(ordinal, text, section_path)`` in reading order. The path is
    per entry rather than per document because that is the shape the failure takes:
    a parent heading's content is filed under its sub-sections, so a document needs
    several nested paths to reproduce it.
    """
    document_id = uuid4()
    processing_run_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(documents).values(
                id=document_id,
                origin_workspace_id=workspace_id,
                active_processing_run_id=processing_run_id,
                deleted_at=deleted_at,
                original_filename=filename,
            )
        )
        await connection.execute(
            insert(links).values(
                workspace_id=workspace_id,
                document_id=document_id,
                filename=filename,
                deleted_at=deleted_at,
            )
        )
        seeded: list[dict[str, object]] = []
        for ordinal, text, section_path in entries:
            chunk_id = uuid4()
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            path = list(section_path)
            is_heading_only = text.rstrip().endswith(":") and len(text.split()) <= 6
            token_count = len(text.split())
            section_key = encode_section_key(path)
            await connection.execute(
                insert(chunks).values(
                    id=chunk_id,
                    origin_workspace_id=workspace_id,
                    document_id=document_id,
                    processing_run_id=processing_run_id,
                    ordinal=ordinal,
                    text=text,
                    token_count=token_count,
                    content_hash=content_hash,
                    section_path=path,
                    section_key=section_key,
                    is_heading_only=is_heading_only,
                    page_numbers=[23],
                )
            )
            await connection.execute(
                insert(embeddings).values(
                    chunk_id=chunk_id,
                    origin_workspace_id=workspace_id,
                    document_id=document_id,
                    processing_run_id=processing_run_id,
                    embedding_profile="test-profile",
                    model="test-embedding-model",
                    dimensions=2,
                    status="indexed",
                    content_hash=content_hash,
                )
            )
            seeded.append(
                {
                    "workspace_id": workspace_id,
                    "document_id": document_id,
                    "processing_run_id": processing_run_id,
                    "chunk_id": chunk_id,
                    "content_hash": content_hash,
                    "ordinal": ordinal,
                    "text": text,
                    "token_count": token_count,
                    "section_path": path,
                    "section_key": section_key,
                    "is_heading_only": is_heading_only,
                    "page_numbers": [23],
                    "filename": filename,
                }
            )
    assert seeded, "a section needs at least one chunk"
    assert seeded[0]["ordinal"] == first_ordinal
    return seeded


async def seed_active_chunk(
    engine: AsyncEngine,
    documents: Table,
    chunks: Table,
    embeddings: Table,
    links: Table,
    workspace_id: UUID,
) -> dict[str, object]:
    document_id = uuid4()
    processing_run_id = uuid4()
    chunk_id = uuid4()
    chunk_text = "Validated paragraph from PostgreSQL."
    content_hash = hashlib.sha256(chunk_text.encode()).hexdigest()
    token_count = len(chunk_text.split())
    async with engine.begin() as connection:
        await connection.execute(
            insert(documents).values(
                id=document_id,
                origin_workspace_id=workspace_id,
                active_processing_run_id=processing_run_id,
                deleted_at=None,
                original_filename="report.pdf",
            )
        )
        await connection.execute(
            insert(links).values(
                workspace_id=workspace_id,
                document_id=document_id,
                filename="report.pdf",
                deleted_at=None,
            )
        )
        await connection.execute(
            insert(chunks).values(
                id=chunk_id,
                origin_workspace_id=workspace_id,
                document_id=document_id,
                processing_run_id=processing_run_id,
                ordinal=2,
                text=chunk_text,
                token_count=token_count,
                content_hash=content_hash,
                section_path=["Summary"],
                page_numbers=[3],
            )
        )
        await connection.execute(
            insert(embeddings).values(
                chunk_id=chunk_id,
                origin_workspace_id=workspace_id,
                document_id=document_id,
                processing_run_id=processing_run_id,
                embedding_profile="test-profile",
                model="test-embedding-model",
                dimensions=2,
                status="indexed",
                content_hash=content_hash,
            )
        )
    return {
        "workspace_id": workspace_id,
        "document_id": document_id,
        "processing_run_id": processing_run_id,
        "chunk_id": chunk_id,
        "content_hash": content_hash,
        "ordinal": 2,
        "text": chunk_text,
        "token_count": token_count,
        "section_path": ["Summary"],
        "section_key": None,
        "is_heading_only": False,
        "page_numbers": [3],
        "filename": "report.pdf",
    }


async def test_a_match_past_the_threshold_is_served_with_its_row() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seed = await seed_active_chunk(engine, documents, chunks, embeddings, links, workspace_id)
    retriever, embedding_requests, fetch = await make_retriever(
        settings, engine, [match_row(seed, 0.93)]
    )
    try:
        sources = await retriever.retrieve("What is in the report?", [workspace_id], 4)
    finally:
        await retriever.aclose()

    assert len(embedding_requests) == 1
    assert fetch.calls == 1
    assert sources[0].text == "Validated paragraph from PostgreSQL."
    assert sources[0].workspace_id == workspace_id
    assert sources[0].page_numbers == [3]
    assert sources[0].score == pytest.approx(0.93)


async def test_below_threshold_match_is_not_materialized_as_context() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "min_relevance_score": 0.95,
        }
    )
    engine = create_async_engine(settings.database_url)
    workspace_id = uuid4()
    seed: dict[str, object] = {
        "workspace_id": workspace_id,
        "document_id": uuid4(),
        "processing_run_id": uuid4(),
        "chunk_id": uuid4(),
        "content_hash": "c" * 64,
        "ordinal": 2,
        "text": "Some text.",
        "token_count": 3,
        "section_path": [],
        "section_key": None,
        "is_heading_only": False,
        "page_numbers": [1],
        "filename": "report.pdf",
    }
    retriever, _, _ = await make_retriever(settings, engine, [match_row(seed, 0.8)])
    try:
        sources = await retriever.retrieve("unrelated question", [workspace_id], 3)
    finally:
        await retriever.aclose()

    assert sources == []


async def test_a_call_can_override_the_relevance_floor() -> None:
    """The configured floor is a default, not a constant: one call can move it."""
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "min_relevance_score": 0.25,
        }
    )
    engine = create_async_engine(settings.database_url)
    workspace_id = uuid4()
    text = "Some text."
    seed: dict[str, object] = {
        "workspace_id": workspace_id,
        "document_id": uuid4(),
        "processing_run_id": uuid4(),
        "chunk_id": uuid4(),
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "ordinal": 2,
        "text": text,
        "token_count": 2,
        "section_path": [],
        "section_key": None,
        "is_heading_only": False,
        "page_numbers": [1],
        "filename": "report.pdf",
    }
    retriever, _, _ = await make_retriever(settings, engine, [match_row(seed, 0.4)])
    try:
        default = await retriever.retrieve("question", [workspace_id], 3)
        lowered = await retriever.retrieve("question", [workspace_id], 3, min_score=0.1)
        raised = await retriever.retrieve("question", [workspace_id], 3, min_score=0.5)
    finally:
        await retriever.aclose()

    assert [source.text for source in default] == [text]
    assert [source.text for source in lowered] == [text]
    assert raised == []


async def test_chunk_text_must_match_persisted_content_hash() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seed = await seed_active_chunk(engine, documents, chunks, embeddings, links, workspace_id)
    row = match_row(seed, 0.9)
    row["text"] = "modified text with stale hash"
    retriever, _, _ = await make_retriever(settings, engine, [row])
    try:
        with pytest.raises(RetrievalUnavailable):
            await retriever.retrieve("question", [workspace_id], 2)
    finally:
        await retriever.aclose()


async def test_an_empty_or_duplicated_scope_is_refused() -> None:
    """The scope is the retriever's only authorization input, so it must
    never be treated as "no filter"."""
    settings_ = make_settings()
    engine = create_async_engine(settings_.database_url)

    retriever, _, fetch = await make_retriever(settings_, engine, [])
    try:
        with pytest.raises(RetrievalUnavailable, match="non-empty workspace scope"):
            await retriever.retrieve("question", [], 4)

        workspace_id = uuid4()
        with pytest.raises(RetrievalUnavailable, match="duplicate"):
            await retriever.retrieve("question", [workspace_id, workspace_id], 4)
    finally:
        await retriever.aclose()
    assert fetch.calls == 0, "a refused scope must not reach the search"


async def test_a_heading_match_pulls_in_the_subtree_it_heads() -> None:
    """The failure this exists for: a heading retrieved, its content did not.

    A heading is short and topically concentrated, so it wins the similarity
    comparison against the prose it introduces. On the UNDP regulations this produced
    an answer asserting the article's content was absent from a document that
    contained it.

    The shape matters. The markdown extractor files the substance of "Pasal 16" under one section
    per numbered sub-provision, so the article's own key is held by a single short
    chunk and the prose lives at deeper keys that no amount of position-based
    expansion reaches. Only a subtree query does.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "Regulation 16.01: The Executive Board shall have sole authority.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
            (
                12,
                "Regulation 16.02: The Administrator is authorized to approve.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.02"],
            ),
        ],
    )
    heading = seeded[0]
    retriever, _, _ = await make_retriever(
        settings, engine, [match_row(heading, 0.81)]
    )
    try:
        sources = await retriever.retrieve("details about article 16", [workspace_id], 4)
    finally:
        await retriever.aclose()

    texts = [source.text for source in sources]
    assert "Pasal 16: Kerangka Umum" in texts
    assert "Regulation 16.01: The Executive Board shall have sole authority." in texts
    assert "Regulation 16.02: The Administrator is authorized to approve." in texts
    assert texts[0] == "Pasal 16: Kerangka Umum"
    assert sources[0].score == pytest.approx(0.81), "the match keeps its own score"
    assert sources[1].score < sources[0].score, "completion never outranks the match"
    assert sources[1].page_numbers == [23]
    assert sources[1].section_path == [
        "PMK",
        "Pasal 16: Kerangka Umum",
        "16.01",
    ]


async def test_completion_never_reaches_a_cousin_section() -> None:
    """The subtree stops at its own branch.

    "Pasal 160" starts with the characters of "Pasal 16", and the two are adjacent in
    reading order. A prefix match that confuses the two would answer a question about
    one article with the text of another, which is worse than returning less.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "Regulation 16.01 belongs to article sixteen.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
            (12, "Pasal 160: Retensiophil", ["PMK", "Pasal 160: Retensiophil"]),
            (
                13,
                "Regulation 160.01 belongs to article one-sixty.",
                ["PMK", "Pasal 160: Retensiophil", "160.01"],
            ),
        ],
    )
    retriever, _, _ = await make_retriever(
        settings, engine, [match_row(seeded[0], 0.81)]
    )
    try:
        sources = await retriever.retrieve("details about article 16", [workspace_id], 4)
    finally:
        await retriever.aclose()

    joined = " ".join(source.text for source in sources)
    assert "belongs to article sixteen." in joined
    assert "one-sixty" not in joined
    assert "Retensiophil" not in joined
    assert all(source.section_path[:2] == ["PMK", "Pasal 16: Kerangka Umum"] for source in sources)


async def test_an_underscore_in_a_heading_does_not_widen_the_subtree() -> None:
    """LIKE reads ``_`` as a wildcard, and headings contain underscores.

    "Ayat (1)_a" and "Ayat (1)Xa" are different sections. Without escaping the key,
    the pattern for the first matches the second and its sub-sections, so a question
    about one provision would be answered with another.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Ayat (1)_a adalah ketentuan pertama.", ["PMK", "Ayat (1)_a"]),
            (11, "Isi ketentuan satu.", ["PMK", "Ayat (1)_a", "isi"]),
            (12, "Ayat (1)Xa adalah ketentuan lain.", ["PMK", "Ayat (1)Xa"]),
            (13, "Isi ketentuan lain.", ["PMK", "Ayat (1)Xa", "isi"]),
        ],
    )
    retriever, _, _ = await make_retriever(
        settings, engine, [match_row(seeded[0], 0.81)]
    )
    try:
        sources = await retriever.retrieve("ketentuan ayat satu", [workspace_id], 4)
    finally:
        await retriever.aclose()

    joined = " ".join(source.text for source in sources)
    assert "Isi ketentuan satu." in joined
    assert "Isi ketentuan lain." not in joined
    assert "Ayat (1)Xa" not in joined


async def test_heading_expansion_never_exceeds_the_callers_budget() -> None:
    """Expansion is bounded by the caller's overall budget, not merely by ``limit``.

    ``limit`` bounds similarity matches; ``budget`` bounds the total. Expansion uses
    the difference, and must still never push the caller past ``budget``.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 10,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "Regulation 16.01 body one with plenty of words to pass the filter.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
            (
                12,
                "Regulation 16.02 body two with plenty of words to pass the filter.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.02"],
            ),
            (
                13,
                "Regulation 16.03 body three with plenty of words to pass it.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.03"],
            ),
            (
                14,
                "Regulation 16.04 body four with plenty of words to pass it.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.04"],
            ),
        ],
    )
    generous, _, _ = await make_retriever(settings, engine, [match_row(seeded[0], 0.81)])
    tight, _, _ = await make_retriever(settings, engine, [match_row(seeded[0], 0.81)])
    try:
        sources = await generous.retrieve("details about article 16", [workspace_id], 2, budget=3)
        capped = await tight.retrieve("details about article 16", [workspace_id], 2, budget=2)
    finally:
        await generous.aclose()

    assert len(sources) == 3, "budget=3 caps matches plus completion at three"
    assert sources[0].text == "Pasal 16: Kerangka Umum"
    assert len(capped) == 2, "a budget equal to limit leaves no completion room"


async def test_a_fully_matched_section_adds_nothing() -> None:
    """Completion fills gaps; it does not duplicate what the search already returned.

    Every chunk anchors its own section now rather than only bare headings, because a
    parent heading whose intro was merged into one chunk is precisely the one whose
    sub-sections went missing. The guard against that costing anything is that a
    section already represented returns no new rows.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Regulation 16.01 body text of ordinary length.", ["PMK", "16.01"]),
            (11, "Regulation 16.02 another body paragraph of ordinary length.", ["PMK", "16.02"]),
        ],
    )
    retriever, _, _ = await make_retriever(
        settings, engine, [match_row(seed, 0.81) for seed in seeded]
    )
    try:
        sources = await retriever.retrieve("executive board authority", [workspace_id], 4, budget=8)
    finally:
        await retriever.aclose()

    assert len(sources) == 2, "both sections were already fully represented"
    assert sources[0].text.startswith("Regulation 16.01 body text")


async def test_completion_never_reaches_outside_the_signed_scope() -> None:
    """Completed rows are not backed by a match in this search, so scope must be re-checked.

    These rows are loaded without a vector match in this search; if the scope filter
    were skipped, a heading in an authorised document could pull in text from a
    workspace the caller never signed for.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "Regulation 16.01 in the signed scope.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
        ],
    )
    await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        other_workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "SECRET other workspace text.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
        ],
    )
    retriever, _, _ = await make_retriever(
        settings, engine, [match_row(seeded[0], 0.81)]
    )
    try:
        sources = await retriever.retrieve("details about article 16", [workspace_id], 6)
    finally:
        await retriever.aclose()

    joined = " ".join(source.text for source in sources)
    assert "Regulation 16.01 in the signed scope." in joined
    assert "SECRET" not in joined
    assert all(source.workspace_id == workspace_id for source in sources), (
        "every source stays inside the requested scope"
    )


async def test_document_scoped_completion_stays_inside_that_document() -> None:
    """A search narrowed to one document must not complete a sibling's section."""
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "Regulation 16.01 in the chosen document.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
        ],
    )
    await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "SIBLING document text.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
        ],
    )
    document_id = UUID(str(seeded[0]["document_id"]))
    retriever, _, _ = await make_retriever(
        settings, engine, [match_row(seeded[0], 0.81)]
    )
    try:
        scoped = await retriever.retrieve(
            "details about article 16", [workspace_id], 6, document_id=document_id
        )
        unscoped = await retriever.retrieve("details about article 16", [workspace_id], 6)
    finally:
        await retriever.aclose()

    scoped_text = " ".join(source.text for source in scoped)
    assert "Regulation 16.01 in the chosen document." in scoped_text
    assert "SIBLING document text." not in scoped_text
    assert all(source.document_id == document_id for source in scoped)
    assert "SIBLING document text." in " ".join(source.text for source in unscoped), (
        "without the filter, the sibling document is a legitimate completion"
    )


async def test_completion_still_fires_when_the_search_fills_its_quota() -> None:
    """The realistic case: a full page of matches must still leave room to complete.

    ``limit`` bounds similarity matches, so when the search returns its whole quota
    there is nothing left over under a naive reading -- yet that is precisely the
    situation that produced the empty Article 16 answer. ``budget`` is what separates
    the two, and no caller passed it, so the feature was unreachable in production.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 3,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "Regulation 16.01: The Executive Board shall have sole authority.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
            (
                12,
                "Regulation 16.02: The Administrator is authorized to approve.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.02"],
            ),
            (
                13,
                "Regulation 16.03: Programme activities are subject to arrangements.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.03"],
            ),
        ],
    )
    rows = [match_row(seeded[0], 0.81), match_row(seeded[1], 0.79)]
    retriever, _, _ = await make_retriever(settings, engine, rows)
    try:
        sources = await retriever.retrieve("details about article 16", [workspace_id], 2, budget=5)
    finally:
        await retriever.aclose()

    texts = [source.text for source in sources]
    assert len(sources) == 4, "two matches plus two completed, within budget=5"
    assert "Regulation 16.01: The Executive Board shall have sole authority." in texts
    assert "Regulation 16.02: The Administrator is authorized to approve." in texts


async def test_completion_is_off_when_no_budget_is_granted() -> None:
    """The regression that made section completion dead code in production.

    ``budget`` defaults to None, which collapses the cap onto ``limit``, so a search
    that filled its quota had no room left. Every measured baseline recorded
    ``expansion_only_hits: 0`` for exactly this reason. The cap must still hold when
    a caller genuinely forgets to pass one -- the fix is that ``search_context``
    passes ``max_context_sources``, not that the default quietly grows.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Pasal 16: Kerangka Umum", ["PMK", "Pasal 16: Kerangka Umum"]),
            (
                11,
                "Regulation 16.01 body of the article.",
                ["PMK", "Pasal 16: Kerangka Umum", "16.01"],
            ),
        ],
    )
    retriever, _, _ = await make_retriever(
        settings, engine, [match_row(seeded[0], 0.81)]
    )
    try:
        without = await retriever.retrieve("article 16", [workspace_id], 1)
        with_budget = await retriever.retrieve(
            "article 16", [workspace_id], 1, budget=settings.max_context_sources
        )
    finally:
        await retriever.aclose()

    assert len(without) == 1, "no budget means no room, as documented"
    assert len(with_budget) == 2, "a budget is what lets the section come back"
    assert settings.max_context_sources > settings.max_context_chunks, (
        "the shipped default must leave room, or the tool can never complete "
        "a section on a full page of matches"
    )


async def test_a_document_linked_to_two_workspaces_is_found_from_either() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    first_workspace = uuid4()
    second_workspace = uuid4()
    seed = await seed_active_chunk(engine, documents, chunks, embeddings, links, first_workspace)
    async with engine.begin() as connection:
        await connection.execute(
            insert(links).values(
                workspace_id=second_workspace,
                document_id=seed["document_id"],
                filename="shared.pdf",
                deleted_at=None,
            )
        )
    row = match_row(seed, 0.93)
    row["workspace_id"] = second_workspace
    row["filename"] = "shared.pdf"
    retriever, _, _ = await make_retriever(settings, engine, [row])
    try:
        sources = await retriever.retrieve("question", [second_workspace], 2)
    finally:
        await retriever.aclose()

    assert len(sources) == 1
    assert sources[0].workspace_id == second_workspace
    assert sources[0].filename == "shared.pdf"


async def text_search_env() -> tuple[ContextRetriever, AsyncEngine, Any, Any, Any, Any]:
    """A retriever over the minimal tables, with no embedding client in play."""
    settings = make_settings()
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings, links = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    return ContextRetriever(settings, engine), engine, documents, chunks, embeddings, links


async def test_text_search_finds_an_identifier_a_vector_would_miss() -> None:
    """The whole point of a lexical tool: a code is a string, not a topic."""
    retriever, engine, documents, chunks, embeddings, links = await text_search_env()
    workspace_id = uuid4()
    await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [
            (10, "Invoice INV-2024-001 was settled in March.", ["Billing"]),
            (11, "Invoice INV-2024-002 is still overdue.", ["Billing"]),
        ],
    )
    try:
        sources = await retriever.search_text("INV-2024-001", [workspace_id], 10)
    finally:
        await retriever.aclose()

    assert [source.text for source in sources] == [
        "Invoice INV-2024-001 was settled in March."
    ]
    assert sources[0].workspace_id == workspace_id
    assert sources[0].score == 1.0


async def test_text_search_is_case_insensitive() -> None:
    retriever, engine, documents, chunks, embeddings, links = await text_search_env()
    workspace_id = uuid4()
    await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [(10, "Pasal 7 mengatur pengadaan langsung.", ["Pasal 7"])],
    )
    try:
        sources = await retriever.search_text("pasal 7", [workspace_id], 10)
    finally:
        await retriever.aclose()

    assert len(sources) == 1
    assert sources[0].text.startswith("Pasal 7")


async def test_text_search_treats_wildcards_as_literal_text() -> None:
    """A query of ``%`` must not turn into "every chunk in the workspace"."""
    retriever, engine, documents, chunks, embeddings, links = await text_search_env()
    workspace_id = uuid4()
    await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [(10, "No percent sign here.", ["Summary"])],
    )
    try:
        sources = await retriever.search_text("%", [workspace_id], 10)
    finally:
        await retriever.aclose()

    assert sources == []


async def test_text_search_stays_inside_the_signed_scope() -> None:
    retriever, engine, documents, chunks, embeddings, links = await text_search_env()
    workspace_id = uuid4()
    await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [(10, "Confidential memo about the merger.", ["Memo"])],
    )
    try:
        sources = await retriever.search_text("merger", [uuid4()], 10)
    finally:
        await retriever.aclose()

    assert sources == []


async def test_text_search_can_be_narrowed_to_one_document() -> None:
    retriever, engine, documents, chunks, embeddings, links = await text_search_env()
    workspace_id = uuid4()
    first = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [(10, "Procurement policy, revision A.", ["Policy"])],
        filename="policy-a.pdf",
    )
    await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [(10, "Procurement policy, revision B.", ["Policy"])],
        filename="policy-b.pdf",
    )
    try:
        both = await retriever.search_text("Procurement policy", [workspace_id], 10)
        narrowed = await retriever.search_text(
            "Procurement policy",
            [workspace_id],
            10,
            document_id=first[0]["document_id"],  # type: ignore[arg-type]
        )
    finally:
        await retriever.aclose()

    assert len(both) == 2
    assert [source.text for source in narrowed] == ["Procurement policy, revision A."]


async def test_text_search_does_not_require_an_indexed_embedding() -> None:
    """Text that failed to embed is still text a reader can use."""
    retriever, engine, documents, chunks, embeddings, links = await text_search_env()
    workspace_id = uuid4()
    await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        links,
        workspace_id,
        [(10, "Unembedded but readable paragraph.", ["Summary"])],
    )
    async with engine.begin() as connection:
        await connection.execute(update(embeddings).values(status="pending"))
    try:
        sources = await retriever.search_text("readable paragraph", [workspace_id], 10)
    finally:
        await retriever.aclose()

    assert [source.text for source in sources] == ["Unembedded but readable paragraph."]


async def test_text_search_refuses_an_empty_query_or_scope() -> None:
    retriever, engine, _documents, _chunks, _embeddings, _links = await text_search_env()
    try:
        with pytest.raises(RetrievalUnavailable):
            await retriever.search_text("   ", [uuid4()], 5)
        with pytest.raises(RetrievalUnavailable):
            await retriever.search_text("merger", [], 5)
    finally:
        await retriever.aclose()
