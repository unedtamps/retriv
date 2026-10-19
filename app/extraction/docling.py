import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.extraction.base import ExtractedChunk, ExtractionError, ExtractionResult
from app.sections import encode_section_key


class DoclingExtractor:
    def __init__(
        self,
        *,
        ocr_languages: list[str],
        chunk_max_tokens: int,
        tokenizer_name: str,
        artifacts_path: Path | None = None,
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
        self._chunker = HybridChunker(
            tokenizer=self._tokenizer,
            merge_peers=True,
            always_emit_headings=True,
            serializer_provider=RowWiseSerializerProvider(),
        )
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
            document_json = json.dumps(
                document.export_to_dict(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                    coord_precision=3,
                    confid_precision=3,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            chunks = [self._convert_chunk(chunk) for chunk in self._chunker.chunk(document)]
        except Exception as exc:
            raise ExtractionError("Docling could not process the document") from exc
        return ExtractionResult(
            document_json=document_json,
            chunks=chunks,
            extractor_version=self._version,
            ocr_enabled=media_type == PDF_MEDIA_TYPE,
            source_text_chars=source_text_chars,
        )

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
            is_heading_only=bool(item_labels)
            and all(label == "section_header" for label in item_labels),
            page_numbers=sorted(page_numbers),
            provenance=provenance_payload,
        )
