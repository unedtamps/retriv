import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.extraction.base import ExtractedChunk, ExtractionError, ExtractionResult

SAFE_ENVIRONMENT_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TESSDATA_PREFIX",
    "TMPDIR",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
)


class SandboxedDoclingExtractor:
    def __init__(
        self,
        *,
        ocr_languages: list[str],
        chunk_max_tokens: int,
        tokenizer_name: str,
        artifacts_path: Path | None,
        timeout_seconds: int,
        memory_limit_bytes: int,
        max_processes: int,
        max_artifact_bytes: int,
        max_chunk_payload_bytes: int,
        max_extracted_text_bytes: int,
        max_chunks: int,
    ) -> None:
        self._ocr_languages = ocr_languages
        self._chunk_max_tokens = chunk_max_tokens
        self._tokenizer_name = tokenizer_name
        self._artifacts_path = artifacts_path
        self._timeout_seconds = timeout_seconds
        self._memory_limit_bytes = memory_limit_bytes
        self._max_processes = max_processes
        self._max_artifact_bytes = max_artifact_bytes
        self._max_chunk_payload_bytes = max_chunk_payload_bytes
        self._max_extracted_text_bytes = max_extracted_text_bytes
        self._max_chunks = max_chunks

    def extract(self, path: Path, *, media_type: str) -> ExtractionResult:
        with tempfile.TemporaryDirectory(
            prefix="context-extraction-", dir=path.parent
        ) as temporary_directory:
            output_dir = Path(temporary_directory)
            command = [
                sys.executable,
                "-m",
                "app.extraction.sandbox_runner",
                "--input",
                str(path),
                "--media-type",
                media_type,
                "--output-dir",
                str(output_dir),
                "--chunk-max-tokens",
                str(self._chunk_max_tokens),
                "--tokenizer-name",
                self._tokenizer_name,
                "--timeout-seconds",
                str(self._timeout_seconds),
                "--memory-limit-bytes",
                str(self._memory_limit_bytes),
                "--max-processes",
                str(self._max_processes),
                "--max-artifact-bytes",
                str(self._max_artifact_bytes),
                "--max-chunk-payload-bytes",
                str(self._max_chunk_payload_bytes),
                "--max-extracted-text-bytes",
                str(self._max_extracted_text_bytes),
                "--max-chunks",
                str(self._max_chunks),
            ]
            for language in self._ocr_languages:
                command.extend(("--ocr-language", language))
            if self._artifacts_path is not None:
                command.extend(("--artifacts-path", str(self._artifacts_path)))

            environment = {
                key: os.environ[key]
                for key in SAFE_ENVIRONMENT_KEYS
                if key in os.environ
            }
            environment.update(
                {
                    "DOCLING_SERVE_ENABLE_REMOTE_SERVICES": "false",
                    "MKL_NUM_THREADS": "4",
                    "NUMEXPR_NUM_THREADS": "4",
                    "OMP_NUM_THREADS": "4",
                    "OPENBLAS_NUM_THREADS": "4",
                }
            )
            if self._artifacts_path is not None:
                environment.update(
                    {
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                    }
                )
            stderr_path = output_dir / "stderr.log"
            with stderr_path.open("wb") as stderr:
                process = subprocess.Popen(  # noqa: S603 - fixed interpreter and module
                    command,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    start_new_session=True,
                )
                try:
                    return_code = process.wait(timeout=self._timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    self._kill_process_group(process)
                    raise ExtractionError(
                        "Document extraction exceeded the time limit"
                    ) from exc
            if return_code != 0:
                detail = self._read_error(output_dir / "error.txt")
                if detail == "Document extraction sandbox failed":
                    detail = self._read_error(stderr_path)
                raise ExtractionError(f"{detail} (exit status {return_code})")

            document_json = self._read_bounded(
                output_dir / "document.json", self._max_artifact_bytes
            )
            chunks_payload = self._read_bounded(
                output_dir / "chunks.json", self._max_chunk_payload_bytes
            )
            try:
                result_payload = json.loads(chunks_payload)
                extractor_version = result_payload["extractor_version"]
                raw_chunks = result_payload["chunks"]
                if not isinstance(extractor_version, str) or not isinstance(raw_chunks, list):
                    raise TypeError
                chunks = [self._parse_chunk(item) for item in raw_chunks]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ExtractionError("Sandbox returned an invalid extraction result") from exc
            if len(chunks) > self._max_chunks:
                raise ExtractionError("Sandbox returned too many chunks")
            if (
                sum(len(chunk.text.encode("utf-8")) for chunk in chunks)
                > self._max_extracted_text_bytes
            ):
                raise ExtractionError("Sandbox returned too much extracted text")
            return ExtractionResult(
                document_json=document_json,
                chunks=chunks,
                extractor_version=extractor_version,
            )

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()

    @staticmethod
    def _read_bounded(path: Path, max_bytes: int) -> bytes:
        try:
            if path.stat().st_size > max_bytes:
                raise ExtractionError("Sandbox output exceeds the processing limit")
            return path.read_bytes()
        except OSError as exc:
            raise ExtractionError("Sandbox output is unavailable") from exc

    @staticmethod
    def _read_error(path: Path) -> str:
        try:
            with path.open(encoding="utf-8", errors="replace") as error_file:
                return error_file.read(1000)
        except OSError:
            return "Document extraction sandbox failed"

    @staticmethod
    def _parse_chunk(value: Any) -> ExtractedChunk:
        if not isinstance(value, dict):
            raise TypeError
        text = value.get("text")
        token_count = value.get("token_count")
        section_path = value.get("section_path")
        page_numbers = value.get("page_numbers")
        provenance = value.get("provenance")
        if (
            not isinstance(text, str)
            or not isinstance(token_count, int)
            or not isinstance(section_path, list)
            or not all(isinstance(item, str) for item in section_path)
            or not isinstance(page_numbers, list)
            or not all(isinstance(item, int) for item in page_numbers)
            or not isinstance(provenance, dict)
        ):
            raise TypeError
        return ExtractedChunk(
            text=text,
            token_count=token_count,
            section_path=section_path,
            page_numbers=page_numbers,
            provenance=provenance,
        )
