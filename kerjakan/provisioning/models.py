from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ProvisioningRun(Base):
    __tablename__ = "provisioning_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    migration_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    source_uri: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
