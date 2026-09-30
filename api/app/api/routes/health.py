from functools import partial
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import ScannerDependency, StorageDependency
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.session import get_session
from app.infrastructure.scanner import ScannerUnavailableError
from app.infrastructure.storage import StorageError

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    storage: StorageDependency,
    scanner: ScannerDependency,
) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
        await to_thread.run_sync(partial(storage.check_health, bucket=settings.s3_bucket))
        await to_thread.run_sync(scanner.check_health)
    except (SQLAlchemyError, StorageError, ScannerUnavailableError, OSError) as exc:
        raise AppError(
            status_code=503,
            code="service_not_ready",
            title="Service not ready",
            detail="One or more required services are unavailable.",
        ) from exc
    return {"status": "ok"}
