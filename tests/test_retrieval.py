import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
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
from app.retrieval.context import (
    ContextRetriever,
    OutOfScopeSearchResult,
    RetrievalUnavailable,
)
from app.sections import encode_section_key


def make_settings() -> Settings:
    # No api token and no scope keypair: the answer path is a direct call,
    # so the only credential the retriever holds is the read-only Qdrant
    # key. That is the assertion these tests now protect.
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        qdrant_read_only_api_key=SecretStr("qdrant-readonly-key-for-tests-0123456789"),
        embedding_api_key=SecretStr("embedding-api-key-for-tests-0123456789"),
        embedding_dimensions=2,
    )


def tables() -> tuple[MetaData, Table, Table, Table]:
    metadata = MetaData()
    documents = Table(
        "documents",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("workspace_id", Uuid(as_uuid=True)),
        Column("active_processing_run_id", Uuid(as_uuid=True)),
        Column("deleted_at", DateTime(timezone=True)),
        Column("original_filename", String(255)),
    )
    chunks = Table(
        "document_chunks",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("workspace_id", Uuid(as_uuid=True)),
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
    embeddings = Table(
        "chunk_embeddings",
        metadata,
        Column("chunk_id", Uuid(as_uuid=True), primary_key=True),
        Column("workspace_id", Uuid(as_uuid=True)),
        Column("document_id", Uuid(as_uuid=True)),
        Column("processing_run_id", Uuid(as_uuid=True)),
        Column("embedding_profile", String(255)),
        Column("model", String(255)),
        Column("dimensions", Integer),
        Column("status", String(32)),
        Column("content_hash", String(64)),
        Column("qdrant_point_id", Uuid(as_uuid=True)),
    )
    return metadata, documents, chunks, embeddings


async def seed_section(
    engine: AsyncEngine,
    documents: Table,
    chunks: Table,
    embeddings: Table,
    workspace_id: UUID,
    entries: Sequence[tuple[int, str, Sequence[str]]],
    *,
    first_ordinal: int = 10,
    deleted_at: object = None,
) -> list[dict[str, UUID | str | int]]:
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
                workspace_id=workspace_id,
                active_processing_run_id=processing_run_id,
                deleted_at=deleted_at,
                original_filename="regulations.pdf",
            )
        )
        seeded: list[dict[str, UUID | str | int]] = []
        for ordinal, text, section_path in entries:
            chunk_id = uuid4()
            point_id = uuid4()
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            path = list(section_path)
            await connection.execute(
                insert(chunks).values(
                    id=chunk_id,
                    workspace_id=workspace_id,
                    document_id=document_id,
                    processing_run_id=processing_run_id,
                    ordinal=ordinal,
                    text=text,
                    token_count=len(text.split()),
                    content_hash=content_hash,
                    section_path=path,
                    section_key=encode_section_key(path),
                    # Only a chunk the parser reported as nothing but headings.
                    is_heading_only=text.rstrip().endswith(":")
                    and len(text.split()) <= 6,
                    page_numbers=[23],
                )
            )
            await connection.execute(
                insert(embeddings).values(
                    chunk_id=chunk_id,
                    workspace_id=workspace_id,
                    document_id=document_id,
                    processing_run_id=processing_run_id,
                    embedding_profile="test-profile",
                    model="test-embedding-model",
                    dimensions=2,
                    status="indexed",
                    content_hash=content_hash,
                    qdrant_point_id=point_id,
                )
            )
            seeded.append(
                {
                    "workspace_id": workspace_id,
                    "document_id": document_id,
                    "processing_run_id": processing_run_id,
                    "chunk_id": chunk_id,
                    "qdrant_point_id": point_id,
                    "content_hash": content_hash,
                    "ordinal": ordinal,
                    "section_key": encode_section_key(path),
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
    workspace_id: UUID,
) -> dict[str, UUID | str]:
    document_id = uuid4()
    processing_run_id = uuid4()
    chunk_id = uuid4()
    point_id = uuid4()
    chunk_text = "Validated paragraph from PostgreSQL."
    content_hash = hashlib.sha256(chunk_text.encode()).hexdigest()
    async with engine.begin() as connection:
        await connection.execute(
            insert(documents).values(
                id=document_id,
                workspace_id=workspace_id,
                active_processing_run_id=processing_run_id,
                deleted_at=None,
                original_filename="report.pdf",
            )
        )
        await connection.execute(
            insert(chunks).values(
                id=chunk_id,
                workspace_id=workspace_id,
                document_id=document_id,
                processing_run_id=processing_run_id,
                ordinal=2,
                text=chunk_text,
                token_count=len(chunk_text.split()),
                content_hash=content_hash,
                section_path=["Summary"],
                page_numbers=[3],
            )
        )
        await connection.execute(
            insert(embeddings).values(
                chunk_id=chunk_id,
                workspace_id=workspace_id,
                document_id=document_id,
                processing_run_id=processing_run_id,
                embedding_profile="test-profile",
                model="test-embedding-model",
                dimensions=2,
                status="indexed",
                content_hash=content_hash,
                qdrant_point_id=point_id,
            )
        )
    return {
        "workspace_id": workspace_id,
        "document_id": document_id,
        "processing_run_id": processing_run_id,
        "chunk_id": chunk_id,
        "qdrant_point_id": point_id,
        "content_hash": content_hash,
    }


def qdrant_payload(row: Mapping[str, UUID | str | int]) -> dict[str, str]:
    return {
        "workspace_id": str(row["workspace_id"]),
        "document_id": str(row["document_id"]),
        "processing_run_id": str(row["processing_run_id"]),
        "chunk_id": str(row["chunk_id"]),
        "content_hash": str(row["content_hash"]),
        "embedding_profile": "test-profile",
    }


async def make_retriever(
    settings: Settings,
    engine: AsyncEngine,
    qdrant_point: dict[str, object] | None,
    *,
    qdrant_handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[ContextRetriever, list[httpx.Request], list[httpx.Request]]:
    embedding_requests: list[httpx.Request] = []
    qdrant_requests: list[httpx.Request] = []

    def embedding_handler(request: httpx.Request) -> httpx.Response:
        embedding_requests.append(request)
        assert request.headers["authorization"] == (
            f"Bearer {settings.embedding_api_key.get_secret_value()}"
        )
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        )

    def vector_handler(request: httpx.Request) -> httpx.Response:
        qdrant_requests.append(request)
        assert request.headers["api-key"] == (
            settings.qdrant_read_only_api_key.get_secret_value()
        )
        if qdrant_handler is not None:
            return qdrant_handler(request)
        return httpx.Response(200, json={"result": {"points": [qdrant_point]}})

    embedding_client = httpx.AsyncClient(
        base_url="http://embedding.test/v1/",
        headers={
            "Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"
        },
        transport=httpx.MockTransport(embedding_handler),
    )
    qdrant_client = httpx.AsyncClient(
        base_url="http://qdrant.test/",
        headers={"api-key": settings.qdrant_read_only_api_key.get_secret_value()},
        transport=httpx.MockTransport(vector_handler),
    )
    retriever = ContextRetriever(
        settings,
        engine,
        embedding_client=embedding_client,
        qdrant_client=qdrant_client,
    )
    return retriever, embedding_requests, qdrant_requests


async def test_retrieval_filters_qdrant_and_returns_only_validated_postgres_text() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "qdrant_collection": "test-chunks",
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    row = await seed_active_chunk(engine, documents, chunks, embeddings, workspace_id)
    point = {
        "id": str(row["qdrant_point_id"]),
        "score": 0.93,
        "payload": qdrant_payload(row),
    }
    retriever, embedding_requests, qdrant_requests = await make_retriever(
        settings, engine, point
    )
    try:
        sources = await retriever.retrieve("What is in the report?", [workspace_id], 4)
    finally:
        await retriever.aclose()

    assert len(embedding_requests) == len(qdrant_requests) == 1
    qdrant_body = httpx.Request(
        "POST", "http://qdrant.test", content=qdrant_requests[0].content
    ).read()
    assert b"Validated paragraph from PostgreSQL" not in qdrant_body
    assert sources[0].text == "Validated paragraph from PostgreSQL."
    assert sources[0].workspace_id == workspace_id
    assert sources[0].page_numbers == [3]
    assert sources[0].score == pytest.approx(0.93)
    body = json.loads(qdrant_requests[0].content)
    assert body["filter"]["must"][0]["match"]["any"] == [str(workspace_id)]
    assert body["filter"]["must"][1]["match"]["value"] == "test-profile"
    assert body["with_payload"] == [
        "workspace_id",
        "document_id",
        "processing_run_id",
        "chunk_id",
        "content_hash",
        "embedding_profile",
    ]
    assert body["with_vector"] is False


async def test_out_of_scope_qdrant_hit_aborts_before_postgres_or_llm() -> None:
    settings = make_settings()
    engine = create_async_engine(settings.database_url)
    workspace_id = uuid4()
    other_workspace = uuid4()
    bad_row: dict[str, UUID | str] = {
        "workspace_id": other_workspace,
        "document_id": uuid4(),
        "processing_run_id": uuid4(),
        "chunk_id": uuid4(),
        "content_hash": "b" * 64,
    }
    point = {
        "id": str(uuid4()),
        "score": 0.9,
        "payload": qdrant_payload(bad_row),
    }
    retriever, _, _ = await make_retriever(settings, engine, point)
    try:
        with pytest.raises(OutOfScopeSearchResult):
            await retriever.retrieve("question", [workspace_id], 2)
    finally:
        await retriever.aclose()


async def test_below_threshold_qdrant_match_is_not_materialized_as_context() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "min_relevance_score": 0.95,
        }
    )
    engine = create_async_engine(settings.database_url)
    workspace_id = uuid4()
    row: dict[str, UUID | str] = {
        "workspace_id": workspace_id,
        "document_id": uuid4(),
        "processing_run_id": uuid4(),
        "chunk_id": uuid4(),
        "content_hash": "c" * 64,
    }
    point = {
        "id": str(uuid4()),
        "score": 0.8,
        "payload": qdrant_payload(row),
    }
    retriever, _, _ = await make_retriever(settings, engine, point)
    try:
        sources = await retriever.retrieve("unrelated question", [workspace_id], 3)
    finally:
        await retriever.aclose()

    assert sources == []


async def test_qdrant_metadata_mismatch_aborts_retrieval() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    row = await seed_active_chunk(engine, documents, chunks, embeddings, workspace_id)
    payload = qdrant_payload(row)
    payload["document_id"] = str(uuid4())
    point = {"id": str(row["qdrant_point_id"]), "score": 0.9, "payload": payload}
    retriever, _, _ = await make_retriever(settings, engine, point)
    try:
        with pytest.raises(RetrievalUnavailable):
            await retriever.retrieve("question", [workspace_id], 2)
    finally:
        await retriever.aclose()


async def test_inactive_document_run_is_never_returned() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    row = await seed_active_chunk(engine, documents, chunks, embeddings, workspace_id)
    async with engine.begin() as connection:
        await connection.execute(
            update(documents)
            .where(documents.c.id == row["document_id"])
            .values(active_processing_run_id=uuid4())
        )
    point = {
        "id": str(row["qdrant_point_id"]),
        "score": 0.9,
        "payload": qdrant_payload(row),
    }
    retriever, _, _ = await make_retriever(settings, engine, point)
    try:
        with pytest.raises(RetrievalUnavailable):
            await retriever.retrieve("question", [workspace_id], 2)
    finally:
        await retriever.aclose()


async def test_chunk_text_must_match_persisted_content_hash() -> None:
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    row = await seed_active_chunk(engine, documents, chunks, embeddings, workspace_id)
    async with engine.begin() as connection:
        await connection.execute(
            update(chunks)
            .where(chunks.c.id == row["chunk_id"])
            .values(text="modified text with stale hash")
        )
    point = {
        "id": str(row["qdrant_point_id"]),
        "score": 0.9,
        "payload": qdrant_payload(row),
    }
    retriever, _, _ = await make_retriever(settings, engine, point)
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

    from app.retrieval.context import ContextRetriever as _ContextRetriever

    retriever = _ContextRetriever(
        settings_,
        engine,
        embedding_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
                )
            )
        ),
        qdrant_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"result": {"points": []}})
            )
        ),
    )
    try:
        with pytest.raises(RetrievalUnavailable, match="non-empty workspace scope"):
            await retriever.retrieve("question", [], 4)

        workspace_id = uuid4()
        with pytest.raises(RetrievalUnavailable, match="duplicate"):
            await retriever.retrieve("question", [workspace_id, workspace_id], 4)
    finally:
        await retriever.aclose()


async def test_a_heading_match_pulls_in_the_subtree_it_heads() -> None:
    """The failure this exists for: a heading retrieved, its content did not.

    A heading is short and topically concentrated, so it wins the similarity
    comparison against the prose it introduces. On the UNDP regulations this produced
    an answer asserting the article's content was absent from a document that
    contained it.

    The shape matters. Docling files the substance of "Pasal 16" under one section
    per numbered sub-provision, so the article's own key is held by a single short
    chunk and the prose lives at deeper keys that no amount of position-based
    expansion reaches. Only a subtree query does.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "qdrant_collection": "test-chunks",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
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
    point = {
        "id": str(heading["qdrant_point_id"]),
        "score": 0.81,
        "payload": qdrant_payload(heading),
    }
    retriever, _embedding_requests, _qdrant_requests = await make_retriever(
        settings, engine, point
    )
    try:
        sources = await retriever.retrieve("details about article 16", [workspace_id], 4)
    finally:
        await retriever.aclose()

    texts = [source.text for source in sources]
    assert "Pasal 16: Kerangka Umum" in texts
    assert "Regulation 16.01: The Executive Board shall have sole authority." in texts
    assert "Regulation 16.02: The Administrator is authorized to approve." in texts
    # Reading order is preserved so the model sees the section as written.
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
            "qdrant_collection": "test-chunks",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
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
    point = {
        "id": str(seeded[0]["qdrant_point_id"]),
        "score": 0.81,
        "payload": qdrant_payload(seeded[0]),
    }
    retriever, _embedding_requests, _qdrant_requests = await make_retriever(
        settings, engine, point
    )
    try:
        sources = await retriever.retrieve("details about article 16", [workspace_id], 4)
    finally:
        await retriever.aclose()

    joined = " ".join(source.text for source in sources)
    assert "belongs to article sixteen." in joined
    assert "one-sixty" not in joined
    assert "Retensiophil" not in joined
    assert all(
        source.section_path[:2] == ["PMK", "Pasal 16: Kerangka Umum"]
        for source in sources
    )


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
            "qdrant_collection": "test-chunks",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        workspace_id,
        [
            (10, "Ayat (1)_a adalah ketentuan pertama.", ["PMK", "Ayat (1)_a"]),
            (11, "Isi ketentuan satu.", ["PMK", "Ayat (1)_a", "isi"]),
            (12, "Ayat (1)Xa adalah ketentuan lain.", ["PMK", "Ayat (1)Xa"]),
            (13, "Isi ketentuan lain.", ["PMK", "Ayat (1)Xa", "isi"]),
        ],
    )
    point = {
        "id": str(seeded[0]["qdrant_point_id"]),
        "score": 0.81,
        "payload": qdrant_payload(seeded[0]),
    }
    retriever, _embedding_requests, _qdrant_requests = await make_retriever(
        settings, engine, point
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
            "qdrant_collection": "test-chunks",
            "section_expand_chunks": 10,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
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
    point = {
        "id": str(seeded[0]["qdrant_point_id"]),
        "score": 0.81,
        "payload": qdrant_payload(seeded[0]),
    }
    # Both retrievers share one engine: the test database is in-memory SQLite, so a
    # second engine would be a second, empty database.
    generous, _e, _q = await make_retriever(settings, engine, point)
    tight, _e, _q = await make_retriever(settings, engine, point)
    try:
        sources = await generous.retrieve(
            "details about article 16", [workspace_id], 2, budget=3
        )
        capped = await tight.retrieve(
            "details about article 16", [workspace_id], 2, budget=2
        )
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
            "qdrant_collection": "test-chunks",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
        workspace_id,
        [
            (10, "Regulation 16.01 body text of ordinary length.", ["PMK", "16.01"]),
            (11, "Regulation 16.02 another body paragraph of ordinary length.", ["PMK", "16.02"]),
        ],
    )
    points = [
        {
            "id": str(row["qdrant_point_id"]),
            "score": 0.81,
            "payload": qdrant_payload(row),
        }
        for row in seeded
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"points": points}})

    retriever, _embedding_requests, _qdrant_requests = await make_retriever(
        settings, engine, None, qdrant_handler=handler
    )
    try:
        sources = await retriever.retrieve(
            "executive board authority", [workspace_id], 4, budget=8
        )
    finally:
        await retriever.aclose()

    assert len(sources) == 2, "both sections were already fully represented"
    assert sources[0].text.startswith("Regulation 16.01 body text")


async def test_completion_never_reaches_outside_the_signed_scope() -> None:
    """Completed rows are not backed by a Qdrant match, so scope must be re-checked.

    These rows are loaded without a vector match in this search; if the scope filter
    were skipped, a heading in an authorised document could pull in text from a
    workspace the caller never signed for.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "qdrant_collection": "test-chunks",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
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
    point = {
        "id": str(seeded[0]["qdrant_point_id"]),
        "score": 0.81,
        "payload": qdrant_payload(seeded[0]),
    }
    retriever, _embedding_requests, _qdrant_requests = await make_retriever(
        settings, engine, point
    )
    try:
        sources = await retriever.retrieve("details about article 16", [workspace_id], 6)
    finally:
        await retriever.aclose()

    joined = " ".join(source.text for source in sources)
    assert "Regulation 16.01 in the signed scope." in joined
    assert "SECRET" not in joined
    assert all(
        source.workspace_id == workspace_id for source in sources
    ), "every source stays inside the requested scope"


async def test_completion_still_fires_when_qdrant_fills_its_quota() -> None:
    """The realistic case: a full page of matches must still leave room to complete.

    ``limit`` bounds similarity matches, so when Qdrant returns its whole quota there
    is nothing left over under a naive reading -- yet that is precisely the situation
    that produced the empty Article 16 answer. ``budget`` is what separates the two,
    and no caller passed it, so the feature was unreachable in production.
    """
    settings = make_settings().model_copy(
        update={
            "embedding_model": "test-embedding-model",
            "embedding_profile": "test-profile",
            "qdrant_collection": "test-chunks",
            "section_expand_chunks": 3,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
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
    # Qdrant returns the heading plus unrelated matches, filling the limit exactly.
    points = [
        {
            "id": str(seeded[index]["qdrant_point_id"]),
            "score": score,
            "payload": qdrant_payload(seeded[index]),
        }
        for index, score in ((0, 0.81), (1, 0.79))
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"points": points}})
    retriever, _embedding_requests, _qdrant_requests = await make_retriever(
        settings, engine, None, qdrant_handler=handler
    )
    try:
        sources = await retriever.retrieve(
            "details about article 16", [workspace_id], 2, budget=5
        )
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
            "qdrant_collection": "test-chunks",
            "section_expand_chunks": 4,
        }
    )
    engine = create_async_engine(settings.database_url)
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    seeded = await seed_section(
        engine,
        documents,
        chunks,
        embeddings,
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
    point = {
        "id": str(seeded[0]["qdrant_point_id"]),
        "score": 0.81,
        "payload": qdrant_payload(seeded[0]),
    }
    retriever, _embedding_requests, _qdrant_requests = await make_retriever(
        settings, engine, point
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
