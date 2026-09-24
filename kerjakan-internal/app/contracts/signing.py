import hashlib
import hmac
import time
from collections.abc import Mapping
from uuid import UUID

from app.core.errors import AppError


def signature_payload(timestamp: int, event_id: UUID, body: bytes) -> bytes:
    return f"{timestamp}.{event_id}.".encode() + body


def calculate_signature(secret: str, timestamp: int, event_id: UUID, body: bytes) -> str:
    digest = hmac.new(
        secret.encode(), signature_payload(timestamp, event_id, body), hashlib.sha256
    ).hexdigest()
    return f"v1={digest}"


def verify_webhook_signature(
    *,
    body: bytes,
    event_id: UUID,
    key_id: str,
    timestamp_value: str,
    supplied_signature: str,
    secrets: Mapping[str, str],
    max_age_seconds: int,
    current_timestamp: int | None = None,
) -> None:
    try:
        timestamp = int(timestamp_value)
    except ValueError as exc:
        raise signature_error() from exc

    now = int(time.time()) if current_timestamp is None else current_timestamp
    if abs(now - timestamp) > max_age_seconds:
        raise AppError(
            status_code=401,
            code="webhook_timestamp_expired",
            title="Webhook timestamp expired",
            detail="The webhook timestamp is outside the accepted time window.",
        )

    secret = secrets.get(key_id)
    if secret is None:
        raise signature_error()
    expected = calculate_signature(secret, timestamp, event_id, body)
    if not hmac.compare_digest(expected, supplied_signature):
        raise signature_error()


def signature_error() -> AppError:
    return AppError(
        status_code=401,
        code="invalid_webhook_signature",
        title="Invalid webhook signature",
        detail="The webhook signature could not be validated.",
    )
