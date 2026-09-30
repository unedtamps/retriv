import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_PENDING,
    OUTBOX_PROCESSING,
    OUTBOX_PUBLISHED,
    OutboxEvent,
)
from outbox.relay import OutboxRelay
from outbox.settings import OutboxSettings

SIGNING_SECRET = "test-outbox-signing-secret-xxxxxxxxxxxxxxxxxxxxxxxx"
SIGNING_KEY_ID = "test-outbox-v1"


def make_event() -> OutboxEvent:
    document_id = uuid4()
    workspace_id = uuid4()
    event_id = uuid4()
    return OutboxEvent(
        id=event_id,
        event_type="document.ready",
        aggregate_type="document",
        aggregate_id=document_id,
        workspace_id=workspace_id,
        payload={
            "id": str(event_id),
            "type": "document.ready",
            "source": "retriv-api",
            "subject": f"document/{document_id}",
            "schema_version": 1,
            "occurred_at": datetime.now(UTC).isoformat(),
            "data": {
                "document_id": str(document_id),
                "workspace_id": str(workspace_id),
                "bucket": "raw-documents",
                "object_key": "workspaces/example/document",
                "original_filename": "report.pdf",
                "media_type": "application/pdf",
                "size_bytes": 10,
                "sha256": "a" * 64,
            },
        },
    )


def relay_settings(**overrides: Any) -> OutboxSettings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": "sqlite+aiosqlite://",
        "target_url": "http://dummy/events",
        "signing_secret": SIGNING_SECRET,
        "signing_key_id": SIGNING_KEY_ID,
        "retry_base_seconds": 0.01,
        "retry_max_seconds": 0.01,
    }
    values.update(overrides)
    return OutboxSettings(**values)


async def add_event(factory: async_sessionmaker[AsyncSession]) -> OutboxEvent:
    event = make_event()
    async with factory() as session:
        session.add(event)
        await session.commit()
    return event


@pytest.mark.asyncio
async def test_relay_signs_and_sends_exact_serialized_payload(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = await add_event(db_session_factory)
    received: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(202, json={"accepted": True})

    relay = OutboxRelay(db_session_factory, relay_settings())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await relay.run_once(client) == 1

    async with db_session_factory() as session:
        stored = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == event.id))

    assert stored is not None
    assert stored.status == OUTBOX_PUBLISHED
    assert stored.attempts == 1
    request = received[0]
    expected_body = json.dumps(
        event.payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    timestamp = request.headers["X-Retriv-Timestamp"]
    expected_signature = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        f"{timestamp}.{event.id}.".encode() + expected_body,
        hashlib.sha256,
    ).hexdigest()

    assert request.content == expected_body
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Idempotency-Key"] == str(event.id)
    assert request.headers["X-Retriv-Key-Id"] == SIGNING_KEY_ID
    assert int(timestamp) > 0
    assert request.headers["X-Retriv-Signature"] == f"v1={expected_signature}"


def test_outbox_settings_store_and_validate_signing_secret() -> None:
    settings = relay_settings()

    assert isinstance(settings.signing_secret, SecretStr)
    assert settings.signing_secret.get_secret_value() == SIGNING_SECRET

    with pytest.raises(ValidationError):
        relay_settings(signing_secret="x" * 31)


def test_outbox_settings_reject_blank_signing_key_id() -> None:
    with pytest.raises(ValidationError):
        relay_settings(signing_key_id="   ")


def test_outbox_production_requires_https_target() -> None:
    assert relay_settings(target_url="http://dummy/events").environment == "test"

    with pytest.raises(ValidationError, match="must use HTTPS in production"):
        relay_settings(environment="production", target_url="http://dummy/events")

    settings = relay_settings(
        environment="production",
        target_url="https://receiver.example/events",
        database_url="postgresql+asyncpg://relay:strong-password@postgres/ingestion",
        signing_secret="production-signing-secret-xxxxxxxxxxxxxxxxxxxxxxxx",
    )
    assert settings.target_url == "https://receiver.example/events"


def test_outbox_request_timeout_must_fit_inside_lease() -> None:
    with pytest.raises(ValidationError, match="shorter than the lease"):
        relay_settings(request_timeout_seconds=60, lease_seconds=60)


def test_outbox_production_rejects_placeholder_signing_secret() -> None:
    with pytest.raises(ValidationError, match="SIGNING_SECRET"):
        relay_settings(
            environment="production",
            target_url="https://receiver.example/events",
            database_url="postgresql+asyncpg://relay:strong-password@postgres/ingestion",
            signing_secret="change-this-signing-secret-xxxxxxxxxxxxxxxxxxxx",
        )


@pytest.mark.asyncio
async def test_relay_retries_transient_http_failure(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = await add_event(db_session_factory)

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503)

    relay = OutboxRelay(db_session_factory, relay_settings())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await relay.run_once(client) == 1

    async with db_session_factory() as session:
        stored = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == event.id))

    assert stored is not None
    assert stored.status == OUTBOX_PENDING
    assert stored.attempts == 1
    assert stored.last_error == "Target returned HTTP 503"


@pytest.mark.asyncio
async def test_relay_dead_letters_non_retryable_response(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = await add_event(db_session_factory)

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(422)

    relay = OutboxRelay(db_session_factory, relay_settings(max_attempts=1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await relay.run_once(client) == 1

    async with db_session_factory() as session:
        stored = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == event.id))

    assert stored is not None
    assert stored.status == OUTBOX_DEAD_LETTER
    assert stored.last_error == "Target returned non-retryable HTTP 422"


@pytest.mark.asyncio
async def test_stale_worker_cannot_publish_reclaimed_event(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = await add_event(db_session_factory)
    relay = OutboxRelay(db_session_factory, relay_settings())
    first_claim = (await relay._claim_batch())[0]
    first_token = first_claim.lease_token
    assert first_token is not None

    async with db_session_factory() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    await relay._recover_expired_leases()
    second_claim = (await relay._claim_batch())[0]
    second_token = second_claim.lease_token
    assert second_token is not None
    assert second_token != first_token

    assert await relay._mark_published(event.id, first_token) is False
    async with db_session_factory() as session:
        stored = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == event.id))
    assert stored is not None
    assert stored.status == OUTBOX_PROCESSING
    assert stored.lease_token == second_token

    assert await relay._mark_published(event.id, second_token) is True
    async with db_session_factory() as session:
        stored = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == event.id))
    assert stored is not None
    assert stored.status == OUTBOX_PUBLISHED
    assert stored.lease_token is None


@pytest.mark.asyncio
async def test_expired_lease_at_max_attempts_is_dead_lettered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = await add_event(db_session_factory)
    relay = OutboxRelay(db_session_factory, relay_settings(max_attempts=1))
    claimed = (await relay._claim_batch())[0]
    assert claimed.attempts == 1
    assert claimed.lease_token is not None

    async with db_session_factory() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    await relay._recover_expired_leases()
    async with db_session_factory() as session:
        stored = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == event.id))

    assert stored is not None
    assert stored.status == OUTBOX_DEAD_LETTER
    assert stored.attempts == 1
    assert stored.lease_until is None
    assert stored.lease_token is None
    assert stored.last_error == "Delivery lease expired after maximum attempts"
    assert await relay._claim_batch() == []
