#!/usr/bin/env python
"""Audit how much of a document's text actually reaches the vector index.

Runs the production extraction pipeline (``app.extraction.docling.DoclingExtractor``)
and then compares the chunks it produced against the full parsed ``DoclingDocument``.
Text that Docling parsed but the chunker never emitted gets attributed to a
concrete cause: a content layer the serializer excludes, an item that serializes
to nothing, or a heading whose section body vanished.

Read-only diagnostic: it touches no database, no object storage, and no production
code path. It does not write anything.

Usage:
    uv run --extra extraction python scripts/audit_extraction_coverage.py <path.pdf>
    uv run --extra extraction python scripts/audit_extraction_coverage.py <path.pdf> --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import (
    DoclingDocument,
    SectionHeaderItem,
    TitleItem,
)

HEADING_TYPES = (TitleItem, SectionHeaderItem)

# Long enough that a match is meaningful, short enough to survive chunk-boundary
# whitespace differences.
PROBE_CHARS = 80
MIN_VERIFIABLE_CHARS = 24


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.documents.validation import PDF_MEDIA_TYPE  # noqa: E402
from app.extraction.docling import DoclingExtractor  # noqa: E402


@dataclass
class Item:
    ref: str
    label: str
    layer: str
    text: str
    is_heading: bool = False
    page_no: int | None = None

    @property
    def size(self) -> int:
        return len(self.text)

    def probe(self) -> str:
        """A distinctive, whitespace-normalised slice of the item's text.

        Markdown decoration (``## ``) is stripped so that a heading is matched the
        way it is actually embedded: ``contextualize()`` prefixes the raw heading
        text, not the serialised markdown form.
        """
        flat = normalize(self.text)
        flat = re.sub(r"^#+\s*", "", flat)
        return flat[:PROBE_CHARS].strip()


@dataclass
class Report:
    pages: int = 0
    chunks: int = 0
    chunk_chars: int = 0
    total_chars: int = 0
    covered_chars: int = 0
    items: list[Item] = field(default_factory=list)
    covered_refs: set[str] = field(default_factory=set)
    excluded_refs: set[str] = field(default_factory=set)
    headings: list[Item] = field(default_factory=list)
    searchable: dict[str, bool | None] = field(default_factory=dict)
    chunk_blob: str = ""

    def is_searchable(self, item: Item) -> bool | None:
        """Whether this item's text ends up in a chunk, i.e. reaches the index.

        Text presence is the ground truth, not provenance: merging can legitimately
        widen a chunk's text without listing every contributing ``doc_items`` ref.
        Returns None when the item is too short to verify without false positives.
        """
        probe = item.probe()
        if len(probe) < MIN_VERIFIABLE_CHARS:
            return None
        return probe in self.chunk_blob

    def missing_reason(self, item: Item) -> str | None:
        if self.is_searchable(item):
            return None
        if item.ref in self.excluded_refs:
            return "layer-excluded"
        if not self.text_present(item):
            return "no-serialized-text"
        if self.is_searchable(item) is None:
            return "too-short-to-verify"
        return "text-not-in-any-chunk"

    def text_present(self, item: Item) -> bool:
        return bool(normalize(item.text))

    @property
    def missing(self) -> list[tuple[Item, str]]:
        return [
            (item, reason)
            for item in self.items
            if (reason := self.missing_reason(item)) is not None
        ]


def first_page_no(node: object) -> int | None:
    """Page number of an item's first provenance region, 1-based for display."""
    prov = getattr(node, "prov", None)
    if not prov:
        return None
    pages = [p.page_no for p in prov if getattr(p, "page_no", None) is not None]
    return min(pages) if pages else None


def collect_items(document: DoclingDocument, serializer: ChunkingDocSerializer) -> list[Item]:
    """Every leaf item in the document, across all content layers.

    Walks with ``included_content_layers`` set to every layer, because the whole
    point is to see what the production chunker excludes.
    """
    items: list[Item] = []
    visited: set[str] = set()
    for node, _level in document.iterate_items(
        with_groups=False,
        included_content_layers=set(ContentLayer),
    ):
        ref = node.self_ref
        if ref in visited:
            continue
        visited.add(ref)
        try:
            text = serializer.serialize(item=node, visited=visited).text or ""
        except Exception:  # a malformed item must not abort the audit
            text = ""
        items.append(
            Item(
                ref=ref,
                label=str(getattr(node, "label", "") or type(node).__name__),
                layer=str(getattr(node, "content_layer", "body") or "body"),
                text=text,
                is_heading=isinstance(node, HEADING_TYPES),
                page_no=first_page_no(node),
            )
        )
    return items


def analyse(
    pdf_path: Path,
    settings: Settings,
    artifacts: Path | None,
    no_ocr: bool = False,
) -> Report:
    if settings.docling_artifacts_path is not None and not settings.docling_artifacts_path.is_dir():
        # The configured path is usually the in-container location
        # (/opt/docling-models), which does not exist on a workstation. Falling back
        # to None makes Docling use its default per-user model cache instead.
        print(
            f"note: configured docling artifacts path "
            f"{settings.docling_artifacts_path} does not exist; using default model cache",
            file=sys.stderr,
            flush=True,
        )
        settings = settings.model_copy(update={"docling_artifacts_path": artifacts})

    extractor = DoclingExtractor(
        ocr_languages=settings.ocr_languages,
        chunk_max_tokens=settings.chunk_max_tokens,
        tokenizer_name=settings.chunk_tokenizer,
        artifacts_path=settings.docling_artifacts_path,
    )

    ocr_enabled = not no_ocr
    source_text_chars = 0
    try:
        source_text_chars, _pages = extractor.measure_pdf_text_layer(pdf_path)
    except Exception:
        source_text_chars = 0
    print(
        f"note: ocr_enabled={ocr_enabled} source_text_chars={source_text_chars:,}",
        file=sys.stderr,
        flush=True,
    )

    print(f"parsing {pdf_path.name} ...", file=sys.stderr, flush=True)
    if no_ocr:
        extractor._converter.format_to_options[  # noqa: SLF001
            InputFormat.PDF
        ].pipeline_options.do_ocr = False
    conversion = extractor._converter.convert(pdf_path)  # noqa: SLF001
    document = conversion.document
    print(f"  parsed {len(document.pages)} page(s)", file=sys.stderr, flush=True)

    print("chunking ...", file=sys.stderr, flush=True)
    result = extractor.extract(pdf_path, media_type=PDF_MEDIA_TYPE)
    print(f"  produced {len(result.chunks)} chunk(s)", file=sys.stderr, flush=True)

    serializer = ChunkingDocSerializer(doc=document)
    report = Report(
        pages=len(document.pages),
        chunks=len(result.chunks),
        chunk_chars=sum(len(c.text) for c in result.chunks),
    )
    report.excluded_refs = set(serializer.get_excluded_refs())
    report.items = collect_items(document, serializer)

    report.chunk_blob = normalize("\n".join(c.text for c in result.chunks))
    for chunk in result.chunks:
        refs = (chunk.provenance or {}).get("source_refs", [])
        report.covered_refs.update(r for r in refs if isinstance(r, str))

    for item in report.items:
        if item.is_heading:
            report.headings.append(item)
        report.total_chars += item.size
        searchable = report.is_searchable(item)
        report.searchable[item.ref] = searchable
        if searchable:
            report.covered_chars += item.size

    return report


def render(report: Report, top: int) -> str:
    lines: list[str] = []
    pct = (report.covered_chars / report.total_chars * 100) if report.total_chars else 0.0

    lines.append("=" * 72)
    lines.append("EXTRACTION COVERAGE  (is the text present in a chunk?)")
    lines.append("=" * 72)
    lines.append(f"  pages               {report.pages}")
    lines.append(f"  chunks produced     {report.chunks}")
    lines.append(f"  chunk text chars    {report.chunk_chars:,}")
    lines.append(f"  document item chars{report.total_chars:>13,}")
    lines.append(f"  covered chars       {report.covered_chars:,}")
    lines.append(f"  COVERAGE            {pct:.1f}%")
    unverified = sum(1 for v in report.searchable.values() if v is None)
    if unverified:
        lines.append(f"  (plus {unverified} item(s) too short to verify)")
    lines.append("")

    headings = report.headings
    lost_headings = [h for h in headings if report.searchable.get(h.ref) is not True]
    found_headings = len(headings) - len(lost_headings)
    lines.append("-" * 72)
    lines.append(f"HEADINGS  {found_headings}/{len(headings)} searchable")
    lines.append("-" * 72)
    if lost_headings:
        lines.append(f"  {len(lost_headings)} heading(s) never reach any chunk text:")
        for heading in lost_headings[:top]:
            page = f"p{heading.page_no:<3}" if heading.page_no else "p?  "
            preview = heading.probe()[:88]
            lines.append(f"    {page} {heading.layer.split('.')[-1]:<10} {preview}")
        if len(lost_headings) > top:
            lines.append(f"    ... and {len(lost_headings) - top} more")
    else:
        lines.append("  every heading text appears in some chunk")
    lines.append("")

    missing = report.missing
    lines.append("-" * 72)
    lines.append(f"MISSING TEXT  {len(missing)} item(s)")
    lines.append("-" * 72)

    by_reason: dict[str, list[Item]] = defaultdict(list)
    for item, reason in missing:
        by_reason[reason].append(item)

    for reason in sorted(by_reason, key=lambda r: -sum(i.size for i in by_reason[r])):
        group = by_reason[reason]
        chars = sum(i.size for i in group)
        share = chars / report.total_chars * 100 if report.total_chars else 0.0
        lines.append(
            f"\n  [{reason}]  {len(group)} item(s), {chars:,} chars, "
            f"{share:.1f}% of document"
        )

        per_layer: dict[str, list[Item]] = defaultdict(list)
        for item in group:
            per_layer[item.layer].append(item)
        for layer in sorted(per_layer, key=lambda k: -sum(i.size for i in per_layer[k])):
            layer_items = per_layer[layer]
            layer_chars = sum(i.size for i in layer_items)
            layer_share = layer_chars / report.total_chars * 100 if report.total_chars else 0.0
            lines.append(
                f"     layer {layer.split('.')[-1]:<10} {len(layer_items):>4} item(s)  "
                f"{layer_chars:>9,} chars  {layer_share:>5.1f}%"
            )
            for label, count in Counter(i.label for i in layer_items).most_common(6):
                lines.append(f"        {label:<28} {count:>4}")

    unreferenced = [
        (i, r) for i, r in missing if r == "text-not-in-any-chunk" and not i.is_heading
    ]
    if unreferenced:
        lines.append("")
        lines.append("-" * 72)
        lines.append("SAMPLES: body text that is searchable nowhere (not headings)")
        lines.append("-" * 72)
        for item, _reason in unreferenced[:top]:
            page = f"p{item.page_no:<3}" if item.page_no else "p?  "
            lines.append(f"  {page} {item.label:<14} {item.probe()[:80]}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def to_json(report: Report) -> str:
    pct = (report.covered_chars / report.total_chars * 100) if report.total_chars else 0.0
    missing = report.missing
    by_layer: dict[str, dict[str, int]] = defaultdict(lambda: {"items": 0, "chars": 0})
    for item, _reason in missing:
        entry = by_layer[item.layer]
        entry["items"] += 1
        entry["chars"] += item.size
    return json.dumps(
        {
            "pages": report.pages,
            "chunks": report.chunks,
            "chunk_chars": report.chunk_chars,
            "document_chars": report.total_chars,
            "covered_chars": report.covered_chars,
            "coverage_pct": round(pct, 2),
            "headings_total": len(report.headings),
            "headings_searchable": sum(
                1 for v in report.searchable.values() if v is True
            ),
            "headings_lost": [
                {"label": h.label, "layer": h.layer, "text": h.probe()[:200]}
                for h in report.headings
                if report.searchable.get(h.ref) is not True
            ],
            "missing_items": len(missing),
            "missing_by_layer": dict(by_layer),
        },
        indent=2,
        ensure_ascii=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--top", type=int, default=25, help="rows per section")
    parser.add_argument(
        "--artifacts-path",
        type=Path,
        default=None,
        help="Docling models directory (default: Docling's user cache)",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="disable OCR to measure coverage on a text PDF",
    )
    args = parser.parse_args()

    if not args.pdf.is_file():
        print(f"not a file: {args.pdf}", file=sys.stderr)
        return 2

    settings = Settings()
    report = analyse(args.pdf, settings, args.artifacts_path, args.no_ocr)

    print(to_json(report) if args.json else render(report, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
