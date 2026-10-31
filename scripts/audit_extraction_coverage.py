#!/usr/bin/env python
"""Audit how much of a document's text actually reaches the chunks.

Runs the production native extractor and then probes the chunks it produced
against the pages it read: for every page, character spans sampled from the
page's text are looked up in the chunk text. A span that is missing means the
packer dropped or mangled prose on that page, and the audit says which page
and shows the span.

Read-only diagnostic: it touches no database, no object storage, and no
production code path. It does not write anything.

Usage:
    uv run --extra extraction python scripts/audit_extraction_coverage.py <path.pdf>
    uv run --extra extraction python scripts/audit_extraction_coverage.py <path.pdf> --json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.documents.validation import PDF_MEDIA_TYPE  # noqa: E402
from app.extraction.markdown_ir import build_pdf_pages  # noqa: E402
from app.extraction.native import NativeMarkdownExtractor  # noqa: E402

PROBE_CHARS = 48
MIN_PAGE_CHARS = 64

_MARKUP = re.compile(r"<br>|</?mark>|\*\*|__|`|^#+\s*", re.MULTILINE)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", _MARKUP.sub(" ", text)).strip()


def prose_segments(page_markdown: str) -> list[str]:
    """The page's paragraphs, markup stripped, tables excluded.

    Probes are sampled inside one paragraph and never across two, because a
    span that crosses a paragraph boundary cannot be expected to survive the
    packer: the chunk it would have to span may not exist.
    """
    segments: list[str] = []
    for paragraph in re.split(r"\n\s*\n", page_markdown):
        lines = [
            line
            for line in paragraph.splitlines()
            if line.strip() and not line.strip().startswith(("|", "!", "#", "<!--"))
        ]
        text = normalize(" ".join(lines))
        if len(text) >= PROBE_CHARS:
            segments.append(text)
    return segments


def table_cells(page_markdown: str) -> list[str]:
    """Every table cell value on the page worth finding in a chunk.

    The dashes separator row under a markdown header is layout, not content,
    so it never counts as a value to find.
    """
    values: list[str] = []
    for line in page_markdown.splitlines():
        if not line.strip().startswith("|"):
            continue
        if set(line.strip().replace("|", "").replace(" ", "")) <= set("-:"):
            continue
        for cell in line.strip().strip("|").split("|"):
            value = normalize(cell)
            if len(value) >= 3 and not set(value) <= set("-:. "):
                values.append(value)
    return values


@dataclass
class PageReport:
    page: int
    is_scan: bool
    page_chars: int
    probes: int = 0
    hits: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.hits / self.probes if self.probes else 1.0


@dataclass
class Report:
    path: str
    pages: int
    chunks: int
    chunk_chars: int
    elapsed_seconds: float
    page_reports: list[PageReport] = field(default_factory=list)

    @property
    def probes(self) -> int:
        return sum(page.probes for page in self.page_reports)

    @property
    def hits(self) -> int:
        return sum(page.hits for page in self.page_reports)

    @property
    def coverage(self) -> float:
        return self.hits / self.probes if self.probes else 1.0


def probe_page(page: object, chunk_blob: str, probes: int, rng: random.Random) -> PageReport:
    """Sample spans inside each paragraph, and look for every table cell.

    A paragraph span that is missing means the packer dropped or mangled
    prose on this page. A table cell that is missing means a value was lost
    in the row-wise serialization -- the format is allowed to differ, the
    content is not.
    """
    text = normalize(page.markdown)
    report = PageReport(page=page.page_no, is_scan=page.is_scan, page_chars=len(text))
    segments = prose_segments(page.markdown)
    for segment in segments:
        if len(segment) < PROBE_CHARS + 4:
            continue
        start = rng.randrange(0, len(segment) - PROBE_CHARS)
        span = segment[start : start + PROBE_CHARS]
        report.probes += 1
        if span in chunk_blob:
            report.hits += 1
        else:
            report.misses.append(span)
        if report.probes >= probes:
            break
    cells = table_cells(page.markdown)
    for value in cells:
        report.probes += 1
        if value in chunk_blob:
            report.hits += 1
        else:
            report.misses.append(f"[cell] {value}")
    return report


def analyse(pdf_path: Path, settings_like, probes: int, seed: int) -> Report:
    extractor = NativeMarkdownExtractor(
        chunk_max_tokens=settings_like.chunk_max_tokens,
        tokenizer_name=settings_like.chunk_tokenizer,
        scan_text_threshold=settings_like.native_scan_text_threshold,
        scan_page_describe=False,
    )

    print(f"parsing {pdf_path.name} ...", file=sys.stderr, flush=True)
    started = time.perf_counter()
    result = extractor.extract(pdf_path, media_type=PDF_MEDIA_TYPE)
    elapsed = time.perf_counter() - started
    pages, _page_count = build_pdf_pages(
        pdf_path, scan_text_threshold=settings_like.native_scan_text_threshold
    )
    print(
        f"  {len(result.chunks)} chunk(s), {elapsed:.1f}s",
        file=sys.stderr,
        flush=True,
    )

    chunk_blob = normalize("\n".join(chunk.text for chunk in result.chunks))
    chunk_blob = chunk_blob.replace(" > ", " ")
    rng = random.Random(seed)  # noqa: S311 - probe sampling, not security
    report = Report(
        path=pdf_path.name,
        pages=len(pages),
        chunks=len(result.chunks),
        chunk_chars=sum(len(chunk.text) for chunk in result.chunks),
        elapsed_seconds=elapsed,
    )
    for page in pages:
        report.page_reports.append(probe_page(page, chunk_blob, probes, rng))
    return report


def render(report: Report, top: int) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("EXTRACTION COVERAGE  (is each page's text present in a chunk?)")
    lines.append("=" * 72)
    lines.append(f"  document            {report.path}")
    lines.append(f"  pages               {report.pages}")
    lines.append(f"  chunks produced     {report.chunks}")
    lines.append(f"  chunk text chars    {report.chunk_chars:,}")
    lines.append(f"  elapsed             {report.elapsed_seconds:.1f}s")
    lines.append(f"  probes              {report.probes}")
    lines.append(f"  COVERAGE            {report.coverage:.1%}")
    lines.append("")
    worst = sorted(
        (page for page in report.page_reports if page.misses),
        key=lambda page: page.coverage,
    )[:top]
    if worst:
        lines.append(f"  weakest pages (worst {top}):")
        for page in worst:
            lines.append(
                f"    page {page.page:>4} {'scan' if page.is_scan else 'text'} "
                f"coverage {page.coverage:.0%}  ({len(page.misses)} miss(es))"
            )
            for span in page.misses[:2]:
                lines.append(f"      missing: {span[:72]}")
    else:
        lines.append("  every probe landed in a chunk.")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--probes", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = analyse(args.pdf, get_settings(), args.probes, args.seed)
    if args.json:
        print(
            json.dumps(
                {
                    "path": report.path,
                    "pages": report.pages,
                    "chunks": report.chunks,
                    "chunk_chars": report.chunk_chars,
                    "elapsed_seconds": round(report.elapsed_seconds, 2),
                    "coverage": round(report.coverage, 4),
                    "pages_detail": [
                        {
                            "page": page.page,
                            "is_scan": page.is_scan,
                            "chars": page.page_chars,
                            "probes": page.probes,
                            "hits": page.hits,
                            "misses": page.misses[:3],
                        }
                        for page in report.page_reports
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render(report, args.top))
    if report.coverage < 0.95:
        sys.exit(1)


if __name__ == "__main__":
    main()
