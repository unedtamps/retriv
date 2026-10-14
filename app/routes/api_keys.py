from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyListResponse,
    ApiKeySummary,
)
from app.core.errors import AppError
from app.db.models import ApiKey
from app.db.session import get_session
from app.security.api_keys import mint_api_key, revoke_api_key
from app.security.auth import CurrentUser

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])

MAX_KEYS_PER_USER = 20


@router.get("", response_model=ApiKeyListResponse)
async def list_api_keys(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ApiKeyListResponse:
    """The caller's own keys, active first. Never returns key material."""
    result = await session.scalars(
        select(ApiKey)
        .where(ApiKey.user_id == current_user.id)
        .order_by(ApiKey.revoked_at.is_(None).desc(), ApiKey.created_at.desc())
    )
    return ApiKeyListResponse(
        items=[api_key_summary(record) for record in result.all()]
    )


@router.post("", response_model=ApiKeyCreatedResponse, status_code=201)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> ApiKeyCreatedResponse:
    """Mint a key. The plaintext is in this response and nowhere else.

    The row count cap exists so a runaway script cannot fill the table with
    credentials nobody will ever use.
    """
    active_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.user_id == current_user.id, ApiKey.revoked_at.is_(None))
        )
        or 0
    )
    if active_count >= MAX_KEYS_PER_USER:
        raise AppError(
            status_code=409,
            code="api_key_limit_reached",
            title="API key limit reached",
            detail=(
                f"Each user may hold {MAX_KEYS_PER_USER} active keys. "
                "Revoke an existing key before creating another."
            ),
        )

    minted = await mint_api_key(
        session, user_id=current_user.id, name=payload.name
    )
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return ApiKeyCreatedResponse(
        id=minted.record.id,
        name=minted.record.name,
        key_prefix=minted.record.key_prefix,
        key=minted.plaintext,
        created_at=minted.record.created_at,
        warning=(
            "This is the only time the key is shown. Store it now; "
            "it cannot be recovered later."
        ),
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key_route(
    key_id: UUID,
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Revoke one of the caller's own keys. Idempotent per active key."""
    revoked = await revoke_api_key(
        session, key_id=key_id, user_id=current_user.id
    )
    if not revoked:
        # 404 for both "not yours" and "already revoked": the caller learns
        # nothing about keys they do not own.
        raise AppError(
            status_code=404,
            code="api_key_not_found",
            title="API key not found",
            detail="No active API key with that ID exists for this account.",
        )
    await session.commit()
    return Response(status_code=204)


def api_key_summary(record: ApiKey) -> ApiKeySummary:
    return ApiKeySummary(
        id=record.id,
        name=record.name,
        key_prefix=record.key_prefix,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
    )
