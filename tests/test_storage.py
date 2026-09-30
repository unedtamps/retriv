import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.infrastructure.storage import (
    ObjectTooLargeError,
    S3ArtifactStorage,
)


class RecordingBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        del size
        return next(self._chunks, b"")

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, body: RecordingBody, content_length: int) -> None:
        self.body = body
        self.content_length = content_length

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"Body": self.body, "ContentLength": self.content_length}


def storage_with_client(client: FakeClient) -> S3ArtifactStorage:
    """The artifact client is the one that downloads; the raw client
    streams for the download route instead."""
    storage = object.__new__(S3ArtifactStorage)
    storage._client = client
    return storage


def test_download_hashes_bytes_and_closes_stream(tmp_path: Path) -> None:
    body = RecordingBody([b"abc", b"def"])
    storage = storage_with_client(FakeClient(body, 6))
    destination = tmp_path / "source.pdf"

    result = storage.download_file(
        destination, bucket="raw-documents", key="key", max_bytes=6
    )

    assert destination.read_bytes() == b"abcdef"
    assert result.sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert body.closed is True


def test_declared_oversized_download_closes_stream(tmp_path: Path) -> None:
    body = RecordingBody([b"unused"])
    storage = storage_with_client(FakeClient(body, 7))
    destination = tmp_path / "source.pdf"

    with pytest.raises(ObjectTooLargeError):
        storage.download_file(
            destination, bucket="raw-documents", key="key", max_bytes=6
        )

    assert body.closed is True
    assert not destination.exists()


def test_streamed_oversized_download_removes_partial_file(tmp_path: Path) -> None:
    body = RecordingBody([b"abcd", b"efgh"])
    storage = storage_with_client(FakeClient(body, 0))
    destination = tmp_path / "source.pdf"

    with pytest.raises(ObjectTooLargeError):
        storage.download_file(
            destination, bucket="raw-documents", key="key", max_bytes=6
        )

    assert body.closed is True
    assert not destination.exists()
