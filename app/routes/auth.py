from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CurrentUserResponse,
    RefreshRequest,
    RefreshTokenResponse,
    TokenResponse,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.session import get_session
from app.security.auth import (
    CurrentUser,
    authenticate_user,
    authentication_error,
    create_access_token,
)
from app.security.rate_limit import LoginRateLimiter, get_login_rate_limiter
from app.security.refresh_tokens import (
    issue_refresh_token,
    purge_expired,
    revoke_refresh_token,
    rotate_refresh_token,
    trim_active_families,
)

router = APIRouter(prefix="/v1/auth", tags=["authentication"])

NO_STORE = {"Cache-Control": "no-store"}


@router.get("/me", response_model=CurrentUserResponse)
async def get_current_user_profile(current_user: CurrentUser) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=current_user.id,
        username=current_user.username,
        role=current_user.role,
        is_active=current_user.is_active,
    )


@router.post("/token", response_model=TokenResponse)
async def issue_access_token(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    rate_limiter: Annotated[LoginRateLimiter, Depends(get_login_rate_limiter)],
) -> TokenResponse:
    normalized_username = form.username.strip().lower()
    client_host = request.client.host if request.client is not None else "unknown"
    rate_limit_key = f"{client_host}:{normalized_username}"
    retry_after = await rate_limiter.retry_after(rate_limit_key)
    if retry_after is not None:
        raise AppError(
            status_code=429,
            code="login_rate_limited",
            title="Too many login attempts",
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        if (
            len(normalized_username) > 255
            or len(form.password.encode("utf-8")) > settings.max_password_bytes
        ):
            raise authentication_error()
        user = await authenticate_user(session, normalized_username, form.password)
    except AppError:
        await rate_limiter.record_failure(rate_limit_key)
        raise

    await rate_limiter.reset(rate_limit_key)
    token, expires_in = create_access_token(user, settings)
    await purge_expired(session, user.id)
    await trim_active_families(session, user.id)
    refresh = await issue_refresh_token(session, user, settings)
    await session.commit()
    response.headers.update(NO_STORE)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        refresh_token=refresh.plaintext,
        refresh_expires_in=refresh.expires_in,
    )


@router.post("/refresh", response_model=RefreshTokenResponse)
async def refresh_access_token(
    payload: RefreshRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RefreshTokenResponse:
    """Exchange a refresh token for a fresh pair.

    Rotating: the presented token dies here and a new one is returned. The
    client must persist the replacement or the session ends.
    """
    user, refresh = await rotate_refresh_token(session, payload.refresh_token, settings)
    token, expires_in = create_access_token(user, settings)
    response.headers.update(NO_STORE)
    return RefreshTokenResponse(
        access_token=token,
        expires_in=expires_in,
        refresh_token=refresh.plaintext,
        refresh_expires_in=refresh.expires_in,
    )


@router.post("/logout", status_code=204)
async def sign_out(
    payload: RefreshRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """End the session the refresh token belongs to.

    Always 204, including for a token the server has never seen: an error
    here would be a way to test guesses against the token store.
    """
    await revoke_refresh_token(session, payload.refresh_token)
    return Response(status_code=204, headers=NO_STORE)
