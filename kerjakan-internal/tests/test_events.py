import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
from pydantic import SecretStr
from sqlalchemy import func, select

from app.db.models import RUN_UNSUPPORTED, EventInbox, ProcessingRun
from tests.conftest import ServiceContext, deleted_event, ready_event, send_event


async def test_accepts_signed_event_and_deduplicates_delivery(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    event = ready_event(b"pdf-content")

    first = await send_event(client, context.settings, event)
    second = await send_event(client, context.settings, event)

    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    async with context.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EventInbox)) == 1
        assert await session.scalar(select(func.count()).select_from(ProcessingRun)) == 1


async def test_rejects_tampered_body(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    event = ready_event(b"pdf-content")
    signed_body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    event["data"]["original_filename"] = "tampered.pdf"
    tampered_body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()

    response = await send_event(
        client,
        context.settings,
        event,
        body=tampered_body,
        signature_body=signed_body,
    )
    assert response.status_code == 401


async def test_rejects_expired_signature(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    event = ready_event(b"pdf-content")
    expired = int(datetime.now(UTC).timestamp()) - 1000

    response = await send_event(client, context.settings, event, timestamp=expired)

    assert response.status_code == 401
    assert response.json()["code"] == "webhook_timestamp_expired"


async def test_same_event_id_with_different_payload_conflicts(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    event = ready_event(b"pdf-content")
    assert (await send_event(client, context.settings, event)).status_code == 202
    event["data"]["original_filename"] = "changed.pdf"

    response = await send_event(client, context.settings, event)

    assert response.status_code == 409
    assert response.json()["code"] == "event_payload_conflict"


async def test_known_but_unsupported_format_is_durable_business_outcome(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    event = ready_event(
        b"spreadsheet",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="budget.xlsx",
    )

    response = await send_event(client, context.settings, event)

    assert response.status_code == 202
    async with context.session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
    assert run is not None
    assert run.status == RUN_UNSUPPORTED


async def test_accepts_rotating_previous_signing_key(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    previous_secret = "previous-webhook-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    context.settings.webhook_previous_key_id = "previous"
    context.settings.webhook_previous_secret = SecretStr(previous_secret)
    event = ready_event(b"pdf-content")

    response = await send_event(
        client,
        context.settings,
        event,
        secret=previous_secret,
        key_id="previous",
    )

    assert response.status_code == 202


async def test_rejects_event_body_before_buffering_beyond_limit(
    client: httpx.AsyncClient,
) -> None:
    event_id = "6d9f6a9d-1b8c-4d5f-8c3d-8a8f2d7b4c11"
    response = await client.post(
        "/internal/events",
        content=b"x" * (65 * 1024),
        headers={
            "Idempotency-Key": event_id,
            "X-Kerjakan-Key-Id": "irrelevant",
            "X-Kerjakan-Timestamp": "1",
            "X-Kerjakan-Signature": "v1=invalid",
        },
    )

    assert response.status_code == 413


async def test_ready_event_after_deletion_retains_only_replay_hash(
    client: httpx.AsyncClient, context: ServiceContext
) -> None:
    ready = ready_event(b"pdf-content")
    deleted = deleted_event(ready)
    assert (await send_event(client, context.settings, deleted)).status_code == 202

    response = await send_event(client, context.settings, ready)

    assert response.status_code == 202
    async with context.session_factory() as session:
        inbox = await session.get(EventInbox, UUID(ready["id"]))
        run_count = await session.scalar(select(func.count()).select_from(ProcessingRun))
    assert inbox is not None
    assert inbox.payload == {"redacted": True}
    assert run_count == 1
