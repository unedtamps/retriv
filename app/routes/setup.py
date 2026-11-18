from datetime import UTC, datetime
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import SetupAdminRequest, SetupStatusResponse, TokenResponse
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import SystemState, User
from app.db.session import get_session
from app.security.auth import create_access_token, hash_password
from app.security.rate_limit import LoginRateLimiter, get_login_rate_limiter
from app.security.refresh_tokens import issue_refresh_token

router = APIRouter(prefix="/v1/setup", tags=["setup"])

NO_STORE = {"Cache-Control": "no-store"}


def setup_completed_error() -> AppError:
    return AppError(
        status_code=409,
        code="setup_already_completed",
        title="Setup already completed",
        detail="An administrator account already exists.",
    )


def setup_disabled_error() -> AppError:
    return AppError(
        status_code=403,
        code="setup_disabled",
        title="Web setup is disabled",
        detail=(
            "Set RETRIV_BOOTSTRAP_ADMIN_USERNAME and "
            "RETRIV_BOOTSTRAP_ADMIN_PASSWORD, then restart the API."
        ),
    )


def username_taken_error() -> AppError:
    return AppError(
        status_code=409,
        code="username_taken",
        title="Username already taken",
        detail="That username is already in use.",
    )


@router.get("/status", response_model=SetupStatusResponse)
async def setup_status(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SetupStatusResponse:
    admin = await session.scalar(select(User.id).where(User.role == "admin").limit(1))
    state = await session.get(SystemState, 1)
    needs_admin = admin is None and (state is None or state.admin_created_at is None)
    return SetupStatusResponse(
        needs_admin=needs_admin,
        setup_allowed=settings.allow_web_setup,
    )


@router.post(
    "/admin",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_first_admin(
    payload: SetupAdminRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    rate_limiter: Annotated[LoginRateLimiter, Depends(get_login_rate_limiter)],
) -> TokenResponse:
    if not settings.allow_web_setup:
        raise setup_disabled_error()
    if len(payload.password.encode("utf-8")) > settings.max_password_bytes:
        raise AppError(
            status_code=422,
            code="password_too_long",
            title="Password is too long",
            detail=f"The password cannot exceed {settings.max_password_bytes} bytes.",
        )

    client_host = request.client.host if request.client is not None else "unknown"
    rate_key = f"setup:{client_host}"
    retry_after = await rate_limiter.retry_after(rate_key)
    if retry_after is not None:
        raise AppError(
            status_code=429,
            code="setup_rate_limited",
            title="Too many setup attempts",
            detail="Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        async with session.begin():
            state = await session.get(SystemState, 1, with_for_update=True)
            if state is None:
                state = SystemState(id=1)
                session.add(state)
                await session.flush()
            if state.admin_created_at is not None:
                raise setup_completed_error()

            existing_admin = await session.scalar(
                select(User.id).where(User.role == "admin").limit(1)
            )
            if existing_admin is not None:
                raise setup_completed_error()

            taken = await session.scalar(
                select(User.id).where(User.username == payload.username).limit(1)
            )
            if taken is not None:
                raise username_taken_error()

            password_hash = await to_thread.run_sync(hash_password, payload.password)
            admin = User(
                username=payload.username,
                password_hash=password_hash,
                role="admin",
                is_active=True,
            )
            session.add(admin)
            await session.flush()

            token, expires_in = create_access_token(admin, settings)
            refresh = await issue_refresh_token(session, admin, settings)
            state.admin_created_at = datetime.now(UTC)
    except AppError:
        await rate_limiter.record_failure(rate_key)
        raise
    except IntegrityError as exc:
        await session.rollback()
        await rate_limiter.record_failure(rate_key)
        raise username_taken_error() from exc

    await rate_limiter.reset(rate_key)
    response.headers.update(NO_STORE)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        refresh_token=refresh.plaintext,
        refresh_expires_in=refresh.expires_in,
    )
