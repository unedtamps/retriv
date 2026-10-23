import base64
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.extraction.base import (
    ExtractedChunk,
    ExtractedImage,
    ExtractionError,
    ExtractionResult,
)
from app.sections import encode_section_key


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
    ) -> None:
        try:
            import tiktoken
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
            raise RuntimeError(
                "Docling extraction dependencies are not installed; "
                "install retriv-ingest[extraction]"
            ) from exc

        pipeline_options = PdfPipelineOptions(
            do_ocr=True,
            do_table_structure=True,
            artifacts_path=artifacts_path,
            # The layout model already tells a figure apart from a logo, a
            # rule and page furniture, and crops the figure out of the page
            # for us. Without this the pictures are found and then thrown
            # away, because the chunking serializer writes one as an empty
            # string.
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
        # OCR stays enabled for every PDF. Docling's PDF_AWARE_LAYOUT_REGIONS mode
        # already skips regions backed by the file's own text layer, so born-digital
        # PDFs do not reach Tesseract anyway -- and gating the stage off per file was
        # measured to change nothing while risking the scanned pages inside a mixed
        # document, where those pages have no text layer of their own.
        self._converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            },
        )
        self._tokenizer = OpenAITokenizer(
            tokenizer=tiktoken.get_encoding(tokenizer_name),
            max_tokens=chunk_max_tokens,
        )
        # always_emit_headings: without it the chunker consumes every heading as
        # context-only and drops it, so a heading whose section body is empty or
        # absent leaves no searchable text at all. contextualize() renders these
        # chunks with the heading text itself, so they are not empty and the
        # extractor's empty-chunk guard stays satisfied.
        #
        # The serializer writes tables one grid row per line, so the chunker
        # splits a long table on row boundaries instead of cutting a cell in
        # half, and repeats the header in every part. See app.extraction.tables.
        #
        # It also writes a picture as a marker naming it, so the chunk knows
        # which picture it holds. See app.extraction.pictures. With the
        # picture stage off, the picture is written as nothing at all, so
        # the document is indexed the way it was before the stage existed.
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
        self._image_min_pixels = image_min_pixels
        self._max_images = max_images
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
        if media_type == PDF_MEDIA_TYPE:
            try:
                source_text_chars, _pages = self.measure_pdf_text_layer(path)
            except Exception:
                # The guardrail treats zero as "no comparable text layer", which is the
                # safe reading: it declines to judge rather than guessing.
                source_text_chars = 0
        try:
            conversion = self._converter.convert(path)
            document = conversion.document
            images = self._collect_images(document)
            # A picture is only marked when it is one of the pictures that
            # will be described. A document whose pictures are all too small
            # to be worth an API call is indexed the way it would be with the
            # stage off, rather than with markers nothing will replace. When
            # some pictures are dropped but others are kept, the dropped ones
            # keep their captions but lose their markers: a logo does not
            # need a description, and an "[Image]" line where the logo was
            # only adds noise to the text that gets embedded.
            self._serializer_provider.mark_pictures(
                {image.ref for image in images}
            )
            document_json = self._export_document(document)
            chunks = [self._convert_chunk(chunk) for chunk in self._chunker.chunk(document)]
        except Exception as exc:
            raise ExtractionError("Docling could not process the document") from exc
        return ExtractionResult(
            document_json=document_json,
            chunks=chunks,
            extractor_version=self._version,
            ocr_enabled=media_type == PDF_MEDIA_TYPE,
            source_text_chars=source_text_chars,
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

    def _collect_images(self, document: Any) -> list[ExtractedImage]:
        """Lift the pictures out of the parsed document.

        The parser hands each picture over as a data URL of the cropped
        region. A picture whose rendered area is below the floor is dropped
        here: at that size it is a logo, a rule, a bullet or an ornament,
        and describing it costs an API call and adds nothing to a search.
        """
        if not self._picture_images_enabled:
            return []
        texts = {text.self_ref: text.text for text in document.texts}
        images: list[ExtractedImage] = []
        for picture in document.pictures:
            if self._max_images and len(images) >= self._max_images:
                break
            image = picture.image
            if image is None or not image.uri:
                continue
            data = self._decode_data_uri(str(image.uri))
            if data is None:
                continue
            width = int(image.size.width)
            height = int(image.size.height)
            if width * height < self._image_min_pixels:
                continue
            caption = " ".join(
                texts.get(ref.cref, "").strip() for ref in picture.captions
            ).strip()
            page_number = picture.prov[0].page_no if picture.prov else 0
            images.append(
                ExtractedImage(
                    ref=picture.self_ref,
                    page_number=page_number,
                    media_type=image.mimetype or "image/png",
                    data=data,
                    caption=caption,
                    width=width,
                    height=height,
                )
            )
        return images

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
            # The parser already knows which items are headings, so record that
            # rather than guessing from the token count. A heading emitted alone
            # is the signal that its content lives further down the tree, in
            # sub-sections that carry a deeper section_path.
            #
            # A picture is not a heading, so a chunk holding one is content even
            # when everything else in it is a heading -- which is right, since
            # the picture's description is content of its own.
            is_heading_only=bool(item_labels)
            and all(label == "section_header" for label in item_labels),
            page_numbers=sorted(page_numbers),
            provenance=provenance_payload,
        )
