import argparse
import json
import resource
from dataclasses import asdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--media-type", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ocr-language", action="append", required=True)
    parser.add_argument("--chunk-max-tokens", type=int, required=True)
    parser.add_argument("--tokenizer-name", required=True)
    parser.add_argument("--artifacts-path", type=Path)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--memory-limit-bytes", type=int, required=True)
    parser.add_argument("--max-processes", type=int, required=True)
    parser.add_argument("--max-artifact-bytes", type=int, required=True)
    parser.add_argument("--max-chunk-payload-bytes", type=int, required=True)
    parser.add_argument("--max-extracted-text-bytes", type=int, required=True)
    parser.add_argument("--max-chunks", type=int, required=True)
    return parser.parse_args()


def set_resource_limits(args: argparse.Namespace) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (args.timeout_seconds, args.timeout_seconds + 5),
    )
    resource.setrlimit(
        resource.RLIMIT_AS,
        (args.memory_limit_bytes, args.memory_limit_bytes),
    )
    resource.setrlimit(resource.RLIMIT_NPROC, (args.max_processes, args.max_processes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    max_file_bytes = max(args.max_artifact_bytes, args.max_chunk_payload_bytes)
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))


def run(args: argparse.Namespace) -> None:
    set_resource_limits(args)
    from app.extraction.docling import DoclingExtractor

    extractor = DoclingExtractor(
        ocr_languages=args.ocr_language,
        chunk_max_tokens=args.chunk_max_tokens,
        tokenizer_name=args.tokenizer_name,
        artifacts_path=args.artifacts_path,
    )
    result = extractor.extract(args.input, media_type=args.media_type)
    if len(result.document_json) > args.max_artifact_bytes:
        raise ValueError("Document artifact exceeds the processing limit")
    if len(result.chunks) > args.max_chunks:
        raise ValueError("Document produced too many chunks")
    if (
        sum(len(chunk.text.encode("utf-8")) for chunk in result.chunks)
        > args.max_extracted_text_bytes
    ):
        raise ValueError("Extracted text exceeds the processing limit")
    chunks_payload = json.dumps(
        {
            "extractor_version": result.extractor_version,
            "chunks": [asdict(chunk) for chunk in result.chunks],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    if len(chunks_payload) > args.max_chunk_payload_bytes:
        raise ValueError("Chunk metadata exceeds the processing limit")
    (args.output_dir / "document.json").write_bytes(result.document_json)
    (args.output_dir / "chunks.json").write_bytes(chunks_payload)


def main() -> None:
    args = parse_args()
    try:
        run(args)
    except Exception as exc:
        error_path = args.output_dir / "error.txt"
        error_path.write_text(
            f"Document extraction failed: {type(exc).__name__}",
            encoding="utf-8",
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
