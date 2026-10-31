"""Lifting pictures out of the source, without a layout model.

A raster picture embedded in a PDF is found by where it sits and how large
it is. Two things are deliberately not pictures: a region covering almost
the whole page -- that is a scanned page, whose words the OCR path reads --
and a region too small to describe. A letterhead logo repeated on every
page of a scan never reaches the vision model as forty-five separate
"pictures"; the full-page image it lives in is excluded, and its page is
handed over once as a whole for the model to judge whether anything worth
describing is on it.

Vector graphics are out of scope for this extractor: on the corpus it
serves, every drawing cluster is a table rule or an underline, and the one
document whose pages are pictures is handled as pages. The current
pipeline, with a layout model, found no picture in either born-digital 10-K
either -- the ground truth is in the stored artifacts.
"""

import re

import pymupdf

from app.extraction.base import ExtractedImage
from app.extraction.markdown_ir import PageIR

_FULL_PAGE_AREA_RATIO = 0.8
_SCAN_PAGE_DPI = 120

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
            rendered = _render_page(pdf, page.page_no, _SCAN_PAGE_DPI)
            if rendered is None:
                continue
            data, width, height = rendered
            if len(data) > max_image_bytes:
                continue
            if total_bytes + len(data) > max_total_image_bytes:
                break
            total_bytes += len(data)
            images.append(
                ExtractedImage(
                    ref=f"scan-page-{page.page_no}",
                    page_number=page.page_no,
                    media_type="image/jpeg",
                    data=data,
                    caption=_page_caption(page.markdown),
                    width=width,
                    height=height,
                    describe_hint="scan_page",
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
                )
            )
            refs.append(ref)
    return images


def image_refs_by_page(images: list[ExtractedImage]) -> dict[int, list[str]]:
    """Every picture handle grouped by its page, in document order.

    Scan pages appear here too: their handle is the whole-page offer, and
    the chunker places it first on the page it belongs to.
    """
    grouped: dict[int, list[str]] = {}
    for image in images:
        grouped.setdefault(image.page_number, []).append(image.ref)
    return grouped


def scan_page_numbers(images: list[ExtractedImage]) -> set[int]:
    """The pages that were offered to the vision stage as whole pages."""
    return {
        image.page_number for image in images if image.describe_hint == "scan_page"
    }


def _render_region(
    pdf: pymupdf.Document,
    page_no: int,
    bbox: tuple[float, float, float, float],
    *,
    scale: float,
    min_pixels: int,
    max_image_bytes: int,
) -> tuple[bytes, int, int] | None:
    """One embedded image cropped from its page, or ``None`` when skipped."""
    page = pdf[page_no - 1]
    region = pymupdf.Rect(bbox)
    page_area = page.rect.width * page.rect.height
    if page_area and region.get_area() > _FULL_PAGE_AREA_RATIO * page_area:
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
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clipped, alpha=False)
    data = pixmap.tobytes("png")
    if len(data) > max_image_bytes:
        return None
    return data, pixmap.width, pixmap.height


def _render_page(pdf: pymupdf.Document, page_no: int, dpi: int) -> tuple[bytes, int, int] | None:
    pixmap = pdf[page_no - 1].get_pixmap(dpi=dpi)
    return pixmap.tobytes("jpeg"), pixmap.width, pixmap.height


def _page_caption(markdown: str) -> str:
    """The nearest thing on the page that reads like a picture caption."""
    for line in markdown.splitlines():
        match = _CAPTION_PATTERN.match(line.strip())
        if match:
            return " ".join(match.group(0).split())
    return ""
