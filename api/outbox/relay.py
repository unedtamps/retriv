import asyncio
import logging
import signal
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_PENDING,
    OUTBOX_PROCESSING,
    OUTBOX_PUBLISHED,
    OutboxEvent,
)
from outbox.settings import OutboxSettings
from outbox.signing import serialize_payload, sign_request

logger = logging.getLogger(__name__)
RETRYABLE_HTTP_STATUS_MIN: Final = 500


def utcnow() -> datetime:
    return datetime.now(UTC)


class OutboxRelay:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: OutboxSettings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def run_once(self, client: httpx.AsyncClient) -> int:
        await self._recover_expired_leases()
        events = await self._claim_batch()
        for event in events:
            await self._deliver(client, event)
        await self._cleanup_published()
        return len(events)

    async def run(self, client: httpx.AsyncClient, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                processed = await self.run_once(client)
            except Exception:
                logger.exception("Outbox relay iteration failed")
                processed = 0

            if processed > 0:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._settings.poll_interval_seconds
                )
            except TimeoutError:
                continue

    async def _recover_expired_leases(self) -> None:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.status == OUTBOX_PROCESSING,
                    OutboxEvent.lease_until.is_not(None),
                    OutboxEvent.lease_until < now,
                    OutboxEvent.attempts >= self._settings.max_attempts,
                )
                .values(
                    status=OUTBOX_DEAD_LETTER,
                    lease_until=None,
                    lease_token=None,
                    last_error="Delivery lease expired after maximum attempts",
                )
            )
            await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.status == OUTBOX_PROCESSING,
                    OutboxEvent.lease_until.is_not(None),
                    OutboxEvent.lease_until < now,
                    OutboxEvent.attempts < self._settings.max_attempts,
                )
                .values(
                    status=OUTBOX_PENDING,
                    available_at=now,
                    lease_until=None,
                    lease_token=None,
                    last_error="Delivery lease expired before completion",
                )
            )

    async def _claim_batch(self) -> list[OutboxEvent]:
        now = utcnow()
        lease_until = now + timedelta(seconds=self._settings.lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.status == OUTBOX_PENDING,
                        OutboxEvent.available_at <= now,
                    )
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    .limit(self._settings.batch_size)
                    .with_for_update(skip_locked=True)
                )
                events = list(result.all())
                for event in events:
                    event.status = OUTBOX_PROCESSING
                    event.attempts += 1
                    event.lease_until = lease_until
                    event.lease_token = uuid4()
            return events

    async def _deliver(self, client: httpx.AsyncClient, event: OutboxEvent) -> None:
        lease_token = event.lease_token
        if lease_token is None:
            logger.error(
                "Claimed outbox event has no lease token",
                extra={"event_id": str(event.id)},
            )
            return

        body = serialize_payload(event.payload)
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": str(event.id),
            "X-Kerjakan-Key-Id": self._settings.signing_key_id,
            "X-Kerjakan-Timestamp": str(timestamp),
            "X-Kerjakan-Signature": sign_request(
                secret=self._settings.signing_secret.get_secret_value(),
                timestamp=timestamp,
                event_id=event.id,
                body=body,
            ),
        }
        try:
            response = await client.post(
                self._settings.target_url,
                headers=headers,
                content=body,
            )
        except httpx.RequestError as exc:
            await self._mark_retry(event.id, lease_token, f"{type(exc).__name__}: {exc}")
            return

        if 200 <= response.status_code < 300:
            await self._mark_published(event.id, lease_token)
            logger.info("Published outbox event", extra={"event_id": str(event.id)})
            return

        if (
            response.status_code < RETRYABLE_HTTP_STATUS_MIN
            and response.status_code != 429
        ):
            await self._mark_dead_letter(
                event.id,
                lease_token,
                f"Target returned non-retryable HTTP {response.status_code}",
            )
            return

        await self._mark_retry(
            event.id, lease_token, f"Target returned HTTP {response.status_code}"
        )

    async def _mark_published(self, event_id: UUID, lease_token: UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.status == OUTBOX_PROCESSING,
                    OutboxEvent.lease_token == lease_token,
                )
                .values(
                    status=OUTBOX_PUBLISHED,
                    lease_until=None,
                    lease_token=None,
                    last_error=None,
                    published_at=utcnow(),
                )
                .returning(OutboxEvent.id)
            )
        updated = result.scalar_one_or_none() == event_id
        if not updated:
            logger.info("Ignored stale outbox publish", extra={"event_id": str(event_id)})
        return updated

    async def _mark_retry(self, event_id: UUID, lease_token: UUID, error: str) -> bool:
        async with self._session_factory() as session, session.begin():
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.status == OUTBOX_PROCESSING,
                    OutboxEvent.lease_token == lease_token,
                )
                .with_for_update()
            )
            if event is None:
                logger.info("Ignored stale outbox retry", extra={"event_id": str(event_id)})
                return False
            event.last_error = error[:2000]
            event.lease_until = None
            event.lease_token = None
            if event.attempts >= self._settings.max_attempts:
                event.status = OUTBOX_DEAD_LETTER
                return True
            exponent = min(event.attempts - 1, 20)
            delay = min(
                self._settings.retry_base_seconds * (2**exponent),
                self._settings.retry_max_seconds,
            )
            event.status = OUTBOX_PENDING
            event.available_at = utcnow() + timedelta(seconds=delay)
            return True

    async def _mark_dead_letter(
        self, event_id: UUID, lease_token: UUID, error: str
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.status == OUTBOX_PROCESSING,
                    OutboxEvent.lease_token == lease_token,
                )
                .values(
                    status=OUTBOX_DEAD_LETTER,
                    lease_until=None,
                    lease_token=None,
                    last_error=error[:2000],
                )
                .returning(OutboxEvent.id)
            )
        updated = result.scalar_one_or_none() == event_id
        if not updated:
            logger.info("Ignored stale outbox dead letter", extra={"event_id": str(event_id)})
        return updated

    async def _cleanup_published(self) -> None:
        cutoff = utcnow() - timedelta(days=self._settings.retention_days)
        async with self._session_factory() as session:
            await session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.status == OUTBOX_PUBLISHED,
                    OutboxEvent.published_at.is_not(None),
                    OutboxEvent.published_at < cutoff,
                )
            )
            await session.commit()


async def run_relay(settings: OutboxSettings) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    relay = OutboxRelay(session_factory, settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(stop_signal, stop_event.set)

    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds
        ) as client:
            await relay.run(client, stop_event)
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = OutboxSettings()
    asyncio.run(run_relay(settings))
