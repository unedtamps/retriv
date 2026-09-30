import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from app.db.models import (
    EMBEDDING_INDEXED,
    EMBEDDING_RETRY_WAIT,
    RUN_CANCELLED,
    RUN_EXTRACTING,
    RUN_FAILED,
    RUN_FETCHING,
    RUN_READY,
    ChunkEmbedding,
    DocumentChunk,
    EventInbox,
    ProcessingRun,
    SourceDocument,
)
from app.embeddings.base import VectorStoreError
from app.processing.worker import LeaseLostError
from tests.conftest import (
    ServiceContext,
    deleted_event,
    ready_event,
    send_event,
)


async def test_worker_stores_artifact_chunks_and_workspace_scoped_context(
    client: httpx.AsyncClient,
    context: ServiceContext,
    internal_api_headers: dict[str, str],
) -> None:
    content = b"a representative PDF"
    event = ready_event(content)
    data = event["data"]
    raw_key = (data["bucket"], data["object_key"])
    context.storage.objects[raw_key] = content
    assert (await send_event(client, context.settings, event)).status_code == 202
    worker = context.worker()

    assert await worker.run_once() is True
    workspace_id = data["workspace_id"]
    document_id = data["document_id"]
    status = await client.get(
        f"/internal/workspaces/{workspace_id}/documents/{document_id}/status",
        headers=internal_api_headers,
    )
    chunks = await client.get(
        f"/internal/workspaces/{workspace_id}/documents/{document_id}/chunks",
        headers=internal_api_headers,
    )
    other_workspace = await client.get(
        f"/internal/workspaces/{uuid4()}/documents/{document_id}/chunks",
        headers=internal_api_headers,
    )

    assert status.status_code == 200
    assert status.json()["runs"][0]["status"] == RUN_READY
    assert status.json()["runs"][0]["embedded_chunk_count"] == 2
    assert status.json()["runs"][0]["embedding_dimensions"] == 8
    assert chunks.status_code == 200
    assert chunks.json()["total"] == 2
    assert chunks.json()["items"][0]["page_numbers"] == [1]
    assert other_workspace.status_code == 404
    artifact_keys = {
        key for bucket, key in context.storage.objects if bucket == "context-documents"
    }
    assert any(key.endswith("docling-document.json.gz") for key in artifact_keys)
    assert any(key.endswith("manifest.json") for key in artifact_keys)
    async with context.session_factory() as session:
        embeddings = (await session.scalars(select(ChunkEmbedding))).all()
    assert len(embeddings) == 2
    assert {embedding.status for embedding in embeddings} == {EMBEDDING_INDEXED}
    assert len(context.vector_store.points) == 2
    for point in context.vector_store.points.values():
        assert point.payload["workspace_id"] == workspace_id
        assert point.payload["document_id"] == document_id
        assert "chunk_id" in point.payload
        assert "text" not in point.payload


async def test_worker_rejects_source_hash_mismatch(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    event = ready_event(b"expected", sha256="a" * 64)
    data = event["data"]
    context.storage.objects[(data["bucket"], data["object_key"])] = b"differnt"
    assert (await send_event(client, context.settings, event)).status_code == 202
    worker = context.worker()

    assert await worker.run_once() is True

    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
        chunks = (await session.scalars(select(DocumentChunk))).all()
    assert run is not None
    assert run.status == RUN_FAILED
    assert "hash does not match" in (run.last_error or "")
    assert chunks == []


async def test_delete_event_removes_chunks_and_artifacts(
    client: httpx.AsyncClient,
    context: ServiceContext,
    internal_api_headers: dict[str, str],
) -> None:
    content = b"a representative PDF"
    ready = ready_event(content)
    data = ready["data"]
    context.storage.objects[(data["bucket"], data["object_key"])] = content
    worker = context.worker()
    assert (await send_event(client, context.settings, ready)).status_code == 202
    assert await worker.run_once() is True
    assert len(context.vector_store.points) == 2
    artifact_prefix = (
        f"workspaces/{data['workspace_id']}/documents/{data['document_id']}/"
    )
    context.storage.objects[
        ("context-documents", f"{artifact_prefix}runs/orphan/attempts/stale/output")
    ] = b"orphan"

    deleted = deleted_event(ready)
    assert (await send_event(client, context.settings, deleted)).status_code == 202
    async with context.session_factory() as session:
        assert (await session.scalars(select(DocumentChunk))).all() == []

    assert await worker.run_once() is True
    artifact_keys = [
        key for bucket, key in context.storage.objects if bucket == "context-documents"
    ]
    assert artifact_keys == []
    assert context.vector_store.points == {}
    assert context.vector_store.deleted_documents[-1] == (
        UUID(data["workspace_id"]),
        UUID(data["document_id"]),
    )
    response = await client.get(
        f"/internal/workspaces/{data['workspace_id']}/documents/{data['document_id']}/status",
        headers=internal_api_headers,
    )
    assert response.status_code == 200
    assert response.json()["deleted_at"] is not None
    assert response.json()["active_processing_run_id"] is None
    assert response.json()["original_filename"] is None
    async with context.session_factory() as session:
        document = await session.get(
            SourceDocument,
            {
                "workspace_id": UUID(data["workspace_id"]),
                "document_id": UUID(data["document_id"]),
            },
        )
        ready_inbox = await session.get(EventInbox, UUID(ready["id"]))
    assert document is not None
    assert document.raw_object_key is None
    assert ready_inbox is not None
    assert ready_inbox.payload == {"redacted": True}

    context.storage.objects[
        (
            "context-documents",
            f"workspaces/{data['workspace_id']}/documents/{data['document_id']}/"
            "runs/stale/attempts/late/orphan",
        )
    ] = b"late orphan"
    reconciliation_worker = context.worker(
        context.settings.model_copy(
            update={"artifact_reconciliation_interval_seconds": 1}
        )
    )
    assert await reconciliation_worker.run_once() is False
    assert [
        key for bucket, key in context.storage.objects if bucket == "context-documents"
    ] == []


async def test_context_endpoint_requires_workload_authentication(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        f"/internal/workspaces/{uuid4()}/documents/{uuid4()}/chunks"
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_service_credentials"


async def test_document_identity_cannot_cross_workspace(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    document_id = UUID("7f8f30f7-f5d2-4d4b-8e83-869ef7f6fa6d")
    first = ready_event(b"first", document_id=document_id)
    second = ready_event(b"second", document_id=document_id)

    assert (await send_event(client, context.settings, first)).status_code == 202
    assert (await send_event(client, context.settings, second)).status_code == 202

    async with context.session_factory() as session:
        runs = (await session.scalars(select(ProcessingRun))).all()
    assert len(runs) == 2
    assert runs[0].workspace_id != runs[1].workspace_id


async def test_partial_artifact_upload_is_compensated(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    content = b"a representative PDF"
    event = ready_event(content)
    data = event["data"]
    context.storage.objects[(data["bucket"], data["object_key"])] = content
    context.storage.fail_put_suffix = "manifest.json"
    worker_settings = context.settings.model_copy(
        update={"processing_max_attempts": 1}
    )
    worker = context.worker(worker_settings)
    assert (await send_event(client, context.settings, event)).status_code == 202

    assert await worker.run_once() is True

    artifact_keys = [
        key for bucket, key in context.storage.objects if bucket == "context-documents"
    ]
    assert artifact_keys == []
    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
    assert run is not None
    assert run.status == RUN_FAILED


async def test_stale_worker_cannot_advance_run_phase(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    event = ready_event(b"source")
    assert (await send_event(client, context.settings, event)).status_code == 202
    worker = context.worker()
    claimed = await worker._claim_one()
    assert claimed is not None
    old_token = claimed.lease_token
    assert old_token is not None
    async with context.session_factory() as session:
        stored = await session.get(ProcessingRun, claimed.id)
        assert stored is not None
        stored.lease_token = uuid4()
        await session.commit()

    with pytest.raises(LeaseLostError):
        await worker._set_phase(claimed.id, old_token, RUN_FETCHING)


async def test_stale_worker_compensates_attempt_artifacts_after_lease_loss(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    content = b"source"
    event = ready_event(content)
    data = event["data"]
    context.storage.objects[(data["bucket"], data["object_key"])] = content
    assert (await send_event(client, context.settings, event)).status_code == 202
    worker = context.worker()
    original_set_phase = worker._set_phase
    phase_calls = 0

    async def cancel_during_upload(
        run_id: UUID, lease_token: UUID, status: str
    ) -> None:
        nonlocal phase_calls
        phase_calls += 1
        if phase_calls == 3:
            async with context.session_factory() as session:
                run = await session.get(ProcessingRun, run_id)
                assert run is not None
                run.status = RUN_CANCELLED
                run.lease_token = None
                run.lease_until = None
                await session.commit()
        await original_set_phase(run_id, lease_token, status)

    worker._set_phase = cancel_during_upload  # type: ignore[method-assign]

    assert await worker.run_once() is True

    artifact_objects = [
        key for bucket, key in context.storage.objects if bucket == "context-documents"
    ]
    assert artifact_objects == []
    deleted_attempt_keys = [
        key for bucket, key in context.storage.deleted_keys if bucket == "context-documents"
    ]
    assert any("/attempts/" in key for key in deleted_attempt_keys)


async def test_expired_lease_at_retry_limit_is_failed(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    event = ready_event(b"source")
    assert (await send_event(client, context.settings, event)).status_code == 202
    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
        assert run is not None
        run.status = RUN_EXTRACTING
        run.attempts = context.settings.processing_max_attempts
        run.lease_token = uuid4()
        run.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    worker = context.worker()

    await worker._recover_expired_leases()

    async with context.session_factory() as session:
        recovered = await session.scalar(select(ProcessingRun))
    assert recovered is not None
    assert recovered.status == RUN_FAILED
    assert recovered.lease_token is None
    assert recovered.completed_at is not None


async def test_extraction_timeout_stops_lease_renewal_and_fails_at_retry_limit(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    content = b"source"
    event = ready_event(content)
    data = event["data"]
    context.storage.objects[(data["bucket"], data["object_key"])] = content
    context.extractor.delay_seconds = 0.1
    assert (await send_event(client, context.settings, event)).status_code == 202
    settings = context.settings.model_copy(
        update={"processing_timeout_seconds": 0.01, "processing_max_attempts": 1}
    )
    worker = context.worker(settings)

    assert await worker.run_once() is True

    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
    assert run is not None
    assert run.status == RUN_FAILED
    assert "time limit" in (run.last_error or "")


async def test_extraction_output_limits_are_enforced(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    content = b"source"
    event = ready_event(content)
    data = event["data"]
    context.storage.objects[(data["bucket"], data["object_key"])] = content
    assert (await send_event(client, context.settings, event)).status_code == 202
    settings = context.settings.model_copy(update={"max_artifact_bytes": 1})
    worker = context.worker(settings)

    assert await worker.run_once() is True

    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
    assert run is not None
    assert run.status == RUN_FAILED
    assert "artifact exceeds" in (run.last_error or "")


async def test_embedding_failure_retries_without_reextracting(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    content = b"source"
    event = ready_event(content)
    data = event["data"]
    context.storage.objects[(data["bucket"], data["object_key"])] = content
    context.vector_store.error = VectorStoreError("offline", retryable=True)
    assert (await send_event(client, context.settings, event)).status_code == 202
    worker = context.worker()

    assert await worker.run_once() is True

    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
        document = await session.scalar(select(SourceDocument))
        embeddings = (await session.scalars(select(ChunkEmbedding))).all()
    assert run is not None
    assert document is not None
    assert run.status == "retry_wait"
    assert document.active_processing_run_id is None
    assert {embedding.status for embedding in embeddings} == {EMBEDDING_RETRY_WAIT}
    assert len(context.extractor.calls) == 1

    context.vector_store.error = None
    await asyncio.sleep(0.02)
    assert await worker.run_once() is True

    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
        document = await session.scalar(select(SourceDocument))
    assert run is not None
    assert document is not None
    assert run.status == RUN_READY
    assert run.embedded_chunk_count == 2
    assert document.active_processing_run_id == run.id
    assert len(context.extractor.calls) == 1
    assert len(context.vector_store.points) == 2


async def test_worker_backfills_active_chunks_without_reextracting(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    content = b"source"
    event = ready_event(content)
    data = event["data"]
    context.storage.objects[(data["bucket"], data["object_key"])] = content
    assert (await send_event(client, context.settings, event)).status_code == 202
    assert await context.worker().run_once() is True
    assert len(context.extractor.calls) == 1

    async with context.session_factory() as session:
        await session.execute(delete(ChunkEmbedding))
        await session.commit()
    context.vector_store.points.clear()

    assert await context.worker().run_once() is True

    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
        embeddings = (await session.scalars(select(ChunkEmbedding))).all()
    assert run is not None
    assert run.status == RUN_READY
    assert run.embedded_chunk_count == 2
    assert len(embeddings) == 2
    assert {embedding.status for embedding in embeddings} == {EMBEDDING_INDEXED}
    assert len(context.extractor.calls) == 1
    assert len(context.vector_store.points) == 2
