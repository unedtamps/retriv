import hashlib
import json
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.db.models import User, Workspace
from app.security.auth import verify_password
from provisioning.models import ProvisioningRun
from provisioning.service import (
    ManifestConflictError,
    ProvisionerSettings,
    ProvisioningError,
    apply_manifest,
    parse_manifest,
    read_manifest_object,
)


def manifest_payload(
    *,
    migration_id: str = "bootstrap-1",
    username: str = "admin",
    password: str = "correct-password-123",
    role: str = "admin",
    is_active: bool = True,
    workspace_name: str = "Finance",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "migration_id": migration_id,
        "users": [
            {
                "username": username,
                "password": password,
                "role": role,
                "is_active": is_active,
            }
        ],
        "workspaces": [
            {
                "slug": "finance",
                "name": workspace_name,
                "description": "Finance workspace",
            }
        ],
    }


def make_manifest(payload: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    _, digest = parse_manifest(raw)
    return raw, digest


@pytest.mark.asyncio
async def test_apply_manifest_is_idempotent(
    db_session_factory: Any,
) -> None:
    manifest_payload_bytes, digest = make_manifest(manifest_payload())

    first = await apply_manifest(
        db_session_factory,
        manifest_payload=manifest_payload_bytes,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )
    replay = await apply_manifest(
        db_session_factory,
        manifest_payload=manifest_payload_bytes,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )

    assert first.users_created == 1
    assert first.workspaces_created == 1
    assert not first.already_applied
    assert replay.already_applied
    assert replay.users_created == 1
    assert replay.workspaces_created == 1

    async with db_session_factory() as session:
        assert (await session.scalar(select(User).where(User.username == "admin"))) is not None
        assert (
            await session.scalar(select(Workspace).where(Workspace.slug == "finance"))
        ) is not None
        assert (
            await session.scalar(
                select(ProvisioningRun).where(ProvisioningRun.migration_id == "bootstrap-1")
            )
        ) is not None


@pytest.mark.asyncio
async def test_new_manifest_id_upserts_existing_records(
    db_session_factory: Any,
) -> None:
    first_payload, first_digest = make_manifest(manifest_payload())
    await apply_manifest(
        db_session_factory,
        manifest_payload=first_payload,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )

    updated_payload, updated_digest = make_manifest(
        manifest_payload(
            migration_id="update-2",
            password="new-correct-password-123",
            role="auditor",
            is_active=False,
            workspace_name="Updated Finance",
        )
    )
    result = await apply_manifest(
        db_session_factory,
        manifest_payload=updated_payload,
        source_uri="s3://migration-manifests/bootstrap/users-v2.json",
    )

    assert result.users_updated == 1
    assert result.workspaces_updated == 1
    async with db_session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        workspace = await session.scalar(select(Workspace).where(Workspace.slug == "finance"))
        assert user is not None
        assert workspace is not None
        assert user.role == "auditor"
        assert not user.is_active
        assert verify_password("new-correct-password-123", user.password_hash)
        assert workspace.name == "Updated Finance"


@pytest.mark.asyncio
async def test_same_migration_id_with_different_payload_is_rejected(
    db_session_factory: Any,
) -> None:
    first_payload, first_digest = make_manifest(manifest_payload())
    await apply_manifest(
        db_session_factory,
        manifest_payload=first_payload,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )
    different_payload, different_digest = make_manifest(
        manifest_payload(password="different-password-123")
    )

    with pytest.raises(ManifestConflictError):
        await apply_manifest(
            db_session_factory,
            manifest_payload=different_payload,
            source_uri="s3://migration-manifests/bootstrap/users-v1.json",
        )


def test_parse_manifest_rejects_duplicates_and_unknown_fields() -> None:
    duplicate_payload = manifest_payload()
    duplicate_payload["users"].append(
        {
            "username": " ADMIN ",
            "password": "another-password-123",
            "role": "user",
        }
    )
    with pytest.raises(ProvisioningError):
        parse_manifest(json.dumps(duplicate_payload).encode())

    unknown_field_payload = manifest_payload()
    unknown_field_payload["unexpected"] = True
    with pytest.raises(ProvisioningError):
        parse_manifest(json.dumps(unknown_field_payload).encode())


def test_read_manifest_object_enforces_bucket_and_size(object_storage: Any) -> None:
    payload = json.dumps(manifest_payload()).encode()
    object_storage.objects[("migration-manifests", "bootstrap/users-v1.json")] = payload

    loaded_payload = read_manifest_object(
        object_storage,
        uri="s3://migration-manifests/bootstrap/users-v1.json",
        bucket="migration-manifests",
        prefix="bootstrap/",
        max_bytes=len(payload),
    )
    manifest, digest = parse_manifest(loaded_payload)

    assert manifest.migration_id == "bootstrap-1"
    assert digest == hashlib.sha256(payload).hexdigest()
    with pytest.raises(ProvisioningError):
        read_manifest_object(
            object_storage,
            uri="s3://raw-documents/bootstrap/users-v1.json",
            bucket="migration-manifests",
            prefix="bootstrap/",
            max_bytes=len(payload),
        )
    with pytest.raises(ProvisioningError):
        read_manifest_object(
            object_storage,
            uri="s3://migration-manifests/other/users-v1.json",
            bucket="migration-manifests",
            prefix="bootstrap/",
            max_bytes=len(payload),
        )


def test_provisioner_settings_reject_insecure_production_storage() -> None:
    with pytest.raises(ValidationError):
        ProvisionerSettings(
            environment="production",
            database_url="postgresql+asyncpg://provisioner:secret@postgres:5432/ingestion",
            s3_endpoint_url="http://minio:9000",
            s3_access_key="reader",
            s3_secret_key=SecretStr("reader-secret"),
            raw_document_bucket="raw-documents",
            manifest_bucket="migration-manifests",
            manifest_uri="s3://migration-manifests/bootstrap/users-v1.json",
        )
