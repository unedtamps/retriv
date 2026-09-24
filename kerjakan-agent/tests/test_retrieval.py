import base64
import hashlib
import json
from collections.abc import Callable
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from sqlalchemy import (
    JSON,
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

from agent.config import Settings
from agent.retrieval import (
    ContextRetriever,
    OutOfScopeSearchResult,
    RetrievalUnavailable,
)


def make_settings() -> Settings:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return Settings(
        environment="test",
        api_token=SecretStr("agent-api-token-for-tests-0123456789"),
        database_url=SecretStr("sqlite+aiosqlite://"),
        scope_public_key_b64=SecretStr(base64.b64encode(public_pem).decode("ascii")),
        qdrant_api_key=SecretStr("qdrant-readonly-key-for-tests-0123456789"),
        embedding_api_key=SecretStr("embedding-api-key-for-tests-0123456789"),
        chat_api_key=SecretStr("chat-api-key-for-tests-0123456789"),
        embedding_dimensions=2,
    )


def tables() -> tuple[MetaData, Table, Table, Table]:
    metadata = MetaData()
    documents = Table(
        "source_documents",
        metadata,
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("document_id", Uuid(as_uuid=True), primary_key=True),
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
        Column("content_hash", String(64)),
        Column("section_path", JSON),
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
                workspace_id=workspace_id,
                document_id=document_id,
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


def qdrant_payload(row: dict[str, UUID | str]) -> dict[str, str]:
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
    qdrant_point: dict[str, object],
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
        assert request.headers["api-key"] == settings.qdrant_api_key.get_secret_value()
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
        headers={"api-key": settings.qdrant_api_key.get_secret_value()},
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
    engine = create_async_engine(settings.database_url.get_secret_value())
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
    engine = create_async_engine(settings.database_url.get_secret_value())
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
    engine = create_async_engine(settings.database_url.get_secret_value())
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
    engine = create_async_engine(settings.database_url.get_secret_value())
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
    engine = create_async_engine(settings.database_url.get_secret_value())
    metadata, documents, chunks, embeddings = tables()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    workspace_id = uuid4()
    row = await seed_active_chunk(engine, documents, chunks, embeddings, workspace_id)
    async with engine.begin() as connection:
        await connection.execute(
            update(documents)
            .where(
                documents.c.workspace_id == workspace_id,
                documents.c.document_id == row["document_id"],
            )
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
    engine = create_async_engine(settings.database_url.get_secret_value())
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
