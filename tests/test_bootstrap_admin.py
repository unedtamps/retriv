"""The first admin comes from the environment, on every API start.

The account is the only way into a fresh deployment, so the tests cover the
three states that matter: it is created when missing, it is left alone when
unchanged, and its password is replaced (with sessions invalidated) when the
environment value changes.
"""

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import User
from app.security.auth import hash_password, verify_password
from app.security.bootstrap import ensure_bootstrap_admin

PASSWORD = "correct-password-123"
NEW_PASSWORD = "different-password-123"


def configured(settings: Settings, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "bootstrap_admin_username": "admin",
        "bootstrap_admin_password": SecretStr(PASSWORD),
    }
    values.update(overrides)
    return settings.model_copy(update=values)


async def the_admin(
    sessions: async_sessionmaker[AsyncSession],
) -> User | None:
    async with sessions() as session:
        return await session.scalar(select(User).where(User.username == "admin"))


async def test_a_fresh_database_gets_the_bootstrap_admin(
    db_session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await ensure_bootstrap_admin(configured(settings), db_session_factory)

    user = await the_admin(db_session_factory)
    assert user is not None
    assert user.role == "admin"
    assert user.is_active is True
    assert user.auth_version == 0
    assert verify_password(PASSWORD, user.password_hash)


async def test_a_second_start_leaves_the_existing_row_alone(
    db_session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await ensure_bootstrap_admin(configured(settings), db_session_factory)
    first = await the_admin(db_session_factory)

    await ensure_bootstrap_admin(configured(settings), db_session_factory)
    second = await the_admin(db_session_factory)

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert second.password_hash == first.password_hash
    assert second.auth_version == 0
    async with db_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_a_changed_password_replaces_the_hash_and_signs_sessions_out(
    db_session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await ensure_bootstrap_admin(configured(settings), db_session_factory)

    await ensure_bootstrap_admin(
        configured(settings, bootstrap_admin_password=SecretStr(NEW_PASSWORD)),
        db_session_factory,
    )

    user = await the_admin(db_session_factory)
    assert user is not None
    assert user.auth_version == 1
    assert verify_password(NEW_PASSWORD, user.password_hash)
    assert not verify_password(PASSWORD, user.password_hash)


async def test_an_existing_non_admin_is_not_promoted(
    db_session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """The env chooses the password, but not the role of a row that exists."""
    async with db_session_factory() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("old-password-123"),
                role="user",
                is_active=False,
            )
        )
        await session.commit()

    await ensure_bootstrap_admin(configured(settings), db_session_factory)

    user = await the_admin(db_session_factory)
    assert user is not None
    assert user.role == "user"
    assert user.is_active is False
    assert user.auth_version == 1
    assert verify_password(PASSWORD, user.password_hash)


async def test_nothing_happens_when_the_pair_is_unset(
    db_session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    unset = settings.model_copy(
        update={
            "bootstrap_admin_username": "",
            "bootstrap_admin_password": SecretStr(""),
        }
    )
    await ensure_bootstrap_admin(unset, db_session_factory)

    async with db_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0
