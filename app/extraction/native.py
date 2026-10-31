"""The native extractor: markdown in, chunks out, no models in between.

A PDF is read page by page -- its text layer where it has one, its raster
where it has none -- and a DOCX is walked by its styles. The pages become
one block stream in reading order, the stream is packed into chunks, and
the pictures are lifted out for the vision stage. Nothing here loads
weights: the whole run is CPU-bound parsing, which is why it finishes in
seconds where a model pipeline took minutes per page.

The stored artifact is the markdown itself with a page map, so what the
extractor made of the file can be read directly by a person.
"""

import json
from pathlib import Path

import pymupdf

from app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.extraction.base import (
    ExtractedChunk,
    ExtractionError,
    ExtractionResourceError,
    ExtractionResult,
    looks_like_allocation_failure,
)
from app.extraction.chunker import MarkdownChunker
from app.extraction.docx_ir import build_docx_blocks
from app.extraction.images_native import (
    collect_pdf_images,
    collect_vector_charts,
    image_refs_by_page,
    scan_page_numbers,
)
from app.extraction.markdown_ir import (
    DocumentBlock,
    assemble_markdown,
    build_pdf_pages,
    insert_image_blocks,
    parse_markdown_blocks,
)

EXTRACTOR_VERSION = "retriv-native-1.1"


class NativeMarkdownExtractor:
    def __init__(
        self,
        *,
        chunk_max_tokens: int,
        tokenizer_name: str,
        scan_text_threshold: int = 32,
        scan_page_describe: bool = True,
        picture_images_enabled: bool = False,
        image_render_scale: float = 2.0,
        image_min_pixels: int = 0,
        max_images: int = 0,
        max_image_bytes: int = 5 * 1024 * 1024,
        max_total_image_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._chunker = MarkdownChunker(
            chunk_max_tokens=chunk_max_tokens, tokenizer_name=tokenizer_name
        )
        self._scan_text_threshold = scan_text_threshold
        self._scan_page_describe = scan_page_describe
        self._picture_images_enabled = picture_images_enabled
        self._image_render_scale = image_render_scale
        self._image_min_pixels = image_min_pixels
        self._max_images = max_images
        self._max_image_bytes = max_image_bytes
        self._max_total_image_bytes = max_total_image_bytes

    def extract(self, path: Path, *, media_type: str) -> ExtractionResult:
        try:
            if media_type == PDF_MEDIA_TYPE:
                return self._extract_pdf(path)
            if media_type == DOCX_MEDIA_TYPE:
                return self._extract_docx(path)
            raise ValueError(f"Unsupported media type for extraction: {media_type}")
        except ExtractionError:
            raise
        except Exception as exc:
            if looks_like_allocation_failure(f"{exc}: {exc.__cause__}"):
                raise ExtractionResourceError(
                    f"The extraction sandbox ran out of address space ({exc})"
                ) from exc
            raise ExtractionError(f"The document could not be parsed: {exc}") from exc

    def _extract_pdf(self, path: Path) -> ExtractionResult:
        with pymupdf.open(path) as pdf:
            pages, page_count = build_pdf_pages(
                path, scan_text_threshold=self._scan_text_threshold
            )
            images = collect_pdf_images(
                pdf,
                pages,
                picture_images_enabled=self._picture_images_enabled,
                image_render_scale=self._image_render_scale,
                image_min_pixels=self._image_min_pixels,
                max_images=self._max_images,
                max_image_bytes=self._max_image_bytes,
                max_total_image_bytes=self._max_total_image_bytes,
                scan_page_describe=self._scan_page_describe,
            )
            images.extend(
                collect_vector_charts(
                    pdf,
                    pages,
                    picture_images_enabled=self._picture_images_enabled,
                    image_render_scale=self._image_render_scale,
                    image_min_pixels=self._image_min_pixels,
                    max_images=self._max_images,
                    max_image_bytes=self._max_image_bytes,
                    max_total_image_bytes=self._max_total_image_bytes,
                    skip_pages={image.page_number for image in images},
                )
            )
        blocks: list[DocumentBlock] = []
        for page in pages:
            blocks.extend(parse_markdown_blocks(page.markdown, page.page_no))
        blocks = insert_image_blocks(
            pages,
            blocks,
            image_refs_by_page(images),
            scan_page_numbers(images),
        )
        return self._result(
            blocks,
            images,
            pages_markdown=assemble_markdown(pages),
            page_count=page_count,
            source_text_chars=sum(page.text_layer_chars for page in pages),
            ocr_enabled=True,
        )

    def _extract_docx(self, path: Path) -> ExtractionResult:
        blocks, images = build_docx_blocks(
            path,
            image_render_scale=self._image_render_scale,
            max_images=self._max_images,
            max_image_bytes=self._max_image_bytes,
            max_total_image_bytes=self._max_total_image_bytes,
        )
        markdown = "\n\n".join(block.text for block in blocks if block.kind != "image")
        return self._result(
            blocks,
            images,
            pages_markdown=markdown,
            page_count=0,
            source_text_chars=0,
            ocr_enabled=False,
        )

    def _result(
        self,
        blocks: list[DocumentBlock],
        images: list,
        *,
        pages_markdown: str,
        page_count: int,
        source_text_chars: int,
        ocr_enabled: bool,
    ) -> ExtractionResult:
        chunks: list[ExtractedChunk] = self._chunker.chunk(blocks)
        document_json = json.dumps(
            {
                "format": "retriv-markdown-1",
                "extractor": EXTRACTOR_VERSION,
                "page_count": page_count,
                "markdown": pages_markdown,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return ExtractionResult(
            document_json=document_json,
            chunks=chunks,
            extractor_version=EXTRACTOR_VERSION,
            ocr_enabled=ocr_enabled,
            source_text_chars=source_text_chars,
            page_count=page_count,
            images=images,
        )
