import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.extraction.base import (
    ExtractedChunk,
    ExtractedImage,
    ExtractionError,
    ExtractionResourceError,
    ExtractionResult,
    looks_like_allocation_failure,
)

logger = logging.getLogger(__name__)

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
    "LD_PRELOAD",
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
        picture_images_enabled: bool = False,
        image_render_scale: float = 2.0,
        image_min_pixels: int = 0,
        max_images: int = 0,
        max_image_bytes: int = 5 * 1024 * 1024,
        max_total_image_bytes: int = 50 * 1024 * 1024,
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
        self._picture_images_enabled = picture_images_enabled
        self._image_render_scale = image_render_scale
        self._image_min_pixels = image_min_pixels
        self._max_images = max_images
        self._max_image_bytes = max_image_bytes
        self._max_total_image_bytes = max_total_image_bytes

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
                "--max-image-bytes",
                str(self._max_image_bytes),
                "--max-total-image-bytes",
                str(self._max_total_image_bytes),
            ]
            if self._picture_images_enabled:
                command.append("--picture-images")
            command.extend(
                (
                    "--image-render-scale",
                    str(self._image_render_scale),
                    "--image-min-pixels",
                    str(self._image_min_pixels),
                    "--max-images",
                    str(self._max_images),
                )
            )
            for language in self._ocr_languages:
                command.extend(("--ocr-language", language))
            if self._artifacts_path is not None:
                command.extend(("--artifacts-path", str(self._artifacts_path)))

            environment = {
                key: os.environ[key] for key in SAFE_ENVIRONMENT_KEYS if key in os.environ
            }
            environment.update(
                {
                    "DOCLING_SERVE_ENABLE_REMOTE_SERVICES": "false",
                    "MKL_NUM_THREADS": "4",
                    "NUMEXPR_NUM_THREADS": "4",
                    "OMP_NUM_THREADS": "4",
                    "OPENBLAS_NUM_THREADS": "4",
                    "MALLOC_ARENA_MAX": "2",
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
                    logger.error(
                        "Document extraction exceeded the time limit (input=%s, timeout=%ss)",
                        path,
                        self._timeout_seconds,
                    )
                    raise ExtractionError("Document extraction exceeded the time limit") from exc
            if return_code != 0:
                detail = self._read_error(output_dir / "error.txt")
                stderr_detail = self._read_tail(stderr_path, 4000)
                if stderr_detail:
                    logger.error(
                        "Extraction sandbox stderr (input=%s, exit=%s): %s",
                        path,
                        return_code,
                        stderr_detail[:3000],
                    )
                if detail == "Document extraction sandbox failed":
                    detail = stderr_detail
                logger.error(
                    "Extraction sandbox failed (input=%s, exit=%s): %s",
                    path,
                    return_code,
                    detail[:3000],
                )
                if return_code == -signal.SIGKILL or looks_like_allocation_failure(
                    f"{detail}\n{stderr_detail}"
                ):
                    raise ExtractionResourceError(
                        f"{detail} (exit status {return_code}, sandbox limit "
                        f"{self._memory_limit_bytes / 1024**3:.1f} GiB)"
                    )
                raise ExtractionError(f"{detail} (exit status {return_code})")

            sandbox_log = self._read_tail(stderr_path, 4000)
            if sandbox_log:
                log = (
                    logger.warning
                    if "WARNING" in sandbox_log.upper() or "ERROR" in sandbox_log.upper()
                    else logger.info
                )
                log("Extraction sandbox log (input=%s): %s", path, sandbox_log[:3000])

            document_json = self._read_bounded(
                output_dir / "document.json", self._max_artifact_bytes
            )
            chunks_payload = self._read_bounded(
                output_dir / "chunks.json", self._max_chunk_payload_bytes
            )
            try:
                result_payload = json.loads(chunks_payload)
                extractor_version = result_payload["extractor_version"]
                ocr_enabled = result_payload["ocr_enabled"]
                source_text_chars = result_payload["source_text_chars"]
                page_count = result_payload.get("page_count", 0)
                raw_chunks = result_payload["chunks"]
                raw_images = result_payload.get("images", [])
                if (
                    not isinstance(extractor_version, str)
                    or not isinstance(ocr_enabled, bool)
                    or not isinstance(source_text_chars, int)
                    or isinstance(source_text_chars, bool)
                    or source_text_chars < 0
                    or not isinstance(page_count, int)
                    or isinstance(page_count, bool)
                    or page_count < 0
                    or not isinstance(raw_chunks, list)
                    or not isinstance(raw_images, list)
                ):
                    raise TypeError
                chunks = [self._parse_chunk(item) for item in raw_chunks]
                images = [self._parse_image(item, output_dir) for item in raw_images]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ExtractionError("Sandbox returned an invalid extraction result") from exc
            except ExtractionError:
                raise
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
                ocr_enabled=ocr_enabled,
                source_text_chars=source_text_chars,
                page_count=page_count,
                images=images,
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
                return error_file.read(4000)
        except OSError:
            return "Document extraction sandbox failed"

    @staticmethod
    def _read_tail(path: Path, max_bytes: int) -> str:
        """Read the end of a log file, where the failure usually is.

        Sandbox startup can emit thousands of bytes before the first page is
        parsed. Reading the head of stderr would hide the conversion warning
        that explains a failed extraction.
        """
        try:
            with path.open("rb") as log_file:
                log_file.seek(0, os.SEEK_END)
                size = log_file.tell()
                log_file.seek(max(0, size - max_bytes))
                return log_file.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _parse_image(self, value: Any, output_dir: Path) -> ExtractedImage:
        """Read one picture back from the sandbox's output directory.

        The bytes are read under the same per-image cap the runner wrote them
        under, so a sandbox that ignored the cap cannot make the parent hold
        more than it agreed to.
        """
        if not isinstance(value, dict):
            raise TypeError
        ref = value.get("ref")
        page_number = value.get("page_number")
        media_type = value.get("media_type")
        caption = value.get("caption", "")
        width = value.get("width", 0)
        height = value.get("height", 0)
        name = value.get("file")
        if (
            not isinstance(ref, str)
            or not ref
            or not isinstance(page_number, int)
            or isinstance(page_number, bool)
            or not isinstance(media_type, str)
            or not media_type
            or not isinstance(caption, str)
            or not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
            or not isinstance(name, str)
            or not name
        ):
            raise TypeError
        path = (output_dir / name).resolve()
        if not path.is_relative_to(output_dir.resolve()):
            raise ValueError("Sandbox returned an image outside its output directory")
        data = self._read_bounded(path, self._max_image_bytes)
        return ExtractedImage(
            ref=ref,
            page_number=page_number,
            media_type=media_type,
            data=data,
            caption=caption,
            width=width,
            height=height,
        )

    @staticmethod
    def _parse_chunk(value: Any) -> ExtractedChunk:
        if not isinstance(value, dict):
            raise TypeError
        text = value.get("text")
        token_count = value.get("token_count")
        section_path = value.get("section_path")
        section_key = value.get("section_key")
        is_heading_only = value.get("is_heading_only")
        page_numbers = value.get("page_numbers")
        provenance = value.get("provenance")
        if (
            not isinstance(text, str)
            or not isinstance(token_count, int)
            or not isinstance(section_path, list)
            or not all(isinstance(item, str) for item in section_path)
            or not isinstance(section_key, str)
            or not isinstance(is_heading_only, bool)
            or not isinstance(page_numbers, list)
            or not all(isinstance(item, int) for item in page_numbers)
            or not isinstance(provenance, dict)
        ):
            raise TypeError
        return ExtractedChunk(
            text=text,
            token_count=token_count,
            section_path=section_path,
            section_key=section_key,
            is_heading_only=is_heading_only,
            page_numbers=page_numbers,
            provenance=provenance,
        )
