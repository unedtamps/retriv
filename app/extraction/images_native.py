"""Lifting pictures out of the source, without a layout model.

A raster picture embedded in a PDF is found by where it sits and how large
it is. Two things are deliberately not pictures: a region covering almost
the whole page -- that is a scanned page, whose words the OCR path reads --
and a region too small to describe. A letterhead logo repeated on every
page of a scan never reaches the vision model as forty-five separate
"pictures"; the full-page image it lives in is excluded, and its page is
handed over once as a whole for the model to judge whether anything worth
describing is on it.

A third kind of picture exists and leaves no raster behind: a chart drawn as
vector line-art. A 10-K's stock-performance graph is paths and rules, not an
image, so nothing in the source marks it as a picture and ``get_image_info``
cannot see it. Its axis labels are real text and survive regardless, but the
shape of the curve -- which series leads, when one crosses another, how
steep the rise is -- lives only in the geometry, and a reader without the
picture cannot state it.

Those are found by clustering the page's line-art and keeping the clusters
that look like a chart. The test is shaped by measurement rather than
intuition: clustering alone returns a table's rules as readily as a chart's
curves, and on a 1,824-page corpus the unfiltered result was almost entirely
table grids and text boxes. What separates them is that a trend line is
neither horizontal nor vertical, a series is often dashed, and a chart's
interior holds far less text than a table's cells do. Scored on those three
signals, the same corpus yielded 17 clusters of which 15 were data charts.

The trade deliberately favours precision. A chart missed here is missed
silently -- its numbers may still be in a table or in the surrounding prose
-- whereas a table admitted costs one vision call and one misleading line.
"""

import logging
import re

import pymupdf

from app.extraction.base import ExtractedImage
from app.extraction.markdown_ir import OCR_DPI, PageIR

logger = logging.getLogger(__name__)

_FULL_PAGE_AREA_RATIO = 0.8
_SCAN_FIGURE_SCALE = OCR_DPI / 72.0
"""The scale a scanned page's figure is cropped at.

It is the resolution OCR read the page at, so the crop carries the same
pixels the reader saw: a chart's axis labels were legible enough to
recognise, and they stay legible enough to describe.
"""

_CHART_MIN_WIDTH = 100.0
_CHART_MIN_HEIGHT = 70.0
_CHART_MIN_AREA_RATIO = 0.02
_CHART_MAX_AREA_RATIO = 0.85
_CHART_MIN_SCORE = 6
_CHART_TEXT_DENSITY_CEILING = 2.0
_CHART_SPARSE_TEXT_DENSITY = 1.0
_CHART_BAR_BONUS = 3
_CHART_MIN_BARS = 5

_CAPTION_PATTERN = re.compile(
    r"^(?:figure|gambar|chart|diagram|table|tabel)\s+\d*\s*[:.\-]?\s*(.{0,200})",
    re.IGNORECASE,
)


def collect_pdf_images(
    pdf: pymupdf.Document,
    pages: list[PageIR],
    *,
    picture_images_enabled: bool,
    image_render_scale: float,
    image_min_pixels: int,
    max_images: int,
    max_image_bytes: int,
    max_total_image_bytes: int,
    scan_page_describe: bool,
) -> list[ExtractedImage]:
    """Every picture the vision stage should see, under the byte and count caps.

    The count cap covers both kinds: lifted figures and scan pages offered
    for judgement. The byte caps apply as the bytes come into existence,
    because that is where an oversized picture can still be skipped instead
    of shipped.
    """
    if not picture_images_enabled:
        return []
    images: list[ExtractedImage] = []
    total_bytes = 0
    for page in pages:
        if max_images and len(images) >= max_images:
            break
        if page.is_scan:
            if not scan_page_describe:
                continue
            for index, figure in enumerate(page.figures):
                if max_images and len(images) >= max_images:
                    break
                rendered = _render_region(
                    pdf,
                    page.page_no,
                    (figure.x0, figure.y0, figure.x1, figure.y1),
                    scale=_SCAN_FIGURE_SCALE,
                    min_pixels=0,
                    max_image_bytes=max_image_bytes,
                    allow_full_page=True,
                )
                if rendered is None:
                    continue
                data, width, height = rendered
                if total_bytes + len(data) > max_total_image_bytes:
                    break
                total_bytes += len(data)
                images.append(
                    ExtractedImage(
                        ref=f"figure-p{page.page_no}-{index}",
                        page_number=page.page_no,
                        media_type="image/png",
                        data=data,
                        caption=_page_caption(page.markdown),
                        width=width,
                        height=height,
                        bbox=_normalized_page_bbox(
                            pdf,
                            page.page_no,
                            (figure.x0, figure.y0, figure.x1, figure.y1),
                        ),
                    )
                )
            continue
        refs: list[str] = []
        for info in pdf[page.page_no - 1].get_image_info(xrefs=True):
            if max_images and len(images) + len(refs) >= max_images:
                break
            rendered = _render_region(
                pdf,
                page.page_no,
                info["bbox"],
                scale=image_render_scale,
                min_pixels=image_min_pixels,
                max_image_bytes=max_image_bytes,
            )
            if rendered is None:
                continue
            data, width, height = rendered
            if total_bytes + len(data) > max_total_image_bytes:
                break
            total_bytes += len(data)
            ref = f"image-p{page.page_no}-{len(refs)}"
            images.append(
                ExtractedImage(
                    ref=ref,
                    page_number=page.page_no,
                    media_type="image/png",
                    data=data,
                    caption=_page_caption(page.markdown),
                    width=width,
                    height=height,
                    bbox=_normalized_page_bbox(
                        pdf, page.page_no, info["bbox"]
                    ),
                )
            )
            refs.append(ref)
    return images


def collect_vector_charts(
    pdf: pymupdf.Document,
    pages: list[PageIR],
    *,
    picture_images_enabled: bool,
    image_render_scale: float,
    image_min_pixels: int,
    max_images: int,
    max_image_bytes: int,
    max_total_image_bytes: int,
    skip_pages: set[int] | None = None,
) -> list[ExtractedImage]:
    """Charts drawn as vector line-art, rendered to the pixels a model can read.

    ``skip_pages`` holds the pages the raster pass already handled. A page with
    a lifted figure is not searched again: the chart on it was a bitmap, and
    re-drawing its line-art would describe the same shape twice under two refs.
    """
    if not picture_images_enabled:
        return []
    skip = skip_pages or set()
    images: list[ExtractedImage] = []
    total_bytes = 0
    for page in pages:
        if max_images and len(images) >= max_images:
            break
        if page.is_scan or page.page_no in skip:
            continue
        try:
            regions = _chart_regions(pdf, page.page_no)
        except Exception as exc:
            logger.warning(
                "Chart detection skipped a page",
                extra={"page": page.page_no, "error": str(exc)},
                exc_info=True,
            )
            continue
        for index, region in enumerate(regions):
            if max_images and len(images) >= max_images:
                break
            rendered = _render_region(
                pdf,
                page.page_no,
                (region.x0, region.y0, region.x1, region.y1),
                scale=image_render_scale,
                min_pixels=image_min_pixels,
                max_image_bytes=max_image_bytes,
            )
            if rendered is None:
                continue
            data, width, height = rendered
            if total_bytes + len(data) > max_total_image_bytes:
                break
            total_bytes += len(data)
            ref = f"chart-p{page.page_no}-{index}"
            images.append(
                ExtractedImage(
                    ref=ref,
                    page_number=page.page_no,
                    media_type="image/png",
                    data=data,
                    caption=_page_caption(page.markdown),
                    width=width,
                    height=height,
                    bbox=_normalized_page_bbox(
                        pdf,
                        page.page_no,
                        (region.x0, region.y0, region.x1, region.y1),
                    ),
                )
            )
    return images


def _normalized_page_bbox(
    pdf: pymupdf.Document,
    page_no: int,
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    page = pdf[page_no - 1]
    width = page.rect.width
    height = page.rect.height
    if width <= 0 or height <= 0:
        return None
    x0, y0, x1, y1 = bbox
    return (
        max(0.0, min(1.0, x0 / width)),
        max(0.0, min(1.0, y0 / height)),
        max(0.0, min(1.0, x1 / width)),
        max(0.0, min(1.0, y1 / height)),
    )


def _chart_regions(pdf: pymupdf.Document, page_no: int) -> list[pymupdf.Rect]:
    """The regions on one page that score as charts, in reading order.

    Background fills are dropped before clustering. A page painted with a
    full-bleed colour -- the prospectuses on the corpus are -- has hundreds of
    page-sized rectangles that merge into one cluster covering everything, and
    that cluster is then discarded for its size; without this the whole page
    is invisible to the search below.
    """
    page = pdf[page_no - 1]
    page_area = page.rect.width * page.rect.height
    if not page_area:
        return []
    drawings = [
        item
        for item in page.get_drawings()
        if pymupdf.Rect(item["rect"]).get_area() < _FULL_PAGE_AREA_RATIO * page_area
    ]
    if not drawings:
        return []
    try:
        clusters = page.cluster_drawings(drawings=drawings)
    except Exception:
        return []
    regions: list[pymupdf.Rect] = []
    for cluster in clusters:
        clip = pymupdf.Rect(
            max(cluster.x0, page.rect.x0),
            max(cluster.y0, page.rect.y0),
            min(cluster.x1, page.rect.x1),
            min(cluster.y1, page.rect.y1),
        )
        if clip.is_empty or clip.width < _CHART_MIN_WIDTH or clip.height < _CHART_MIN_HEIGHT:
            continue
        ratio = clip.get_area() / page_area
        if ratio < _CHART_MIN_AREA_RATIO or ratio > _CHART_MAX_AREA_RATIO:
            continue
        if _chart_score(page, clip, drawings) >= _CHART_MIN_SCORE:
            regions.append(clip)
    return regions


def _chart_score(
    page: pymupdf.Page, clip: pymupdf.Rect, drawings: list[dict]
) -> int:
    """How much a cluster looks like a chart rather than a table or a text box.

    Each signal is one thing a table does not do: draw a line that runs
    diagonally, break a series into dashes, leave its interior mostly empty of
    words, or stack bars of one width and many heights. The weights put a
    diagonal above the rest because it is the hardest to fake -- a table's
    rules are horizontal or vertical by construction.

    The bar signal exists because a bar chart fails every other test: it has no
    diagonal by definition, and the value label above each bar makes its text
    denser than a line chart's. On the annual-report corpus, five charts were
    missed for exactly that reason. Four points for the same signal admitted
    forty-three table cells instead, so the weight is three.
    """
    score = 0
    diagonals = 0
    dashed = 0
    filled: list[pymupdf.Rect] = []
    for item in drawings:
        rect = pymupdf.Rect(item["rect"])
        if not rect.intersects(clip):
            continue
        segments = sum(1 for entry in item.get("items", []) if entry[0] == "l")
        if segments >= 2 and rect.width > 2 and rect.height > 2:
            diagonals += 1
        dashes = item.get("dashes")
        if dashes and dashes not in ("[] 0", "[]0"):
            dashed += 1
        if item.get("fill") and rect.height > 8 and rect.width > 4:
            filled.append(rect)

    if diagonals >= 2:
        score += 3
    if diagonals >= 4:
        score += 1
    if dashed >= 1:
        score += 2
    if _bars(filled) >= _CHART_MIN_BARS:
        score += _CHART_BAR_BONUS

    text = page.get_text("dict", clip=clip)
    characters = sum(
        len(span["text"].strip())
        for block in text["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
    )
    density = characters / max(clip.get_area() / 1000, 1)
    if density < _CHART_TEXT_DENSITY_CEILING:
        score += 2
    if density < _CHART_SPARSE_TEXT_DENSITY:
        score += 1

    if len(drawings) >= 5:
        score += 1
    if clip.get_area() / max(page.rect.width * page.rect.height, 1) >= 0.05:
        score += 1
    return score


def _bars(filled: list[pymupdf.Rect]) -> int:
    """The longest run of filled rectangles that share a width and differ in height.

    A bar chart's bars are all one width and all different heights. A table's
    cells share their width too -- they share their height as well, which is
    what this excludes. Zebra striping is the remaining risk: a shaded table
    whose rows alternate both width and height can pass, which is why the
    score this feeds is one signal and not a decision on its own.
    """
    best = 0
    for width in {round(rect.width) for rect in filled}:
        same = [rect for rect in filled if abs(rect.width - width) <= 2]
        if len(same) < _CHART_MIN_BARS:
            continue
        if len({round(rect.height) for rect in same}) < 3:
            continue
        best = max(best, len(same))
    return best


def image_refs_by_page(images: list[ExtractedImage]) -> dict[int, list[str]]:
    """Every picture handle grouped by its page, in document order.

    Scan pages appear here too: their handle is the whole-page offer, and
    the chunker places it first on the page it belongs to.
    """
    grouped: dict[int, list[str]] = {}
    for image in images:
        grouped.setdefault(image.page_number, []).append(image.ref)
    return grouped


def _render_region(
    pdf: pymupdf.Document,
    page_no: int,
    bbox: tuple[float, float, float, float],
    *,
    scale: float,
    min_pixels: int,
    max_image_bytes: int,
    allow_full_page: bool = False,
) -> tuple[bytes, int, int] | None:
    """One picture cropped from its page, or ``None`` when skipped.

    ``allow_full_page`` is for a figure the layout model found on a scanned
    page: a photograph that fills the page is still a figure, whereas an
    embedded bitmap covering the whole page of a text document is the scan
    itself and is handled by the OCR path instead.
    """
    page = pdf[page_no - 1]
    region = pymupdf.Rect(bbox)
    page_area = page.rect.width * page.rect.height
    if not allow_full_page and page_area and region.get_area() > _FULL_PAGE_AREA_RATIO * page_area:
        return None
    estimated_pixels = region.get_area() * scale * scale
    if estimated_pixels < min_pixels:
        return None
    clipped = pymupdf.Rect(
        max(region.x0, page.rect.x0),
        max(region.y0, page.rect.y0),
        min(region.x1, page.rect.x1),
        min(region.y1, page.rect.y1),
    )
    if clipped.is_empty or clipped.width <= 0 or clipped.height <= 0:
        return None
    # A crop that lands over the byte cap is re-rendered smaller rather than
    # dropped: the same figure at a lower scale carries everything a model
    # reads, and dropping it would leave no picture, no marker and no row.
    effective = scale
    while True:
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(effective, effective), clip=clipped, alpha=False
        )
        data = pixmap.tobytes("png")
        if len(data) <= max_image_bytes:
            return data, pixmap.width, pixmap.height
        if effective <= 0.25:
            return None
        effective = max(0.25, effective * 0.75)


def _page_caption(markdown: str) -> str:
    """The nearest thing on the page that reads like a picture caption."""
    for line in markdown.splitlines():
        match = _CAPTION_PATTERN.match(line.strip())
        if match:
            return " ".join(match.group(0).split())
    return ""
