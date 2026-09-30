import socket
import struct
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings


class ScannerUnavailableError(Exception):
    """The malware scanner could not be reached or returned an invalid response."""


class MalwareDetectedError(Exception):
    """The uploaded file contains malware."""


class MalwareScanner(Protocol):
    def scan_file(self, path: Path) -> None: ...

    def check_health(self) -> None: ...


class DisabledMalwareScanner:
    def scan_file(self, path: Path) -> None:
        del path

    def check_health(self) -> None:
        return


class ClamAVScanner:
    _chunk_size = 1024 * 1024
    _max_response_bytes = 4096

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    def scan_file(self, path: Path) -> None:
        try:
            with socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            ) as connection:
                connection.settimeout(self._timeout)
                connection.sendall(b"zINSTREAM\0")
                with path.open("rb") as source:
                    while chunk := source.read(self._chunk_size):
                        connection.sendall(struct.pack("!I", len(chunk)))
                        connection.sendall(chunk)
                connection.sendall(struct.pack("!I", 0))
                response = self._receive_response(connection)
        except (OSError, TimeoutError) as exc:
            raise ScannerUnavailableError("Malware scanner is unavailable") from exc

        if response.endswith(" OK"):
            return
        if response.endswith(" FOUND"):
            raise MalwareDetectedError("Malware was detected in the uploaded file")
        raise ScannerUnavailableError("Malware scanner returned an unexpected response")

    def check_health(self) -> None:
        try:
            with socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            ) as connection:
                connection.settimeout(self._timeout)
                connection.sendall(b"zPING\0")
                response = self._receive_response(connection)
        except (OSError, TimeoutError) as exc:
            raise ScannerUnavailableError("Malware scanner is unavailable") from exc
        if response != "PONG":
            raise ScannerUnavailableError("Malware scanner health check failed")

    def _receive_response(self, connection: socket.socket) -> str:
        response = bytearray()
        while len(response) < self._max_response_bytes:
            chunk = connection.recv(min(1024, self._max_response_bytes - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if b"\0" in chunk:
                break
        return bytes(response).rstrip(b"\0\n").decode("utf-8", errors="replace")


@lru_cache
def get_malware_scanner() -> MalwareScanner:
    settings = get_settings()
    if not settings.antivirus_enabled:
        return DisabledMalwareScanner()
    return ClamAVScanner(
        host=settings.clamd_host,
        port=settings.clamd_port,
        timeout=settings.clamd_timeout_seconds,
    )
