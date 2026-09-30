import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings


class StorageError(Exception):
    """Object storage could not complete an operation."""


class ObjectNotFoundError(StorageError):
    """The requested object does not exist."""


class ObjectTooLargeError(StorageError):
    """The requested object exceeds the configured processing limit."""


@dataclass(frozen=True)
class DownloadResult:
    size_bytes: int
    sha256: str


class ObjectStorage(Protocol):
    def download_file(
        self,
        destination: Path,
        *,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> DownloadResult: ...

    def put_bytes(
        self,
        data: bytes,
        *,
        bucket: str,
        key: str,
        content_type: str,
        content_encoding: str | None,
        sha256: str,
    ) -> None: ...

    def delete_object(self, *, bucket: str, key: str) -> None: ...

    def delete_prefix(self, *, bucket: str, prefix: str) -> None: ...

    def check_health(self, *, bucket: str) -> None: ...


class S3ObjectStorage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
    ) -> None:
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                retries={"mode": "standard", "total_max_attempts": 4},
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=120,
            ),
        )

    def download_file(
        self,
        destination: Path,
        *,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> DownloadResult:
        stream: Any | None = None
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            stream = response["Body"]
            assert stream is not None
            content_length = int(response.get("ContentLength", 0))
            if content_length > max_bytes:
                raise ObjectTooLargeError("Source object exceeds the processing limit")
            digest = hashlib.sha256()
            total_bytes = 0
            with destination.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise ObjectTooLargeError(
                            "Source object exceeds the processing limit"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            return DownloadResult(size_bytes=total_bytes, sha256=digest.hexdigest())
        except ObjectTooLargeError:
            destination.unlink(missing_ok=True)
            raise
        except ClientError as exc:
            destination.unlink(missing_ok=True)
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in {"NoSuchKey", "NoSuchObject", "404"}:
                raise ObjectNotFoundError("Source object does not exist") from exc
            raise StorageError("Object download failed") from exc
        except (BotoCoreError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise StorageError("Object download failed") from exc
        finally:
            if stream is not None:
                stream.close()

    def put_bytes(
        self,
        data: bytes,
        *,
        bucket: str,
        key: str,
        content_type: str,
        content_encoding: str | None,
        sha256: str,
    ) -> None:
        request: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
            "ContentLength": len(data),
            "ContentType": content_type,
            "Metadata": {"sha256": sha256},
        }
        if content_encoding is not None:
            request["ContentEncoding"] = content_encoding
        try:
            self._client.put_object(**request)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Artifact upload failed") from exc

    def delete_object(self, *, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Artifact deletion failed") from exc

    def delete_prefix(self, *, bucket: str, prefix: str) -> None:
        try:
            while True:
                page = self._client.list_objects_v2(
                    Bucket=bucket,
                    Prefix=prefix,
                    MaxKeys=1000,
                )
                objects = [
                    {"Key": item["Key"]}
                    for item in page.get("Contents", [])
                    if item.get("Key")
                ]
                if not objects:
                    return
                response = self._client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
                if response.get("Errors"):
                    raise StorageError("Artifact prefix deletion was incomplete")
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Artifact prefix deletion failed") from exc

    def check_health(self, *, bucket: str) -> None:
        try:
            self._client.get_bucket_location(Bucket=bucket)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Object storage is unavailable") from exc


@lru_cache
def get_object_storage() -> ObjectStorage:
    settings = get_settings()
    return S3ObjectStorage(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
    )
