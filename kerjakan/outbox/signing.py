import hashlib
import hmac
import json
from typing import Any
from uuid import UUID


def serialize_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_request(
    *, secret: str, timestamp: int, event_id: UUID, body: bytes
) -> str:
    signing_input = f"{timestamp}.{event_id}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()
    return f"v1={digest}"
