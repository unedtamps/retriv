from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import CurrentUserResponse, TokenResponse
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

router = APIRouter(prefix="/v1/auth", tags=["authentication"])


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
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(access_token=token, expires_in=expires_in)
