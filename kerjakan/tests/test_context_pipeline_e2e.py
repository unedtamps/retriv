import asyncio
import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import uuid4

import boto3
import httpx
import pytest
from anyio import to_thread
from pydantic import SecretStr
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import OUTBOX_PROCESSING, OUTBOX_PUBLISHED, OutboxEvent
from app.events.contracts import (
    DOCUMENT_DELETED_EVENT_TYPE,
    DOCUMENT_READY_EVENT_TYPE,
    EVENT_SOURCE,
    DocumentDeletedData,
    DocumentDeletedEvent,
    DocumentReadyData,
    DocumentReadyEvent,
)
from outbox.relay import OutboxRelay
from outbox.settings import OutboxSettings
from tests.document_samples import pdf_bytes

RUN_E2E = os.getenv("RUN_CONTEXT_E2E") == "1"
pytestmark = pytest.mark.skipif(not RUN_E2E, reason="Set RUN_CONTEXT_E2E=1")


async def test_source_relay_to_context_pipeline_and_deletion() -> None:
    source_database_url = required_environment("TEST_SOURCE_POSTGRES_URL")
    internal_database_url = required_environment("TEST_INTERNAL_POSTGRES_URL")
    context_url = required_environment("TEST_CONTEXT_URL").rstrip("/")
    webhook_secret = required_environment("TEST_CONTEXT_WEBHOOK_SECRET")
    webhook_key_id = required_environment("TEST_CONTEXT_WEBHOOK_KEY_ID")
    api_token = required_environment("TEST_CONTEXT_API_TOKEN")
    s3_endpoint = required_environment("TEST_S3_ENDPOINT_URL")
    s3_access_key = required_environment("TEST_S3_ACCESS_KEY")
    s3_secret_key = required_environment("TEST_S3_SECRET_KEY")
    raw_bucket = os.getenv("TEST_S3_RAW_BUCKET", "raw-documents")

    workspace_id = uuid4()
    document_id = uuid4()
    ready_event_id = uuid4()
    deleted_event_id = uuid4()
    source = pdf_bytes()
    source_sha256 = hashlib.sha256(source).hexdigest()
    object_key = (
        f"workspaces/{workspace_id}/objects/{source_sha256}/{document_id}"
    )
    s3 = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=s3_access_key,
        aws_secret_access_key=s3_secret_key,
        region_name="us-east-1",
    )
    source_engine = create_async_engine(source_database_url, pool_pre_ping=True)
    internal_engine = create_async_engine(internal_database_url, pool_pre_ping=True)
    source_sessions = async_sessionmaker(source_engine, expire_on_commit=False)
    relay = OutboxRelay(
        source_sessions,
        OutboxSettings(
            environment="test",
            database_url=source_database_url,
            target_url=f"{context_url}/internal/events",
            signing_secret=SecretStr(webhook_secret),
            signing_key_id=webhook_key_id,
        ),
    )
    try:
        await to_thread.run_sync(
            partial(
                s3.put_object,
                Bucket=raw_bucket,
                Key=object_key,
                Body=source,
                ContentType="application/pdf",
                Metadata={"sha256": source_sha256},
            )
        )
        ready = DocumentReadyEvent(
            id=ready_event_id,
            type=DOCUMENT_READY_EVENT_TYPE,
            source=EVENT_SOURCE,
            subject=f"document/{document_id}",
            schema_version=1,
            occurred_at=datetime.now(UTC),
            data=DocumentReadyData(
                document_id=document_id,
                workspace_id=workspace_id,
                bucket=raw_bucket,
                object_key=object_key,
                original_filename="e2e-report.pdf",
                media_type="application/pdf",
                size_bytes=len(source),
                sha256=source_sha256,
            ),
        )
        async with httpx.AsyncClient(timeout=30) as client:
            await deliver(relay, client, source_sessions, ready)
            status_url = (
                f"{context_url}/internal/workspaces/{workspace_id}/documents/"
                f"{document_id}/status"
            )
            headers = {"Authorization": f"Bearer {api_token}"}
            ready_status = await wait_for_status(
                client,
                status_url,
                headers,
                lambda payload: payload.get("active_processing_run_id") is not None,
            )
            assert ready_status["runs"][-1]["status"] == "ready"
            chunks = await client.get(
                status_url.removesuffix("/status") + "/chunks", headers=headers
            )
            assert chunks.status_code == 200
            assert chunks.json()["total"] > 0

            deleted = DocumentDeletedEvent(
                id=deleted_event_id,
                type=DOCUMENT_DELETED_EVENT_TYPE,
                source=EVENT_SOURCE,
                subject=f"document/{document_id}",
                schema_version=1,
                occurred_at=datetime.now(UTC),
                data=DocumentDeletedData(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    sha256=source_sha256,
                ),
            )
            await deliver(relay, client, source_sessions, deleted)
            deleted_status = await wait_for_status(
                client,
                status_url,
                headers,
                lambda payload: payload.get("deleted_at") is not None
                and payload["runs"][-1]["job_kind"] == "delete"
                and payload["runs"][-1]["status"] == "ready",
            )
            assert deleted_status["active_processing_run_id"] is None
    finally:
        await to_thread.run_sync(
            partial(s3.delete_object, Bucket=raw_bucket, Key=object_key)
        )
        async with source_sessions() as session:
            await session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.id.in_((ready_event_id, deleted_event_id))
                )
            )
            await session.commit()
        async with internal_engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM source_documents "
                    "WHERE workspace_id = :workspace_id AND document_id = :document_id"
                ),
                {"workspace_id": workspace_id, "document_id": document_id},
            )
            await connection.execute(
                text(
                    "DELETE FROM event_inbox "
                    "WHERE event_id IN (:ready_event_id, :deleted_event_id)"
                ),
                {
                    "ready_event_id": ready_event_id,
                    "deleted_event_id": deleted_event_id,
                },
            )
        await source_engine.dispose()
        await internal_engine.dispose()


async def deliver(
    relay: OutboxRelay,
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    event: DocumentReadyEvent | DocumentDeletedEvent,
) -> None:
    lease_token = uuid4()
    stored = OutboxEvent(
        id=event.id,
        event_type=event.type,
        aggregate_type="document",
        aggregate_id=event.data.document_id,
        workspace_id=event.data.workspace_id,
        payload=event.model_dump(mode="json"),
        status=OUTBOX_PROCESSING,
        attempts=1,
        lease_until=datetime.now(UTC) + timedelta(seconds=60),
        lease_token=lease_token,
    )
    async with session_factory() as session:
        session.add(stored)
        await session.commit()
    await relay._deliver(client, stored)
    async with session_factory() as session:
        published = await session.get(OutboxEvent, event.id)
    assert published is not None
    assert published.status == OUTBOX_PUBLISHED


async def wait_for_status(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 180
    last_payload: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(url, headers=headers)
        if response.status_code == 200:
            last_payload = response.json()
            if predicate(last_payload):
                return last_payload
        await asyncio.sleep(1)
    pytest.fail(f"Timed out waiting for pipeline state; last payload: {last_payload}")


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.fail(f"{name} is required when RUN_CONTEXT_E2E=1")
    return value
