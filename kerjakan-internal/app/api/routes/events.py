import hashlib
import json
import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import EventAcceptedResponse
from app.contracts.events import DOCUMENT_EVENT_ADAPTER
from app.contracts.signing import signature_error, verify_webhook_signature
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import EventInbox
from app.db.session import get_session
from app.processing.intake import accept_event

router = APIRouter(prefix="/internal/events", tags=["events"])
MAX_EVENT_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


@router.post("", response_model=EventAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def receive_event(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[str | None, Header()] = None,
    x_kerjakan_key_id: Annotated[str | None, Header()] = None,
    x_kerjakan_timestamp: Annotated[str | None, Header()] = None,
    x_kerjakan_signature: Annotated[str | None, Header()] = None,
) -> EventAcceptedResponse:
    if (
        idempotency_key is None
        or x_kerjakan_key_id is None
        or x_kerjakan_timestamp is None
        or x_kerjakan_signature is None
    ):
        raise signature_error()
    try:
        event_id = UUID(idempotency_key)
    except (TypeError, ValueError) as exc:
        raise signature_error() from exc

    body_parts: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > MAX_EVENT_BYTES:
            raise event_too_large_error()
        body_parts.append(chunk)
    body = b"".join(body_parts)
    if not body:
        raise event_too_large_error()

    verify_webhook_signature(
        body=body,
        event_id=event_id,
        key_id=x_kerjakan_key_id,
        timestamp_value=x_kerjakan_timestamp,
        supplied_signature=x_kerjakan_signature,
        secrets=settings.webhook_secrets(),
        max_age_seconds=settings.webhook_max_age_seconds,
    )

    try:
        payload: dict[str, Any] = json.loads(body)
        event = DOCUMENT_EVENT_ADAPTER.validate_python(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
        raise AppError(
            status_code=422,
            code="invalid_event_payload",
            title="Invalid event payload",
            detail="The webhook body does not match a supported event contract.",
        ) from exc
    if event.id != event_id:
        raise AppError(
            status_code=422,
            code="event_id_mismatch",
            title="Event ID mismatch",
            detail="Idempotency-Key must match the event ID.",
        )

    canonical_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    payload_sha256 = hashlib.sha256(canonical_payload).hexdigest()
    try:
        duplicate = await accept_event(
            session,
            event,
            payload=payload,
            payload_sha256=payload_sha256,
            settings=settings,
        )
    except IntegrityError as exc:
        await session.rollback()
        existing_event = await session.get(EventInbox, event.id)
        if existing_event is not None and existing_event.payload_sha256 == payload_sha256:
            return EventAcceptedResponse(duplicate=True, event_id=event.id)
        logger.exception(
            "Event intake integrity conflict", extra={"event_id": str(event.id)}
        )
        raise AppError(
            status_code=503,
            code="event_concurrency_conflict",
            title="Event concurrency conflict",
            detail="The event could not be accepted due to a transient concurrency conflict.",
        ) from exc
    return EventAcceptedResponse(duplicate=duplicate, event_id=event.id)


def event_too_large_error() -> AppError:
    return AppError(
        status_code=413,
        code="event_payload_too_large",
        title="Event payload too large",
        detail=f"Event payloads cannot exceed {MAX_EVENT_BYTES} bytes.",
    )
