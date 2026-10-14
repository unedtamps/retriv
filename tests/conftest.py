from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.db.models import User, Workspace, WorkspaceMembership
from app.db.session import Base, get_session
from app.infrastructure.scanner import MalwareScanner, get_malware_scanner
from app.infrastructure.storage import (
    ObjectNotFoundError,
    ObjectStorage,
    ReadableObject,
    StorageError,
    get_object_storage,
)
from app.main import create_app
from app.security.auth import hash_password


class InMemoryObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict[str, str]] = {}
        self.presigned: list[tuple[str, str, int]] = []
        self.upload_error = False
        self.open_error = False
        self.delete_error = False
        self.presign_error = False

    def upload_file(
        self,
        path: Path,
        *,
        bucket: str,
        key: str,
        content_type: str,
        sha256: str,
    ) -> None:
        if self.upload_error:
            raise StorageError("Injected upload failure")
        self.objects[(bucket, key)] = path.read_bytes()
        self.metadata[(bucket, key)] = {"content_type": content_type, "sha256": sha256}

    def open_object(self, *, bucket: str, key: str) -> ReadableObject:
        if self.open_error:
            raise StorageError("Injected open failure")
        try:
            return BytesIO(self.objects[(bucket, key)])
        except KeyError as exc:
            raise ObjectNotFoundError("Object does not exist") from exc

    def presign_get(self, *, bucket: str, key: str, expires_in: int) -> str:
        """A stand-in for a SigV4 URL, with the shape the tool depends on.

        The expiry and signature parameters are real in the URL a test sees,
        so a test can assert the tool handed out a time-limited address rather
        than a permanent one. No signature is actually computed.
        """
        self.presigned.append((bucket, key, expires_in))
        if self.presign_error:
            raise StorageError("Injected presign failure")
        if (bucket, key) not in self.objects:
            raise ObjectNotFoundError("Object does not exist")
        return (
            f"https://objects.example.test/{bucket}/{key}"
            f"?X-Amz-Expires={expires_in}&X-Amz-Signature=stub"
        )

    def delete_object(self, *, bucket: str, key: str) -> None:
        if self.delete_error:
            raise StorageError("Injected delete failure")
        self.objects.pop((bucket, key), None)
        self.metadata.pop((bucket, key), None)

    def check_health(self, *, bucket: str) -> None:
        del bucket


class CleanScanner:
    error: Exception | None = None

    def scan_file(self, path: Path) -> None:
        del path
        if self.error is not None:
            raise self.error

    def check_health(self) -> None:
        return


@dataclass(frozen=True)
class SeededEntities:
    user: User
    second_user: User
    admin_user: User
    finance: Workspace
    legal: Workspace


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        jwt_secret=SecretStr(
            "test-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        ),
        s3_endpoint_url="http://unused",
        s3_access_key="test",
        s3_secret_key=SecretStr("test-s3-secret-xxxxxxxxxxxxxxxxxx"),
        s3_artifact_secret_key=SecretStr("test-artifact-secret-xxxxxxxxxxxxxx"),
        s3_raw_bucket="raw-documents",
        s3_artifact_bucket="context-documents",
        qdrant_read_only_api_key=SecretStr("test-qdrant-read-only-key-xxxxxxxxxx"),
        antivirus_enabled=False,
        upload_temp_dir=tmp_path,
        max_upload_bytes=1024 * 1024,
        max_pdf_pages=10,
    )


@pytest_asyncio.fixture
async def db_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # SQLite ignores foreign keys unless asked, so ON DELETE CASCADE is a
    # no-op by default. Without this the delete tests would pass while
    # proving nothing about the cascade the schema relies on to take the
    # chunks and the embedding ledger with the document.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


@pytest.fixture
def object_storage() -> InMemoryObjectStorage:
    return InMemoryObjectStorage()


@pytest.fixture
def scanner() -> CleanScanner:
    return CleanScanner()


@pytest.fixture
def app(
    settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    object_storage: ObjectStorage,
    scanner: MalwareScanner,
) -> Iterator[Any]:
    application = create_app(settings)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_object_storage] = lambda: object_storage
    application.dependency_overrides[get_malware_scanner] = lambda: scanner
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest_asyncio.fixture
async def entities(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> SeededEntities:
    user = User(
        username="alice",
        password_hash=hash_password("correct-horse-battery-staple"),
        role="user",
    )
    second_user = User(
        username="bob",
        password_hash=hash_password("another-correct-password"),
        role="auditor",
    )
    admin_user = User(
        username="admin",
        password_hash=hash_password("admin-correct-password"),
        role="admin",
    )
    finance = Workspace(slug="finance", name="Finance")
    legal = Workspace(slug="legal", name="Legal")
    async with db_session_factory() as session:
        session.add_all([user, second_user, admin_user, finance, legal])
        await session.flush()
        session.add_all(
            [
                WorkspaceMembership(
                    user_id=user.id,
                    workspace_id=finance.id,
                    role="owner",
                    granted_by_user_id=user.id,
                ),
                WorkspaceMembership(
                    user_id=user.id,
                    workspace_id=legal.id,
                    role="owner",
                    granted_by_user_id=user.id,
                ),
                WorkspaceMembership(
                    user_id=second_user.id,
                    workspace_id=legal.id,
                    role="viewer",
                    granted_by_user_id=user.id,
                ),
            ]
        )
        await session.commit()
    return SeededEntities(
        user=user,
        second_user=second_user,
        admin_user=admin_user,
        finance=finance,
        legal=legal,
    )


@pytest_asyncio.fixture
async def auth_headers(client: httpx.AsyncClient, entities: SeededEntities) -> dict[str, str]:
    del entities
    response = await client.post(
        "/v1/auth/token",
        data={"username": "alice", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
