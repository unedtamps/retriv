from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    type: str
    source: str
    subject: str
    schema_version: int
    occurred_at: datetime
    data: dict[str, Any]


app = FastAPI(title="Dummy Event Receiver", version="0.1.0")
received_events: dict[UUID, dict[str, Any]] = {}


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events", status_code=202)
async def receive_event(event: EventEnvelope) -> dict[str, Any]:
    duplicate = event.id in received_events
    if not duplicate:
        received_events[event.id] = event.model_dump(mode="json")
    return {"accepted": True, "duplicate": duplicate, "event_id": str(event.id)}


@app.get("/events")
async def list_events() -> dict[str, Any]:
    events = list(received_events.values())
    return {"items": events, "total": len(events)}


@app.delete("/events")
async def clear_events() -> dict[str, int]:
    count = len(received_events)
    received_events.clear()
    return {"deleted": count}
