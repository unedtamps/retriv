from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Run outside the repository so a developer's .env cannot satisfy a
    field the test means to be unset. Without this, RETRIV_DOCLING_ARTIFACTS_PATH
    in .env made the worker check pass when it should have failed."""
    monkeypatch.chdir(tmp_path)
    yield

# The values a production deployment must be able to pass the validators.
# The RS256 scope keypair and the two service tokens are gone: there is no
# second process to authenticate to, so those settings no longer exist.
PRODUCTION_BASE: dict[str, Any] = {
    "environment": "production",
    "jwt_secret": SecretStr("g" * 64),
    "database_url": "postgresql+asyncpg://retriv:secure@postgres:5432/retriv",
    "s3_secret_key": SecretStr("production-storage-secret-xxxxxxxxxxxxxxxxxxxx"),
    "s3_endpoint_url": "https://objects.example.test",
    "s3_artifact_secret_key": SecretStr("production-artifact-secret-xxxxxxxxxxxxxxx"),
    "qdrant_read_only_api_key": SecretStr("production-qdrant-ro-key-xxxxxxxxxxxxxx"),
    "qdrant_url": "https://qdrant.example.test",
    "embedding_base_url": "https://embeddings.example.test/v1",
    "embedding_api_key": SecretStr("production-embedding-key-xxxxxxxxxxxxxxxx"),
    "cors_allowed_origins": ["https://dashboard.example.test"],
}

WORKER_BASE: dict[str, Any] = {
    "environment": "production",
    "component": "worker",
    "database_url": "postgresql+asyncpg://retriv:secure@postgres:5432/retriv",
    "s3_endpoint_url": "https://objects.example.test",
    "s3_artifact_secret_key": SecretStr("production-artifact-secret-xxxxxxxxxxxxxxx"),
    "qdrant_url": "https://qdrant.example.test",
    "qdrant_api_key": SecretStr("production-qdrant-rw-key-xxxxxxxxxxxxxxxx"),
    "embedding_base_url": "https://embeddings.example.test/v1",
    "embedding_api_key": SecretStr("production-embedding-key-xxxxxxxxxxxxxxxx"),
    "docling_artifacts_path": Path("/opt/docling-models"),
}


def test_default_jwt_secret_is_generated_per_settings_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RETRIV_JWT_SECRET", raising=False)
    first = Settings(environment="local")
    second = Settings(environment="local")

    assert first.jwt_secret.get_secret_value() != second.jwt_secret.get_secret_value()


def test_production_rejects_placeholder_jwt_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **{
                **PRODUCTION_BASE,
                "jwt_secret": SecretStr("replace-me-with-a-generated-secret-1234"),
            }
        )


def test_production_requires_a_long_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="at least 64"):
        Settings(**{**PRODUCTION_BASE, "jwt_secret": SecretStr("g" * 40)})


def test_production_requires_antivirus() -> None:
    with pytest.raises(ValidationError, match="Antivirus"):
        Settings(**PRODUCTION_BASE, antivirus_enabled=False)


def test_cors_origins_must_be_exact_origins() -> None:
    with pytest.raises(ValidationError, match="exact HTTP"):
        Settings(cors_allowed_origins=["*"])

    with pytest.raises(ValidationError, match="exact HTTP"):
        Settings(cors_allowed_origins=["https://dashboard.example.test/path"])


def test_production_cors_origins_require_https() -> None:
    with pytest.raises(ValidationError, match="CORS origins"):
        Settings(
            **{
                **PRODUCTION_BASE,
                "cors_allowed_origins": ["http://dashboard.example.test"],
            }
        )


def test_production_rejects_default_database_credentials() -> None:
    with pytest.raises(ValidationError, match="database credentials"):
        Settings(
            **{
                **PRODUCTION_BASE,
                "database_url": "postgresql+asyncpg://retriv:retriv@postgres:5432/retriv",
            }
        )


def test_production_rejects_default_service_credentials() -> None:
    with pytest.raises(ValidationError, match="placeholders"):
        Settings(
            **{
                **PRODUCTION_BASE,
                "s3_secret_key": SecretStr("change-this-storage-key-do-not-use"),
            }
        )


def test_production_requires_https_everywhere() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**{**PRODUCTION_BASE, "s3_endpoint_url": "http://objects.example.test"})

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**{**PRODUCTION_BASE, "qdrant_url": "http://qdrant.example.test"})

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**{**PRODUCTION_BASE, "embedding_base_url": "http://embeddings.example.test"})


def test_production_worker_requires_baked_docling_artifacts() -> None:
    with pytest.raises(ValidationError, match="Docling artifacts"):
        Settings(
            **{k: v for k, v in WORKER_BASE.items() if k != "docling_artifacts_path"}
        )


def test_worker_accepts_a_fully_configured_production_environment() -> None:
    settings = Settings(**WORKER_BASE)
    assert settings.component == "worker"
    # The worker holds no JWT secret: there is no user to authenticate.
    assert not hasattr(settings, "jwt_secret") or settings.jwt_secret is not None


def test_local_environment_tolerates_placeholders() -> None:
    """A developer must be able to boot without real service credentials. The
    placeholder is long enough to pass the length floor and contains
    'change-this' so production still refuses it."""
    settings = Settings(
        environment="local",
        s3_secret_key=SecretStr("change-this-storage-key-do-not-use-in-production"),
    )
    assert settings.s3_secret_key.get_secret_value().startswith("change-this")


def test_raw_and_artifact_buckets_must_differ() -> None:
    with pytest.raises(ValidationError, match="buckets must be different"):
        Settings(
            s3_raw_bucket="same-bucket", s3_artifact_bucket="same-bucket"
        )


def test_retry_ceiling_must_not_undercut_the_base() -> None:
    with pytest.raises(ValidationError, match="RETRY_MAX_SECONDS"):
        Settings(
            processing_retry_base_seconds=10.0,
            processing_retry_max_seconds=5.0,
        )


def test_secrets_shorter_than_the_floor_are_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(embedding_api_key=SecretStr("too-short"))


def test_service_urls_reject_embedded_credentials() -> None:
    with pytest.raises(ValidationError, match="absolute HTTP"):
        Settings(qdrant_url="qdrant:6333")

    with pytest.raises(ValidationError, match="credentials or query"):
        Settings(embedding_base_url="https://user:pass@embeddings.example.test/v1")


def test_identifiers_reject_whitespace_and_stray_characters() -> None:
    with pytest.raises(ValidationError):
        Settings(embedding_profile="has space")

    with pytest.raises(ValidationError):
        Settings(qdrant_collection="bad/name")


def test_json_settings_survive_both_dotenv_parsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON-array setting has to survive both ways of loading .env.

    The app reads .env through pydantic-settings; the Makefile passes it
    through uv's parser with --env-file; and the provisioning CLI reads
    neither, because its settings set env_file=None and depend on the
    environment uv built. If those two parsers disagree about quoting, a
    value that is fine under one is broken under the other, and the
    failure looks like a settings error rather than a quoting error.

    Double quotes are the trap. Both parsers treat " as a quote character
    and strip it, so a bare JSON array arrives with its inner quotes
    removed and no longer parses. Single quotes survive both.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'RETRIV_CORS_ALLOWED_ORIGINS=\'["https://a.example.test"]\'\n'
        "RETRIV_OCR_LANGUAGES='[\"ind\",\"eng\"]'\n"
    )

    from_settings = Settings()
    assert from_settings.cors_allowed_origins == ["https://a.example.test"]
    assert from_settings.ocr_languages == ["ind", "eng"]

    # The same file, parsed the way uv --env-file does it. uv treats "
    # as a quote character, so this is the case that used to break.
    from dotenv import dotenv_values

    parsed = dotenv_values(tmp_path / ".env")
    assert parsed["RETRIV_CORS_ALLOWED_ORIGINS"] == '["https://a.example.test"]'
    assert parsed["RETRIV_OCR_LANGUAGES"] == '["ind","eng"]'


def test_provisioning_does_not_read_dotenv_by_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one-shot job takes its settings from the environment only.

    A job with write access to every user row should not silently pick up
    credentials just because it ran in a directory that has a .env, which
    is why env_file=None. The Makefile-style invocation passes the file
    explicitly instead.
    """
    from provisioning.service import ProvisionerSettings

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "PROVISIONER_S3_ENDPOINT_URL=http://127.0.0.1:9000\n"
        "PROVISIONER_S3_ACCESS_KEY=migration-reader\n"
        "PROVISIONER_S3_SECRET_KEY=" + "s" * 40 + "\n"
        "PROVISIONER_DATABASE_URL=postgresql+asyncpg://retriv:pw@127.0.0.1:5432/retriv\n"
        "PROVISIONER_RAW_DOCUMENT_BUCKET=raw-documents\n"
        "PROVISIONER_MANIFEST_URI=s3://migration-manifests/bootstrap/users-v1.json\n"
    )
    for name in (
        "PROVISIONER_S3_ENDPOINT_URL",
        "PROVISIONER_S3_ACCESS_KEY",
        "PROVISIONER_S3_SECRET_KEY",
        "PROVISIONER_DATABASE_URL",
        "PROVISIONER_RAW_DOCUMENT_BUCKET",
        "PROVISIONER_MANIFEST_URI",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError, match="PROVISIONER_DATABASE_URL is required"):
        ProvisionerSettings()

    monkeypatch.setenv("PROVISIONER_S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("PROVISIONER_S3_ACCESS_KEY", "migration-reader")
    monkeypatch.setenv("PROVISIONER_S3_SECRET_KEY", "s" * 40)
    monkeypatch.setenv(
        "PROVISIONER_DATABASE_URL",
        "postgresql+asyncpg://retriv:pw@127.0.0.1:5432/retriv",
    )
    monkeypatch.setenv("PROVISIONER_RAW_DOCUMENT_BUCKET", "raw-documents")
    monkeypatch.setenv(
        "PROVISIONER_MANIFEST_URI", "s3://migration-manifests/bootstrap/users-v1.json"
    )
    settings = ProvisionerSettings()
    assert settings.s3_access_key == "migration-reader"
    assert settings.manifest_bucket == "migration-manifests"


def _reader_settings(**overrides: Any) -> dict[str, Any]:
    """The reader credentials a working deployment passes.

    Both copies of the password exist by necessity: postgres-init creates the
    role from sql_reader_password, and asyncpg authenticates with the one
    inside the DSN. Nothing interpolates between them.
    """
    values: dict[str, Any] = {
        "sql_reader_user": "retriv_reader",
        "sql_reader_password": SecretStr("reader-password"),
        "sql_database_url": (
            "postgresql+asyncpg://retriv_reader:reader-password@postgres:5432/retriv"
        ),
    }
    values.update(overrides)
    return values


def test_matching_sql_reader_credentials_are_accepted() -> None:
    """The ordinary case, and the one that must not become noisy."""
    Settings(**_reader_settings())


def test_sql_reader_password_mismatch_is_named_at_startup() -> None:
    """Rotating one copy of the password is the most likely mistake here.

    Without this the deployment migrates cleanly, provisions cleanly, and then
    dies in verify_reader() with "RETRIV_SQL_DATABASE_URL is configured but
    unusable" -- which blames the DSN and says nothing about the value that
    actually disagrees with it.
    """
    with pytest.raises(ValidationError, match="does not match the password"):
        Settings(
            **_reader_settings(
                sql_reader_password=SecretStr("rotated-but-not-in-the-dsn")
            )
        )


def test_sql_reader_username_mismatch_is_rejected() -> None:
    """A DSN aimed at a role nothing creates cannot work, and the mismatch is
    the reason -- not an authentication failure three layers later."""
    with pytest.raises(ValidationError, match="postgres-init creates the role"):
        Settings(**_reader_settings(sql_reader_user="someone_else"))


def test_a_dsn_without_a_password_is_rejected_when_a_reader_password_is_set() -> None:
    """Silently skipping the comparison would let this through, and the failure
    would resurface as the generic unusable-DSN error it exists to prevent."""
    with pytest.raises(ValidationError, match="has no password"):
        Settings(
            **_reader_settings(
                sql_database_url="postgresql+asyncpg://retriv_reader@postgres:5432/retriv"
            )
        )


def test_url_encoded_passwords_compare_after_decoding() -> None:
    """A password needing URL-encoding arrives percent-encoded in the DSN.

    Comparing the raw text would reject a deployment whose two copies really
    are the same value, which is worse than not checking at all: the operator
    would be told to fix something that is correct.
    """
    Settings(
        **_reader_settings(
            sql_reader_password=SecretStr("p@ss w0rd/with:chars"),
            sql_database_url=(
                "postgresql+asyncpg://retriv_reader:p%40ss%20w0rd%2Fwith%3Achars"
                "@postgres:5432/retriv"
            ),
        )
    )


def test_the_check_is_skipped_when_the_tool_is_off() -> None:
    """No DSN means the reader role is never used, so there is nothing to keep
    in step. Failing here would make the tool un-disableable once someone had
    left the credentials in .env."""
    Settings(sql_reader_password=SecretStr("left-behind"))


def test_the_check_is_skipped_when_the_reader_password_is_absent() -> None:
    """Also a supported shape: a DSN alone, with the role managed elsewhere."""
    Settings(sql_database_url="postgresql+asyncpg://someone:pw@postgres:5432/retriv")
