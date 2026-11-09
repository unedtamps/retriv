import hmac
import re
from functools import lru_cache
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Deployment settings for the whole application.

    One process serves the API, the ingestion endpoints, and the answer
    harness, so there is one settings object and one RETRIV_ prefix. The
    worker is the only separate process and reads this same class with
    component="worker".
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="RETRIV_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Retriv"
    component: Literal["api", "worker"] = "api"
    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+asyncpg://retriv:retriv@localhost:7432/retriv"

    jwt_secret: SecretStr = Field(default_factory=lambda: SecretStr(token_urlsafe(48)))
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "retriv"
    jwt_audience: str = "retriv"
    access_token_minutes: int = Field(default=60, ge=1, le=1440)
    refresh_token_days: int = Field(default=30, ge=1, le=365)
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_window_seconds: int = Field(default=300, ge=1, le=3600)
    max_password_bytes: int = Field(default=1024, ge=64, le=8192)
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: SecretStr = SecretStr("")
    cors_allowed_origins: list[str] = [
        "http://127.0.0.1:9500",
        "http://localhost:9500",
    ]

    s3_endpoint_url: str | None = None
    s3_access_key: str = "retriv"
    s3_secret_key: SecretStr = SecretStr("development-s3-secret-placeholder")
    s3_raw_bucket: str = "raw-documents"
    s3_artifact_bucket: str = "context-documents"
    s3_region: str = "us-east-1"
    s3_addressing_style: Literal["auto", "path", "virtual"] = "auto"

    antivirus_enabled: bool = True
    clamd_host: str = "localhost"
    clamd_port: int = Field(default=4410, ge=1, le=65535)
    clamd_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    mcp_download_link_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    max_pdf_pages: int = Field(default=1_000, gt=0)
    """Pages one document may carry, and slides one deck may carry.

    Measured on the corpus: 410 pages cost ~4.3 minutes, ~750 MiB resident and
    ~1,390 chunks, so a thousand pages is about ten minutes of one worker's
    CPU. The hard ceiling is elsewhere -- ``processing_timeout_seconds`` is
    reached around 2,500 pages -- which is why this cap bounds a document's
    cost well below what the machine could technically chew through. It reads
    as a page count rather than a byte count because pages are what drive
    time, memory, vision calls and embedding spend at once.
    """
    max_archive_entries: int = Field(default=10_000, gt=0)
    max_archive_central_directory_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_archive_uncompressed_bytes: int = Field(default=300 * 1024 * 1024, gt=0)
    max_archive_entry_bytes: int = Field(default=150 * 1024 * 1024, gt=0)
    max_archive_compression_ratio: float = Field(default=100.0, gt=1)
    max_archive_entry_compression_ratio: float = Field(default=100.0, gt=1)
    max_office_xml_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_csv_rows: int = Field(default=1_000_000, gt=0)
    max_csv_columns: int = Field(default=512, ge=1, le=4096)
    max_csv_field_bytes: int = Field(default=64 * 1024, gt=0)
    max_json_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    """The JSON file or request-body cap.

    Kept below the general upload cap because stdlib JSON is parsed into a
    whole Python tree. A larger payload needs a streaming reader, not just a
    larger number."""
    max_json_depth: int = Field(default=64, ge=1, le=256)
    max_json_nodes: int = Field(default=2_000_000, gt=0)
    max_json_rows: int = Field(default=1_000_000, gt=0)
    max_json_tables: int = Field(default=20, ge=1, le=100)
    max_json_value_bytes: int = Field(default=64 * 1024, gt=0)
    max_json_row_bytes: int = Field(default=1024 * 1024, gt=0)
    max_xlsx_sheets: int = Field(default=64, gt=0)
    """Sheets one workbook may carry.

    A sheet is another table to detect, stream and store, and another
    catalogue entry the agent reads; past a few dozen the workbook is a
    database export that should have been split before it was uploaded."""
    max_xlsx_images: int = Field(default=20, gt=0)
    """Pictures and rendered charts one workbook may contribute.

    Every one of these becomes a vision call and an artifact object, so the
    cap bounds the cost of a workbook that embeds a logo on every sheet."""
    max_concurrent_uploads: int = Field(default=2, ge=1, le=16)
    request_read_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    upload_temp_dir: Path = Path("/tmp")  # noqa: S108 - container-scoped tmpfs

    max_source_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_chunks_per_document: int = Field(default=10_000, gt=0)
    max_artifact_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_extracted_text_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_chunk_payload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    pipeline_version: str = "2026.1"
    min_extraction_coverage: float = Field(default=0.6, gt=0, le=2)
    chunk_max_tokens: int = Field(default=700, ge=100, le=8192)
    chunk_tokenizer: str = "cl100k_base"
    native_scan_text_threshold: int = Field(default=32, ge=0)
    """A page whose own text is thinner than this is read as a scan."""

    processing_timeout_seconds: int = Field(default=3600, ge=30, le=86_400)
    parser_memory_limit_bytes: int = Field(default=4 * 1024 * 1024 * 1024, ge=512 * 1024 * 1024)
    """Virtual, not resident: the OCR engine's thread stacks and arenas count
    against it even though actual RAM stays under a gigabyte."""
    parser_max_processes: int = Field(default=4096, ge=16, le=32_768)
    """A per-user process ceiling: threads count against it too, so it stays
    high enough to exceed whatever else runs under the same account."""
    processing_temp_dir: Path = Path("/tmp")  # noqa: S108 - container-scoped tmpfs
    processing_poll_interval_seconds: float = Field(default=2.0, gt=0, le=3600)
    artifact_reconciliation_interval_seconds: int = Field(default=300, ge=1, le=86_400)
    processing_lease_seconds: int = Field(default=1800, ge=30, le=86_400)
    processing_max_attempts: int = Field(default=3, ge=1, le=100)
    processing_retry_base_seconds: float = Field(default=5.0, gt=0, le=3600)
    processing_retry_max_seconds: float = Field(default=300.0, gt=0, le=86_400)
    processing_max_concurrent_runs: int = Field(default=2, ge=1, le=8)
    parser_max_concurrent_extractions: int = Field(default=1, ge=1, le=4)

    embedding_base_url: str = "http://127.0.0.1:1234/v1"
    embedding_api_key: SecretStr = SecretStr("development-embedding-api-key-change-this")
    embedding_model: str = "text-embedding-qwen3-embedding-8b"
    embedding_profile: str = "qwen3-embedding-8b-2000-v1"
    embedding_dimensions: int = Field(default=2000, ge=1, le=65_536)
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    """How many chunks one embedding request carries.

    Request grouping is neutral on the local LM Studio endpoint: the same 16
    texts took ~2.12 s as one batch and ~2.14 s as 16 single requests. Keep a
    larger batch to reduce HTTP overhead; the per-text cost is driven by input
    length and the model, not by the batch size. A server that batches
    efficiently can raise this; verify it against the target server.
    """
    embedding_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    embedding_reconciliation_interval_seconds: int = Field(default=300, ge=1, le=86_400)

    image_processing_enabled: bool = True
    layout_model_path: str | None = None
    """The PP-DocLayout ONNX file that finds the figures on a scanned page.

    Empty means the package resolves and downloads its own copy on first use,
    the way the OCR models already arrive. The worker image bakes one in, so
    a deployed worker never reaches the network for it.
    """
    layout_min_score: float = Field(default=0.5, ge=0, le=1)
    """The layout model's confidence floor for calling a region a figure."""
    image_render_scale: float = Field(default=2.0, gt=0, le=8)
    image_min_pixels: int = Field(default=10_000, ge=0)
    max_image_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_total_image_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    vlm_base_url: str = "https://zenmux.ai/api/v1/chat/completions"
    vlm_api_key: SecretStr = SecretStr("development-vlm-api-key-change-this")
    vlm_model: str = "minimax/minimax-m3.1-flash"
    vlm_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    vlm_max_tokens: int = Field(default=8192, ge=256, le=131_072)
    vlm_max_tokens_ceiling: int = Field(default=131_072, ge=256, le=1_048_576)
    vlm_max_images_per_document: int = Field(default=100, ge=0, le=1_000)
    vlm_max_concurrency: int = Field(default=4, ge=1, le=32)
    vlm_max_description_chars: int = Field(default=1200, ge=100, le=8000)
    vlm_scan_page_describe: bool = True
    vlm_session_header: str = ""
    """Header a routing gateway wants on every request, e.g. the opencode
    zen gateway's ``x-opencode-session``. Empty means the endpoint needs
    none, which is true of a plain OpenAI-compatible endpoint."""
    vlm_session_id: str = ""
    """The value sent in that header. Sent only when both this and the
    header name are set, so an endpoint that rejects unknown headers is
    unaffected."""
    """Offer each scanned page to the vision model once, to catch figures."""
    model_wait_base_seconds: float = Field(default=5.0, gt=0, le=3600)
    model_wait_max_seconds: float = Field(default=60.0, gt=0, le=3600)
    model_wait_timeout_seconds: float = Field(default=3600.0, gt=0, le=86_400)

    max_context_chunks: int = Field(default=8, ge=1, le=20)
    min_relevance_score: float = Field(default=0.25, ge=-1.0, le=1.0)
    section_expand_chunks: int = Field(default=4, ge=0, le=20)
    max_context_sources: int = Field(default=12, ge=1, le=40)
    retrieval_candidate_multiplier: int = Field(default=8, ge=1, le=32)
    """How many HNSW candidates to request per desired match.

    The nearest-first CTE over-samples because the scope, live-document and
    active-run joins can discard candidates. Eight is deliberately generous
    for a small corpus; it is capped by the caller's limit and the HNSW
    ``ef_search`` setting in the retrieval transaction.
    """
    navigation_token_budget: int = Field(default=8_000, ge=500, le=64_000)
    sql_database_url: str | None = None
    sql_statement_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)
    sql_max_result_rows: int = Field(default=200, ge=1, le=5_000)
    sql_reader_user: str = "retriv_reader"
    sql_reader_password: SecretStr | None = None

    # -- tracing --------------------------------------------------------
    tracing_enabled: bool = False
    tracing_endpoint: str = "http://127.0.0.1:4318/v1/traces"
    tracing_sample_ratio: float = Field(default=1.0, gt=0, le=1)

    @model_validator(mode="after")
    def check_sql_reader_credentials_agree(self) -> "Settings":
        """Catch the reader role being created with one password and connected
        to with another.

        The password appears twice by necessity: `postgres-init` creates the
        role from `sql_reader_password`, and the DSN carries it separately
        because that is what asyncpg authenticates with. Nothing interpolates
        between them. Rotating one and forgetting the other leaves a deployment
        that migrates cleanly, is configured cleanly, and then fails at startup
        with "configured but unusable" -- which names the DSN and hides the
        actual mismatch.
        """
        if self.sql_database_url is None or self.sql_reader_password is None:
            return self
        password = self.sql_reader_password.get_secret_value()
        if not password:
            return self
        parsed = make_url(self.sql_database_url)
        if parsed.password is None or not parsed.password:
            raise ValueError(
                "RETRIV_SQL_DATABASE_URL has no password, so it cannot match "
                "RETRIV_SQL_READER_PASSWORD. Include one, or drop "
                "RETRIV_SQL_READER_PASSWORD to stop this check."
            )
        if not hmac.compare_digest(unquote(parsed.password), password):
            raise ValueError(
                "RETRIV_SQL_READER_PASSWORD does not match the password in "
                "RETRIV_SQL_DATABASE_URL. postgres-init creates the role with "
                "the first and the app connects with the second; they must be "
                "the same value."
            )
        if parsed.username != self.sql_reader_user:
            raise ValueError(
                f"RETRIV_SQL_DATABASE_URL connects as {parsed.username!r} but "
                f"postgres-init creates the role named {self.sql_reader_user!r}. "
                "Point the DSN at the role this deployment creates."
            )
        return self

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("RETRIV_JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("bootstrap_admin_username")
    @classmethod
    def normalize_bootstrap_admin_username(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) > 255:
            raise ValueError("RETRIV_BOOTSTRAP_ADMIN_USERNAME cannot exceed 255 characters")
        return normalized

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_allowed_origins(cls, values: list[str]) -> list[str]:
        normalized_origins: list[str] = []
        for value in values:
            origin = value.strip().rstrip("/")
            parsed = urlsplit(origin)
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError("CORS origins must be exact HTTP(S) origins")
            if origin not in normalized_origins:
                normalized_origins.append(origin)
        return normalized_origins

    @field_validator("s3_endpoint_url", mode="before")
    @classmethod
    def normalize_s3_endpoint_url(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("s3_endpoint_url")
    @classmethod
    def validate_s3_endpoint_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RETRIV_S3_ENDPOINT_URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("RETRIV_S3_ENDPOINT_URL must not contain credentials or query data")
        return value.rstrip("/")

    @field_validator("embedding_base_url", "vlm_base_url")
    @classmethod
    def validate_service_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Service URLs must be absolute HTTP(S) URLs")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Service URLs must not contain credentials or query data")
        return value.rstrip("/")

    @field_validator(
        "s3_secret_key",
        "embedding_api_key",
        "vlm_api_key",
    )
    @classmethod
    def validate_secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("Service secrets must be at least 32 characters")
        return value

    @field_validator(
        "embedding_model",
        "embedding_profile",
        "pipeline_version",
    )
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("Identifiers must be non-empty and free of whitespace")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value):
            raise ValueError("Identifiers must contain only letters, numbers, '.', '_', or '-'")
        return value

    @field_validator("vlm_model")
    @classmethod
    def validate_vlm_model(cls, value: str) -> str:
        """Accept a gateway's model id, which is a namespace and a name.

        The other identifiers are bare words this codebase owns, so they
        cannot contain a slash. A gateway's model id is somebody else's
        namespace -- ``minimax/minimax-m3.1-flash`` -- so the shape is
        wider here: letters, numbers and the separators a provider
        actually uses, and nothing else.
        """
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("Model ids must be non-empty and free of whitespace")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}", value):
            raise ValueError(
                "Model ids must contain only letters, numbers, '.', '_', ':', '/', or '-'"
            )
        return value

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if self.s3_raw_bucket == self.s3_artifact_bucket:
            raise ValueError("Raw and artifact buckets must be different")
        if self.processing_retry_max_seconds < self.processing_retry_base_seconds:
            raise ValueError("RETRIV_PROCESSING_RETRY_MAX_SECONDS must be >= retry base seconds")
        if self.max_artifact_bytes > self.max_chunk_payload_bytes:
            raise ValueError(
                "RETRIV_MAX_ARTIFACT_BYTES must not exceed RETRIV_MAX_CHUNK_PAYLOAD_BYTES"
            )
        if self.max_total_image_bytes < self.max_image_bytes:
            raise ValueError(
                "RETRIV_MAX_TOTAL_IMAGE_BYTES must not be below RETRIV_MAX_IMAGE_BYTES"
            )
        if self.max_json_value_bytes > self.max_json_row_bytes:
            raise ValueError(
                "RETRIV_MAX_JSON_VALUE_BYTES must not exceed RETRIV_MAX_JSON_ROW_BYTES"
            )
        if self.model_wait_max_seconds < self.model_wait_base_seconds:
            raise ValueError(
                "RETRIV_MODEL_WAIT_MAX_SECONDS must be >= RETRIV_MODEL_WAIT_BASE_SECONDS"
            )
        if self.vlm_max_tokens_ceiling < self.vlm_max_tokens:
            raise ValueError(
                "RETRIV_VLM_MAX_TOKENS_CEILING must not be below RETRIV_VLM_MAX_TOKENS"
            )

        bootstrap_password = self.bootstrap_admin_password.get_secret_value()
        if bool(self.bootstrap_admin_username) != bool(bootstrap_password):
            raise ValueError(
                "RETRIV_BOOTSTRAP_ADMIN_USERNAME and "
                "RETRIV_BOOTSTRAP_ADMIN_PASSWORD must be set together"
            )
        if bootstrap_password:
            password_bytes = len(bootstrap_password.encode("utf-8"))
            if password_bytes < 12:
                raise ValueError("RETRIV_BOOTSTRAP_ADMIN_PASSWORD must be at least 12 bytes")
            if password_bytes > self.max_password_bytes:
                raise ValueError(
                    "RETRIV_BOOTSTRAP_ADMIN_PASSWORD cannot exceed RETRIV_MAX_PASSWORD_BYTES"
                )

        if self.environment != "production":
            return self

        secrets: tuple[str, ...]
        if self.component == "api":
            secrets = (
                self.jwt_secret.get_secret_value(),
                self.s3_secret_key.get_secret_value(),
                self.bootstrap_admin_password.get_secret_value(),
            )
        else:
            secrets = (
                self.s3_secret_key.get_secret_value(),
                self.embedding_api_key.get_secret_value(),
                self.vlm_api_key.get_secret_value(),
            )
        if any(
            marker in secret.lower()
            for secret in secrets
            for marker in (
                "change-this",
                "change_me",
                "change-me",
                "development",
                "replace",
                "replace-me",
            )
        ):
            raise ValueError("Production secrets must not use development placeholders")

        if any(
            marker in self.database_url.lower()
            for marker in ("change-this", "development", "replace-me")
        ):
            raise ValueError("Production database credentials must be configured")
        database = urlsplit(self.database_url)
        if not database.password or database.password == database.username:
            raise ValueError("Production database credentials must not use defaults")

        if self.s3_endpoint_url is not None and not self.s3_endpoint_url.startswith("https://"):
            raise ValueError("Production object storage must use HTTPS")
        if any(
            not url.startswith("https://")
            for url in (self.embedding_base_url, self.vlm_base_url)
        ):
            raise ValueError("Production embedding and vision endpoints must use HTTPS")
        if self.component == "api" and not self.antivirus_enabled:
            raise ValueError("Antivirus scanning cannot be disabled in production")
        if self.component == "api" and any(
            not origin.startswith("https://") for origin in self.cors_allowed_origins
        ):
            raise ValueError("Production CORS origins must use HTTPS")
        if self.component == "api" and len(self.jwt_secret.get_secret_value()) < 64:
            raise ValueError("RETRIV_JWT_SECRET must be at least 64 characters in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
