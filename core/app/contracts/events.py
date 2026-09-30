from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

DOCUMENT_READY_EVENT_TYPE = "document.ready"
DOCUMENT_DELETED_EVENT_TYPE = "document.deleted"
PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class DocumentReadyData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    workspace_id: UUID
    bucket: str = Field(min_length=1, max_length=255)
    object_key: str = Field(min_length=1, max_length=1024)
    original_filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return normalized


class DocumentDeletedData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    workspace_id: UUID
    sha256: str

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return DocumentReadyData.validate_sha256(value)


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    source: str
    subject: str
    schema_version: Literal[1]
    occurred_at: datetime


class DocumentReadyEvent(EventEnvelope):
    type: Literal["document.ready"]
    data: DocumentReadyData


class DocumentDeletedEvent(EventEnvelope):
    type: Literal["document.deleted"]
    data: DocumentDeletedData


DocumentEvent = Annotated[
    DocumentReadyEvent | DocumentDeletedEvent,
    Field(discriminator="type"),
]
DOCUMENT_EVENT_ADAPTER: TypeAdapter[DocumentEvent] = TypeAdapter(DocumentEvent)
