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
    """Report ready only when every dependency can actually serve traffic.

    The vector extension is checked because this process answers questions
    from it: a process that reports ready and then 503s on the first chat is
    worse than one that stays unready. The check is one SELECT against the
    same session the query path uses, so it also proves the database is
    migrated.
    """
    try:
        await session.execute(text("SELECT 1"))
        await check_vector_extension(session)
        await to_thread.run_sync(partial(storage.check_health, bucket=settings.s3_raw_bucket))
        await to_thread.run_sync(scanner.check_health)
    except (SQLAlchemyError, StorageError, ScannerUnavailableError, OSError) as exc:
        raise service_not_ready_error() from exc
    return {"status": "ok"}


async def check_vector_extension(session: AsyncSession) -> None:
    installed = await session.scalar(
        text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    )
    if installed != 1:
        raise SQLAlchemyError("The pgvector extension is not installed")


def service_not_ready_error() -> AppError:
    return AppError(
        status_code=503,
        code="service_not_ready",
        title="Service not ready",
        detail="One or more required services are unavailable.",
    )
