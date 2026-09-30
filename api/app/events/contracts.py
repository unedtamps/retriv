from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

DOCUMENT_READY_EVENT_TYPE: Literal["document.ready"] = "document.ready"
DOCUMENT_DELETED_EVENT_TYPE: Literal["document.deleted"] = "document.deleted"
EVENT_SOURCE = "retriv-api"


class DocumentReadyData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    workspace_id: UUID
    bucket: str
    object_key: str
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str


class DocumentReadyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    type: Literal["document.ready"]
    source: str
    subject: str
    schema_version: int
    occurred_at: datetime
    data: DocumentReadyData


class DocumentDeletedData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    workspace_id: UUID
    sha256: str


class DocumentDeletedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    type: Literal["document.deleted"]
    source: str
    subject: str
    schema_version: Literal[1]
    occurred_at: datetime
    data: DocumentDeletedData
