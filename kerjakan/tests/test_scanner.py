import socket
import struct
from pathlib import Path

import pytest

from app.infrastructure.scanner import ClamAVScanner, MalwareDetectedError, ScannerUnavailableError


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = bytearray()

    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, *_args: object) -> None:
        return

    def settimeout(self, timeout: float) -> None:
        del timeout

    def sendall(self, content: bytes) -> None:
        self.sent.extend(content)

    def recv(self, size: int) -> bytes:
        content, self.response = self.response[:size], self.response[size:]
        return content


def install_fake_connection(monkeypatch: pytest.MonkeyPatch, response: bytes) -> FakeSocket:
    connection = FakeSocket(response)
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: connection)
    return connection


def test_clamav_instream_protocol_for_clean_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"safe-content"
    path = tmp_path / "document.pdf"
    path.write_bytes(payload)
    connection = install_fake_connection(monkeypatch, b"stream: OK\0")

    ClamAVScanner("clamav", 3310, 1).scan_file(path)

    assert connection.sent.startswith(b"zINSTREAM\0")
    assert struct.pack("!I", len(payload)) + payload in connection.sent
    assert connection.sent.endswith(struct.pack("!I", 0))


def test_clamav_rejects_detected_malware(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "document.pdf"
    path.write_bytes(b"unsafe")
    install_fake_connection(monkeypatch, b"stream: Eicar-Signature FOUND\0")

    with pytest.raises(MalwareDetectedError):
        ClamAVScanner("clamav", 3310, 1).scan_file(path)


def test_clamav_fails_closed_on_limit_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "document.pdf"
    path.write_bytes(b"content")
    install_fake_connection(monkeypatch, b"INSTREAM size limit exceeded. ERROR\0")

    with pytest.raises(ScannerUnavailableError):
        ClamAVScanner("clamav", 3310, 1).scan_file(path)


def test_clamav_health_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = install_fake_connection(monkeypatch, b"PONG\0")

    ClamAVScanner("clamav", 3310, 1).check_health()

    assert connection.sent == b"zPING\0"
