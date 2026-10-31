"""Minting, rotating, and revoking refresh tokens.

The model is the OAuth rotating-refresh-token one, because the alternative --
a single long-lived credential -- cannot be revoked per session and cannot
tell a replayed copy from a legitimate use.

* Every refresh mints a new token and revokes the presented one.
* All tokens descended from one login share a ``family_id``.
* Presenting a token that was already rotated revokes the entire family. A
  revoked token can only resurface if a copy of it exists, and at that point
  there is no way to tell which holder is the original, so the session ends
  and the legitimate user logs in again.

Only the SHA-256 hash is persisted. The plaintext is returned once, in the
response that minted it.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import RefreshToken, User
from app.security.auth import authentication_error

TOKEN_SCHEME = "rt"  # noqa: S105 - a credential prefix, not a credential
TOKEN_BODY_LENGTH = 43
MAX_ACTIVE_FAMILIES = 10

_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_refresh_token() -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(TOKEN_BODY_LENGTH))
    return f"{TOKEN_SCHEME}_{body}"


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_refresh_token(value: str) -> bool:
    return (
        value.startswith(f"{TOKEN_SCHEME}_")
        and len(value) == len(TOKEN_SCHEME) + 1 + TOKEN_BODY_LENGTH
    )


@dataclass(frozen=True)
class IssuedRefreshToken:
    plaintext: str
    expires_in: int


def refresh_token_ttl(settings: Settings) -> timedelta:
    return timedelta(days=settings.refresh_token_days)


async def issue_refresh_token(
    session: AsyncSession,
    user: User,
    settings: Settings,
    *,
    family_id: UUID | None = None,
) -> IssuedRefreshToken:
    """Mint a token for this user, starting a new family unless one is given."""
    plaintext = generate_refresh_token()
    ttl = refresh_token_ttl(settings)
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id or uuid4(),
            token_hash=hash_refresh_token(plaintext),
            auth_version=user.auth_version,
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + ttl,
        )
    )
    await session.flush()
    return IssuedRefreshToken(plaintext=plaintext, expires_in=int(ttl.total_seconds()))


async def rotate_refresh_token(
    session: AsyncSession, token: str, settings: Settings
) -> tuple[User, IssuedRefreshToken]:
    """Exchange a refresh token for a new one, or refuse.

    Every refusal is the same 401, because distinguishing "unknown" from
    "expired" from "already used" tells an attacker which of their guesses
    was a real token.

    Commits before returning: the caller's next request may rotate the token
    this call just issued, and that would fail against an uncommitted row.
    """
    record = await _find_token(session, token)

    if record.revoked_at is not None:
        await revoke_family(session, record.family_id)
        await session.commit()
        raise authentication_error()

    if _is_expired(record):
        await session.commit()
        raise authentication_error()

    user = await session.get(User, record.user_id)
    if (
        user is None
        or not user.is_active
        or user.auth_version != record.auth_version
    ):
        await revoke_family(session, record.family_id)
        await session.commit()
        raise authentication_error()

    record.revoked_at = datetime.now(UTC)
    issued = await issue_refresh_token(session, user, settings, family_id=record.family_id)
    await session.commit()
    return user, issued


async def revoke_refresh_token(session: AsyncSession, token: str) -> None:
    """End the session this token belongs to. Unknown tokens are not an error.

    Idempotent on purpose: signing out twice, or with a token the server has
    already forgotten, must not surface as a failure, and must not tell the
    caller whether the token ever existed.
    """
    if not looks_like_refresh_token(token):
        return
    record = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(token))
    )
    if record is None:
        return
    await revoke_family(session, record.family_id)
    await session.commit()


async def revoke_family(session: AsyncSession, family_id: UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def purge_expired(session: AsyncSession, user_id: UUID) -> None:
    """Drop this user's dead rows so the table tracks live sessions only."""
    await session.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.expires_at < datetime.now(UTC),
        )
    )


async def trim_active_families(
    session: AsyncSession, user_id: UUID, *, keep: int = MAX_ACTIVE_FAMILIES
) -> None:
    """Cap concurrent sessions, revoking the oldest.

    A client that logs in repeatedly without ever signing out would otherwise
    accumulate live chains that no one is watching. The newest ``keep``
    families survive; everything older is revoked.

    Grouped by family rather than ordered with ``DISTINCT ON``: the tests run
    on SQLite, which has no such clause.
    """
    newest_per_family = (
        select(
            RefreshToken.family_id.label("family_id"),
            func.max(RefreshToken.created_at).label("last_seen"),
        )
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .group_by(RefreshToken.family_id)
        .subquery()
    )
    families = (
        await session.execute(
            select(newest_per_family.c.family_id).order_by(newest_per_family.c.last_seen.desc())
        )
    ).scalars()
    for family_id in list(families)[keep:]:
        await revoke_family(session, family_id)


async def _find_token(session: AsyncSession, token: str) -> RefreshToken:
    if not looks_like_refresh_token(token):
        raise authentication_error()
    record = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(token))
    )
    if record is None:
        raise authentication_error()
    return record


def _is_expired(record: RefreshToken) -> bool:
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)
