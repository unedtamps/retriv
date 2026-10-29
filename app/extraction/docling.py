import base64
import json
import logging
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.extraction.base import (
    ExtractedChunk,
    ExtractedImage,
    ExtractionError,
    ExtractionResourceError,
    ExtractionResult,
    looks_like_allocation_failure,
)
from app.sections import encode_section_key

logger = logging.getLogger(__name__)


class DoclingExtractor:
    def __init__(
        self,
        *,
        ocr_languages: list[str],
        chunk_max_tokens: int,
        tokenizer_name: str,
        artifacts_path: Path | None = None,
        picture_images_enabled: bool = False,
        image_render_scale: float = 2.0,
        image_min_pixels: int = 0,
        max_images: int = 0,
        max_image_bytes: int = 5 * 1024 * 1024,
        max_total_image_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        try:
            import tiktoken
            from docling.datamodel.backend_options import (
                ThreadedDoclingParseBackendOptions,
            )
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                TableFormerMode,
                TableStructureOptions,
                TesseractCliOcrOptions,
            )
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling_core.transforms.chunker import HybridChunker
            from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer

            from app.extraction.tables import RowWiseSerializerProvider
        except ImportError as exc:
            if looks_like_allocation_failure(f"{exc}: {exc.__cause__}"):
                raise ExtractionResourceError(
                    "Could not map the parser's libraries: the sandbox address "
                    f"space is too small ({exc})"
                ) from exc
            raise RuntimeError(
                "Docling extraction dependencies are not installed; "
                "install retriv-ingest[extraction]"
            ) from exc

        pipeline_options = PdfPipelineOptions(
            do_ocr=True,
            do_table_structure=True,
            artifacts_path=artifacts_path,
            generate_picture_images=picture_images_enabled,
            images_scale=image_render_scale,
        )
        pipeline_options.ocr_options = TesseractCliOcrOptions(
            lang=ocr_languages,
        )
        pipeline_options.table_structure_options = TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=True,
        )
        self._converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options,
                    backend_options=ThreadedDoclingParseBackendOptions.model_validate(
                        {"release_native_memory_every_n_pages": 8}
                    ),
                )
            },
        )
        self._tokenizer = OpenAITokenizer(
            tokenizer=tiktoken.get_encoding(tokenizer_name),
            max_tokens=chunk_max_tokens,
        )
        self._serializer_provider = RowWiseSerializerProvider(
            pictures_marked=picture_images_enabled
        )
        self._chunker = HybridChunker(
            tokenizer=self._tokenizer,
            merge_peers=True,
            always_emit_headings=True,
            serializer_provider=self._serializer_provider,
        )
        self._picture_images_enabled = picture_images_enabled
        self._image_render_scale = image_render_scale
        self._image_min_pixels = image_min_pixels
        self._max_images = max_images
        self._max_image_bytes = max_image_bytes
        self._max_total_image_bytes = max_total_image_bytes
        try:
            self._version = version("docling")
        except PackageNotFoundError:
            self._version = "unknown"

    @staticmethod
    def measure_pdf_text_layer(path: Path) -> tuple[int, int]:
        """Return ``(total_text_chars, page_count)`` for a PDF's own text layer.

        Reads the embedded text directly, without Docling, so the number stays
        independent of whatever the parser later does with the file. The worker's
        coverage guardrail needs that independence to tell "this file had no text"
        apart from "the parser lost the text".
        """
        import pymupdf

        with pymupdf.open(path) as document:
            pages = document.page_count
            characters = 0
            for page in document:
                characters += len(page.get_text().strip())
        return characters, pages

    def extract(self, path: Path, *, media_type: str) -> ExtractionResult:
        if media_type not in {PDF_MEDIA_TYPE, DOCX_MEDIA_TYPE}:
            raise ExtractionError(f"Unsupported media type: {media_type}")
        source_text_chars = 0
        source_page_count = 0
        if media_type == PDF_MEDIA_TYPE:
            try:
                source_text_chars, source_page_count = self.measure_pdf_text_layer(path)
            except Exception:
                source_text_chars = 0
        try:
            conversion = self._converter.convert(path)
            status = getattr(conversion, "status", None)
            status_value = getattr(status, "value", str(status))
            conversion_errors = getattr(conversion, "errors", None) or []
            if status_value != "success" or conversion_errors:
                logger.warning(
                    "Docling conversion did not complete cleanly: status=%s errors=%d %s",
                    status_value,
                    len(conversion_errors),
                    _error_summary(conversion_errors),
                )
            document = conversion.document
            page_count = _page_count(document)
            if not isinstance(page_count, int):
                page_count = source_page_count
            allocation_failure = _allocation_failure(conversion_errors)
            if allocation_failure is not None:
                raise ExtractionResourceError(
                    f"Docling ran out of memory after {page_count} pages: {allocation_failure}"
                )
            images = self._collect_images(document, path, media_type)
            self._serializer_provider.mark_pictures({image.ref for image in images})
            document_json = self._export_document(document)
            chunks = [self._convert_chunk(chunk) for chunk in self._chunker.chunk(document)]
            with suppress(Exception):
                logger.info(
                    "Docling extraction finished: status=%s pages=%s chunks=%d "
                    "source_chars=%d ocr=%s version=%s",
                    status_value,
                    _page_count(document),
                    len(chunks),
                    source_text_chars,
                    media_type == PDF_MEDIA_TYPE,
                    self._version,
                )
        except ExtractionResourceError:
            raise
        except Exception as exc:
            if looks_like_allocation_failure(str(exc)):
                raise ExtractionResourceError(
                    f"Docling ran out of memory after {source_page_count} pages: "
                    f"{' '.join(str(exc).split())[:240]}"
                ) from exc
            raise ExtractionError("Docling could not process the document") from exc
        return ExtractionResult(
            document_json=document_json,
            chunks=chunks,
            extractor_version=self._version,
            ocr_enabled=media_type == PDF_MEDIA_TYPE,
            source_text_chars=source_text_chars,
            page_count=page_count,
            images=images,
        )

    def _export_document(self, document: Any) -> bytes:
        """Serialize the document, without the pictures' base64 payloads.

        The pictures are the bulk of the artifact once they are rendered, and
        they are described and then dropped. What the artifact is for is
        showing what the parser made of the file, and a base64 PNG in the
        middle of it says nothing that the description does not.
        """
        payload = document.export_to_dict(
            mode="json",
            by_alias=True,
            exclude_none=True,
            coord_precision=3,
            confid_precision=3,
        )
        for picture in payload.get("pictures", []):
            if isinstance(picture, dict):
                picture.pop("image", None)
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def _collect_images(
        self, document: Any, source_path: Path | None, media_type: str
    ) -> list[ExtractedImage]:
        """Lift the pictures out of the parsed document, cropping them here.

        Cropping after the conversion, from the PDF, keeps Docling from
        holding every page's raster until assembly; pictures that carry
        their own bytes, the ones a DOCX embeds, are taken as the parser
        handed them over. The byte caps are applied here because this is
        where the bytes exist in memory.
        """
        if not self._picture_images_enabled:
            return []
        texts = {text.self_ref: text.text for text in document.texts}
        images: list[ExtractedImage] = []
        total_bytes = 0
        renderable = media_type == PDF_MEDIA_TYPE and source_path is not None
        source_pdf = None
        try:
            for picture in document.pictures:
                if self._max_images and len(images) >= self._max_images:
                    break
                image = picture.image
                if image is not None and image.uri:
                    data = self._decode_data_uri(str(image.uri))
                    if data is None:
                        continue
                    media_type_of_picture = image.mimetype or "image/png"
                    width = int(image.size.width)
                    height = int(image.size.height)
                elif renderable:
                    if source_pdf is None:
                        import pymupdf

                        source_pdf = pymupdf.open(source_path)
                    rendered = self._render_picture(source_pdf, document, picture)
                    if rendered is None:
                        continue
                    data, media_type_of_picture, width, height = rendered
                else:
                    continue
                if width * height < self._image_min_pixels:
                    continue
                if len(data) > self._max_image_bytes:
                    continue
                if total_bytes + len(data) > self._max_total_image_bytes:
                    break
                total_bytes += len(data)
                caption = " ".join(
                    texts.get(ref.cref, "").strip() for ref in picture.captions
                ).strip()
                page_number = picture.prov[0].page_no if picture.prov else 0
                images.append(
                    ExtractedImage(
                        ref=picture.self_ref,
                        page_number=page_number,
                        media_type=media_type_of_picture,
                        data=data,
                        caption=caption,
                        width=width,
                        height=height,
                    )
                )
        finally:
            if source_pdf is not None:
                source_pdf.close()
        return images

    def _render_picture(
        self, source_pdf: Any, document: Any, picture: Any
    ) -> tuple[bytes, str, int, int] | None:
        """Crop one picture out of the source PDF at the render scale.

        The parser's bboxes are bottom-left in points and PyMuPDF is
        top-left, so the flip is the page height minus the two vertical
        edges; the result is clamped to the page.
        """
        import pymupdf
        from docling_core.types.doc import CoordOrigin

        if not picture.prov:
            return None
        prov = picture.prov[0]
        bbox = getattr(prov, "bbox", None)
        page_no = prov.page_no
        if bbox is None or page_no < 1 or page_no > source_pdf.page_count:
            return None
        scale = self._image_render_scale
        estimated_pixels = abs(bbox.r - bbox.l) * scale * abs(bbox.t - bbox.b) * scale
        if estimated_pixels < self._image_min_pixels:
            return None
        page = source_pdf[page_no - 1]
        if bbox.coord_origin == CoordOrigin.BOTTOMLEFT:
            page_item = document.pages.get(page_no)
            page_height = (
                page_item.size.height
                if page_item is not None and page_item.size.height
                else page.rect.height
            )
            rect = pymupdf.Rect(bbox.l, page_height - bbox.t, bbox.r, page_height - bbox.b)
        else:
            rect = pymupdf.Rect(bbox.as_tuple())
        clipped = pymupdf.Rect(
            max(rect.x0, page.rect.x0),
            max(rect.y0, page.rect.y0),
            min(rect.x1, page.rect.x1),
            min(rect.y1, page.rect.y1),
        )
        if clipped.is_empty or clipped.width <= 0 or clipped.height <= 0:
            return None
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            clip=clipped,
            alpha=False,
        )
        pixmap.set_dpi(int(72 * scale), int(72 * scale))
        return pixmap.tobytes("png"), "image/png", pixmap.width, pixmap.height

    @staticmethod
    def _decode_data_uri(uri: str) -> bytes | None:
        if not uri.startswith("data:") or "," not in uri:
            return None
        header, payload = uri.split(",", 1)
        if "base64" not in header:
            return None
        try:
            return base64.b64decode(payload)
        except (ValueError, TypeError):
            return None

    def _convert_chunk(self, chunk: Any) -> ExtractedChunk:
        metadata: dict[str, Any] = chunk.meta.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        text = self._chunker.contextualize(chunk).strip()
        section_path = [
            str(heading) for heading in metadata.get("headings", []) if str(heading).strip()
        ]
        doc_items = metadata.get("doc_items", [])
        source_refs: list[str] = []
        page_regions: list[dict[str, Any]] = []
        page_numbers: set[int] = set()
        item_labels: list[str] = []
        for item in doc_items if isinstance(doc_items, list) else []:
            if not isinstance(item, dict):
                continue
            label = item.get("label")
            if isinstance(label, str):
                item_labels.append(label)
            self_ref = item.get("self_ref")
            if isinstance(self_ref, str):
                source_refs.append(self_ref)
            provenance = item.get("prov", [])
            for region in provenance if isinstance(provenance, list) else []:
                if not isinstance(region, dict):
                    continue
                page_no = region.get("page_no")
                if isinstance(page_no, int):
                    page_numbers.add(page_no)
                page_regions.append(region)

        provenance_payload: dict[str, Any] = {
            "source_refs": source_refs,
            "page_regions": page_regions,
        }
        captions = metadata.get("captions")
        if captions:
            provenance_payload["captions"] = captions
        return ExtractedChunk(
            text=text,
            token_count=self._tokenizer.count_tokens(text),
            section_path=section_path,
            section_key=encode_section_key(section_path),
            is_heading_only=bool(item_labels)
            and all(label == "section_header" for label in item_labels),
            page_numbers=sorted(page_numbers),
            provenance=provenance_payload,
        )


def _error_summary(errors: list[Any]) -> str:
    """A bounded, log-safe summary of Docling's per-page errors.

    This is diagnostic only: it must never turn a conversion result into an
    exception of its own. Errors are user-visible through logs, not content.
    """
    try:
        parts: list[str] = []
        for error in errors[:10]:
            page = getattr(error, "page_no", None)
            category = getattr(error, "category", "unknown")
            component = getattr(error, "component_type", "unknown")
            message = str(getattr(error, "error_message", error)).replace("\n", " ")
            parts.append(
                f"page={page} category={category} component={component} message={message[:240]}"
            )
        if len(errors) > 10:
            parts.append(f"... {len(errors) - 10} more")
        return " | ".join(parts)
    except Exception:
        return "<error summary unavailable>"


def _page_count(document: Any) -> int | str:
    try:
        pages = getattr(document, "pages", None)
        return len(pages) if pages is not None else "unknown"
    except Exception:
        return "unknown"


def _allocation_failure(errors: list[Any]) -> str | None:
    """The first per-page error that says the parser ran out of memory, if any.

    Docling finishes a memory-starved conversion with ``partial_success`` and one
    error per page it could not read, so a caller that only inspects the status
    sees a successful parse of a nearly empty document.
    """
    for error in errors:
        message = " ".join(str(getattr(error, "error_message", error)).split())
        if not looks_like_allocation_failure(message):
            continue
        page = getattr(error, "page_no", None)
        return f"page {page}: {message[:240]}" if page is not None else message[:240]
    return None
