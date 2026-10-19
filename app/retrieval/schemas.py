from uuid import UUID

from pydantic import BaseModel


class ContextSource(BaseModel):
    workspace_id: UUID
    document_id: UUID
    processing_run_id: UUID
    chunk_id: UUID
    ordinal: int
    score: float
    content_hash: str
    filename: str | None
    section_path: list[str]
    # Canonical key of section_path. Empty for sources that carry no section at
    # all, such as CSV rows, which section completion never reaches.
    section_key: str = ""
    is_heading_only: bool = False
    page_numbers: list[int]
    text: str
    token_count: int = 0
