"""The native markdown intermediate representation for PDF documents.

Each page becomes either markdown from its own text layer or, when the page
carries almost no text, lines lifted out of its raster by OCR. The pages are
then parsed into one stream of blocks -- headings, paragraphs, tables and
picture markers -- in reading order, which is the stream the chunker packs.

The heading structure is advisory. A heading is whatever the markdown says it
is, promoted where a recognisable landmark (``PART II``, ``Item 7.``,
``Pasal 16``) appears as plain text because the file's typography gives every
line the same size. A document whose headings come out wrong still navigates:
the ordinal carries position, the page numbers carry place, and the section
path only labels what the file itself made obvious.
"""

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pymupdf

from app.extraction.images import picture_marker
from app.extraction.layout import LAYOUT_DPI, FigureFinder, LayoutRegion

OCR_DPI = 200
"""The resolution a scanned page's words are read at."""

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
_MARKUP_PATTERN = re.compile(r"<mark>|</mark>|\*\*|__|`")
_IMAGE_LINE_PATTERN = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")
_HRULE_PATTERN = re.compile(r"^-{3,}\s*$")

LANDMARK_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^PART\s+[IVXLC]+\b", re.IGNORECASE), 1),
    (re.compile(r"^(BAB|BAGIAN)\s+(?:[IVXLC]+|\d+)\b", re.IGNORECASE), 1),
    (re.compile(r"^Chapter\s+(?:[A-Z]|\d+)\b", re.IGNORECASE), 1),
    (re.compile(r"^Item\s+\d+[A-Z]?\b[.:)]?", re.IGNORECASE), 2),
    (re.compile(r"^(Pasal|Article|Section)\s+\d+\b", re.IGNORECASE), 2),
    (re.compile(r"^(Regulation|Rule|Ayat)\s+\d+(?:\.\d+)?\b", re.IGNORECASE), 3),
)
"""Lines that start like a structural landmark are headings even when the
file set them in body type. The level is the one the landmark's own grammar
implies, which overrides whatever the font-size heuristic guessed."""

LANDMARK_MAX_LINE_CHARS = 160

_ONNX_THREAD_CAP = 4
_ONNX_THREADS_PINNED = False


def _pin_onnx_threads() -> None:
    """Cap the thread pools of every ONNX session built from here on.

    Importing pymupdf.layout eagerly constructs all of its inference sessions,
    and a session left unconfigured sizes its pool to every core the host
    reports. The models are too small for that fan-out to buy speed: it only
    burns CPU-seconds the sandbox counts against its CPU limit and stacks it
    cannot afford under its address-space cap. Sessions that set their own
    thread count, like the OCR engine's, pass through untouched.
    """
    global _ONNX_THREADS_PINNED
    if _ONNX_THREADS_PINNED:
        return
    try:
        import onnxruntime
    except ImportError:
        return
    built = onnxruntime.InferenceSession.__init__

    def init(
        self: Any,
        path_or_bytes: Any,
        sess_options: Any = None,
        providers: Any = None,
        provider_options: Any = None,
        **kwargs: Any,
    ) -> None:
        if sess_options is None:
            sess_options = onnxruntime.SessionOptions()
            sess_options.enable_cpu_mem_arena = False
        if sess_options.intra_op_num_threads == 0:
            sess_options.intra_op_num_threads = _ONNX_THREAD_CAP
        built(
            self,
            path_or_bytes,
            sess_options=sess_options,
            providers=providers,
            provider_options=provider_options,
            **kwargs,
        )

    onnxruntime.InferenceSession.__init__ = init
    _ONNX_THREADS_PINNED = True


_pin_onnx_threads()


@dataclass(frozen=True)
class SourceRegion:
    """A normalized page region a chunk came from.

    Coordinates are relative to the rendered page box in the range 0..1.
    Normalizing at extraction time keeps the viewer independent of DPI,
    zoom, and whether the page came from a text layer or a raster/OCR pass.
    """

    page_no: int
    bbox: tuple[float, float, float, float]
    kind: str = "text"
    ref: str = ""


@dataclass(frozen=True)
class PageLine:
    """One line read from a page, with the box it occupied if known."""

    text: str
    page_no: int
    bbox: tuple[float, float, float, float] | None = None
    kind: str = "text"
    ref: str = ""


@dataclass(frozen=True)
class PageIR:
    """One page's markdown and how it was read."""

    page_no: int
    markdown: str
    is_scan: bool
    text_layer_chars: int
    figures: tuple[LayoutRegion, ...] = ()
    """The regions a layout model called a figure, in reading order.

    Empty for a text page: its pictures are lifted by bbox from the file
    itself, which needs no model. For a scan these are what the vision stage
    is shown -- without them the whole page would go to the model, and most
    of those calls would be answered with "nothing here".
    """
    lines: tuple[PageLine, ...] = ()
    """The page's text lines, with normalized boxes when the reader had them."""


@dataclass(frozen=True)
class DocumentBlock:
    """One block of the reading-order stream the chunker packs."""

    kind: str
    text: str
    page_no: int
    level: int = 0
    ref: str = ""
    regions: tuple[SourceRegion, ...] = ()


def build_pdf_pages(
    path: Path,
    *,
    scan_text_threshold: int,
    figures: FigureFinder | None = None,
) -> tuple[list[PageIR], int]:
    """Read every page as markdown, OCR-ing the pages that have no text layer.

    A page whose own text is thinner than ``scan_text_threshold`` characters
    is a scan: its words come from the raster, via OCR, because no amount of
    parsing gets text out of pixels. The other pages go through the markdown
    converter in one pass, so heading sizes are ranked across the whole
    document rather than page by page. The converter's own OCR is switched
    off: left on, it spins up an engine sized to every core the host
    reports -- a thread bomb inside the sandbox -- and this pipeline already
    owns the OCR decision through :func:`ocr_page`.

    ``figures`` is the layout model, asked once per scanned page where its
    figures are. It is left out when nothing downstream would use the answer,
    because loading the model is the expensive part of asking.
    """
    with pymupdf.open(path) as pdf:
        page_texts = [page.get_text().strip() for page in pdf]
        text_pages = [
            index
            for index, text in enumerate(page_texts)
            if len(text) >= scan_text_threshold
        ]
        markdown_by_page = _markdown_for_text_pages(pdf, path, text_pages)
        pages: list[PageIR] = []
        for index, text in enumerate(page_texts):
            page_no = index + 1
            is_scan = len(text) < scan_text_threshold
            if is_scan:
                lines = ocr_page_lines(pdf, index)
                pages.append(
                    PageIR(
                        page_no=page_no,
                        markdown="\n".join(
                            promote_landmarks([line.text for line in lines])
                        ),
                        is_scan=True,
                        text_layer_chars=len(text),
                        figures=_page_figures(pdf, index, figures),
                        lines=tuple(lines),
                    )
                )
            else:
                markdown = markdown_by_page.get(index, "")
                pages.append(
                    PageIR(
                        page_no=page_no,
                        markdown="\n".join(promote_landmarks(markdown.splitlines())),
                        is_scan=False,
                        text_layer_chars=len(text),
                        lines=tuple(_pdf_text_lines(pdf[index], page_no)),
                    )
                )
        return pages, pdf.page_count


def _page_figures(
    pdf: pymupdf.Document, index: int, figures: FigureFinder | None
) -> tuple[LayoutRegion, ...]:
    """The figures on one scanned page, or nothing when no finder was given."""
    if figures is None:
        return ()
    pixmap = pdf[index].get_pixmap(dpi=LAYOUT_DPI)
    return tuple(figures.find(pixmap, dpi=LAYOUT_DPI))


def _markdown_for_text_pages(
    pdf: pymupdf.Document, path: Path, text_pages: list[int]
) -> dict[int, str]:
    import pymupdf4llm

    if not text_pages:
        return {}
    converted = pymupdf4llm.to_markdown(
        pdf,
        pages=text_pages,
        page_chunks=True,
        table_strategy="lines_strict",
        use_ocr=False,
    )
    if isinstance(converted, str):
        return {text_pages[0]: converted}
    by_page: dict[int, str] = {}
    for entry in converted:
        if not isinstance(entry, dict):
            continue
        page_number = (entry.get("metadata") or {}).get("page_number")
        if not isinstance(page_number, int) or not 1 <= page_number <= pdf.page_count:
            continue
        by_page[page_number - 1] = entry.get("text", "")
    if not by_page:
        for entry, page_index in zip(converted, text_pages, strict=False):
            text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
            by_page[page_index] = text
    return by_page


def _pdf_text_lines(page: pymupdf.Page, page_no: int) -> list[PageLine]:
    """The text layer's lines with normalized boxes.

    PyMuPDF reports coordinates in page points with the origin at the top
    left. Dividing by the page box turns them into the same 0..1 space the
    OCR path uses, so the viewer can draw either source without knowing which
    one produced a chunk.
    """
    width = page.rect.width or 1.0
    height = page.rect.height or 1.0
    lines: list[PageLine] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(
                str(span.get("text", "")) for span in line.get("spans", [])
            ).strip()
            bbox = line.get("bbox")
            if not text or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = (float(value) for value in bbox)
            if x1 <= x0 or y1 <= y0:
                continue
            lines.append(
                PageLine(
                    text=text,
                    page_no=page_no,
                    bbox=(x0 / width, y0 / height, x1 / width, y1 / height),
                )
            )
    return lines


def ocr_page_text(pdf: pymupdf.Document, page_index: int) -> str:
    """One scanned page as text lines, read from its raster by OCR."""
    return "\n".join(line.text for line in ocr_page_lines(pdf, page_index))


def ocr_page_lines(pdf: pymupdf.Document, page_index: int) -> list[PageLine]:
    """One scanned page as lines, with their normalized OCR boxes.

    The reader reports where on the page each fragment sits, so the lines are
    rebuilt top to bottom and left to right: a two-column statement keeps its
    rows intact instead of interleaving its halves. The fragment boxes are
    kept as the union of each line's boxes; this is the provenance the PDF
    viewer needs and it does not depend on the text existing in the PDF.
    """
    pixmap = pdf[page_index].get_pixmap(dpi=OCR_DPI)
    fragments = _ocr_engine().read(pixmap)
    if not fragments:
        return []
    width = pixmap.width or 1.0
    height = pixmap.height or 1.0
    heights = sorted(fragment[3] for fragment in fragments)
    median_height = heights[len(heights) // 2] or 12.0
    fragments.sort(key=lambda fragment: fragment[1] + fragment[3])
    lines: list[PageLine] = []
    current: list[tuple[float, float, float, float, str]] = []
    line_center = 0.0
    for fragment in fragments:
        left, top, _width, fragment_height, _text = fragment
        center = top + fragment_height / 2
        if current and center - line_center > median_height * 0.6:
            lines.append(_ocr_line(current, page_index + 1, width, height))
            current = []
        if not current:
            line_center = center
        current.append(fragment)
    if current:
        lines.append(_ocr_line(current, page_index + 1, width, height))
    return lines


def _ocr_line(
    fragments: list[tuple[float, float, float, float, str]],
    page_no: int,
    page_width: float,
    page_height: float,
) -> PageLine:
    ordered = sorted(fragments, key=lambda fragment: fragment[0])
    left = min(fragment[0] for fragment in fragments)
    top = min(fragment[1] for fragment in fragments)
    right = max(fragment[0] + fragment[2] for fragment in fragments)
    bottom = max(fragment[1] + fragment[3] for fragment in fragments)
    return PageLine(
        text=" ".join(fragment[4] for fragment in ordered).strip(),
        page_no=page_no,
        bbox=(
            left / page_width,
            top / page_height,
            right / page_width,
            bottom / page_height,
        ),
    )


class _RapidOcr:
    """The OCR reader, loaded once per process and shared by every page.

    The engine's thread pools are pinned low on purpose. Left automatic they
    size themselves to every core the host reports -- dozens of threads, each
    an 8 MB stack of virtual memory -- which inside a sandbox with an address
    space cap is how a reader of ten pages ends up frozen at the ceiling.

    The orientation classifier is switched off. It is meant to flip a text
    line that was scanned upside down, and on the all-caps monospace of a
    typed archival record it flipped upright lines instead: it reported 180
    degrees with 0.997 confidence on body lines whose words it then destroyed,
    while the same crop read perfectly with the classifier skipped. Three to
    four lines per page came out as noise, and raising the resolution made it
    worse rather than better. Pages are upright by the time they reach here.
    """

    _PARAMS = {
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Global.use_cls": False,
    }

    def __init__(self) -> None:
        from rapidocr import RapidOCR

        self._engine = RapidOCR(params=self._PARAMS)

    def read(self, pixmap: pymupdf.Pixmap) -> list[tuple[float, float, float, float, str]]:
        """One page as ``(left, top, right, bottom, text)`` fragments."""
        image = _pixmap_bytes(pixmap)
        result = self._engine(image)
        boxes = getattr(result, "boxes", None)
        texts = getattr(result, "txts", None)
        if boxes is None or texts is None:
            return []
        fragments: list[tuple[float, float, float, float, str]] = []
        for box, text in zip(boxes, texts, strict=False):
            if not str(text).strip():
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            fragments.append((min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys), str(text)))
        return fragments


def _pixmap_bytes(pixmap: pymupdf.Pixmap) -> bytes:
    return pixmap.tobytes("png")


_OCR: "_RapidOcr | None" = None


def _ocr_engine() -> "_RapidOcr":
    global _OCR
    if _OCR is None:
        _OCR = _RapidOcr()
    return _OCR


def promote_landmarks(lines: list[str]) -> list[str]:
    """Turn lines that start like a structural landmark into headings.

    The heading text keeps its own wording. A line already carrying markdown
    heading marks is left alone here; the block parser re-levels those whose
    text matches a landmark, so a ``PART II`` set in body size does not end
    up a level-six child of whatever preceded it.
    """
    promoted: list[str] = []
    for line in lines:
        if len(line) > LANDMARK_MAX_LINE_CHARS or line.lstrip().startswith(("#", "|", "!")):
            promoted.append(line)
            continue
        level = _landmark_level(line)
        if level is None:
            promoted.append(line)
        else:
            promoted.append(f"{'#' * level} {line.strip()}")
    return promoted


def _landmark_level(line: str) -> int | None:
    text = line.strip()
    if not text or len(text) > LANDMARK_MAX_LINE_CHARS:
        return None
    for pattern, level in LANDMARK_PATTERNS:
        if pattern.match(text):
            return level
    return None


def parse_markdown_blocks(
    markdown: str,
    page_no: int,
    lines: tuple[PageLine, ...] = (),
) -> list[DocumentBlock]:
    """One page's markdown as the block stream, in the order written.

    ``lines`` are the page's text lines with boxes. They are matched back to
    the blocks by normalized text after parsing; a markdown table or a
    synthesized heading does not match a single line, so a block with no
    match simply keeps an empty region list and the viewer falls back to the
    page.
    """
    blocks: list[DocumentBlock] = []
    paragraph: list[str] = []
    table: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = " ".join(part.strip() for part in paragraph if part.strip())
            if text:
                blocks.append(DocumentBlock(kind="text", text=text, page_no=page_no))
            paragraph.clear()

    def flush_table() -> None:
        if table:
            blocks.append(
                DocumentBlock(kind="table", text="\n".join(table), page_no=page_no)
            )
            table.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_table()
            continue
        if _IMAGE_LINE_PATTERN.match(stripped) or _HRULE_PATTERN.match(stripped):
            flush_paragraph()
            flush_table()
            continue
        heading = _HEADING_PATTERN.match(stripped)
        if heading is not None:
            flush_paragraph()
            flush_table()
            blocks.append(_heading_block(heading.group(1), heading.group(2), page_no))
            continue
        if stripped.startswith("|"):
            flush_paragraph()
            table.append(stripped)
            continue
        flush_table()
        paragraph.append(_MARKUP_PATTERN.sub("", stripped))
    flush_paragraph()
    flush_table()
    return _attach_regions(blocks, lines)


def _normalized_text(value: str) -> str:
    return " ".join(value.lower().split())


def _attach_regions(
    blocks: list[DocumentBlock],
    lines: tuple[PageLine, ...],
) -> list[DocumentBlock]:
    """Match page lines to blocks and attach their boxes.

    The match is deliberately conservative: a line shorter than four
    characters is ignored, and only a substring match counts. A block with
    no match keeps no regions instead of claiming the whole page.
    """
    available = [
        (_normalized_text(line.text), line)
        for line in lines
        if line.bbox is not None and len(_normalized_text(line.text)) >= 4
    ]
    if not available:
        return blocks
    result: list[DocumentBlock] = []
    for block in blocks:
        block_text = _normalized_text(block.text)
        matches: list[SourceRegion] = []
        seen: set[tuple[int, tuple[float, float, float, float]]] = set()
        for line_text, line in available:
            if line_text not in block_text and block_text not in line_text:
                continue
            key = (line.page_no, line.bbox)  # type: ignore[arg-type]
            if key in seen or line.bbox is None:
                continue
            seen.add(key)
            matches.append(
                SourceRegion(
                    page_no=line.page_no,
                    bbox=line.bbox,
                    kind=line.kind,
                    ref=line.ref,
                )
            )
        result.append(replace(block, regions=tuple(matches)) if matches else block)
    return result


def _heading_block(marks: str, text: str, page_no: int) -> DocumentBlock:
    level = len(marks)
    cleaned = " ".join(_MARKUP_PATTERN.sub("", text).split())
    landmark = _landmark_level(cleaned)
    if landmark is not None:
        level = landmark
    return DocumentBlock(kind="heading", text=cleaned, page_no=page_no, level=level)


def assemble_markdown(pages: list[PageIR]) -> str:
    """Every page's markdown, page-broken, for the stored document artifact."""
    parts: list[str] = []
    for page in pages:
        parts.append(f"<!-- page {page.page_no} {'(ocr)' if page.is_scan else ''} -->")
        parts.append(page.markdown)
    return "\n\n".join(parts).strip()


def insert_image_blocks(
    pages: list[PageIR],
    blocks: list[DocumentBlock],
    image_refs_by_page: dict[int, list[str]],
    image_regions: dict[str, SourceRegion] | None = None,
) -> list[DocumentBlock]:
    """Place picture markers into the block stream.

    A page's markers come after its text, scanned or not. On a text page that
    is simply where the converter can name them. On a scanned page it is the
    point of the exercise: the figure's caption and its axis labels are lines
    of that page's OCR text, so a description placed before them lands in the
    chunk that comes *before* the page -- the one talking about something
    else -- while a description placed after them lands in the chunk that
    already mentions the chart.
    """
    result: list[DocumentBlock] = []
    by_page: dict[int, list[DocumentBlock]] = {}
    for block in blocks:
        by_page.setdefault(block.page_no, []).append(block)
    for page in pages:
        result.extend(by_page.get(page.page_no, []))
        for ref in image_refs_by_page.get(page.page_no, []):
            region = image_regions.get(ref) if image_regions is not None else None
            result.append(
                DocumentBlock(
                    kind="image",
                    text=picture_marker(ref),
                    page_no=page.page_no,
                    regions=(region,) if region is not None else (),
                )
            )
    return result
