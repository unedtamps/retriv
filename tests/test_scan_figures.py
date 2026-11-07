"""The figures on a scanned page, found by the layout model.

A scan carries no bbox to point at, so a layout model answers where its
figures are. These tests drive the extractor with a stand-in engine: the real
model is a 125 MB download and is exercised by the integration test at the
bottom, which skips when it is not on disk.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.documents.validation import PDF_MEDIA_TYPE
from app.extraction import layout
from app.extraction.images import picture_marker, picture_refs
from app.extraction.layout import LayoutFigureFinder, LayoutRegion
from app.extraction.markdown_ir import _RapidOcr
from app.extraction.native import NativeMarkdownExtractor
from tests.document_samples import scanned_pdf_bytes

_PAGE_WIDTH = 612
_PAGE_HEIGHT = 792
_LAYOUT_DPI = 150
_SCALE = _LAYOUT_DPI / 72


def write_sample(tmp_path: Path, data: bytes, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def an_extractor(**overrides: Any) -> NativeMarkdownExtractor:
    options: dict[str, Any] = {
        "chunk_max_tokens": 700,
        "tokenizer_name": "cl100k_base",
        "picture_images_enabled": True,
        "scan_page_describe": True,
        "image_min_pixels": 0,
    }
    options.update(overrides)
    return NativeMarkdownExtractor(**options)


class FakeEngine:
    """A layout model that answers with whatever a test asked for."""

    def __init__(self, boxes: list[tuple[float, float, float, float]],
                 scores: list[float], names: list[str]) -> None:
        self.boxes = boxes
        self.scores = scores
        self.class_names = names

    def __call__(self, _image: bytes) -> SimpleNamespace:
        return SimpleNamespace(
            boxes=self.boxes, scores=self.scores, class_names=self.class_names
        )


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(
        boxes: list[tuple[float, float, float, float]],
        scores: list[float],
        names: list[str],
    ) -> FakeEngine:
        engine = FakeEngine(boxes, scores, names)
        monkeypatch.setattr(layout, "_engine", lambda _path: engine)
        monkeypatch.setattr(layout, "_ENGINE", None)
        monkeypatch.setattr(layout, "_ENGINE_KEY", None)
        return engine

    return install


def test_only_figures_are_taken_from_the_model(fake_engine: Any) -> None:
    """`table`, `header` and a low score are not figures; the rest converts."""
    fake_engine(
        boxes=[
            (150, 300, 1200, 1200),
            (150, 60, 1200, 200),
            (150, 1400, 1200, 1600),
        ],
        scores=[0.94, 0.95, 0.40],
        names=["image", "table", "chart"],
    )
    pixmap = SimpleNamespace(width=1, height=1, tobytes=lambda _fmt: b"")
    figures = LayoutFigureFinder().find(pixmap, dpi=_LAYOUT_DPI)  # type: ignore[arg-type]

    assert len(figures) == 1
    assert figures[0].kind == "image"
    assert figures[0].x0 == pytest.approx(150 * 72 / _LAYOUT_DPI)
    assert figures[0].y1 == pytest.approx(1200 * 72 / _LAYOUT_DPI)


@pytest.mark.integration
def test_a_scanned_page_is_cropped_to_its_figure(tmp_path: Path, fake_engine: Any) -> None:
    fake_engine(
        boxes=[(150, 300, 1200, 1200)],
        scores=[0.94],
        names=["image"],
    )
    path = write_sample(tmp_path, scanned_pdf_bytes(), "scan.pdf")
    result = an_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    assert len(result.images) == 1
    image = result.images[0]
    assert image.ref == "figure-p1-0"
    assert image.describe_hint == "picture"
    assert image.media_type == "image/png"
    assert image.page_number == 1
    expected_width = (1200 - 150) * 72 / _LAYOUT_DPI * 200 / 72
    assert image.width == pytest.approx(expected_width, rel=0.05)

    marked = next(chunk for chunk in result.chunks if picture_refs(chunk.text))
    assert marked.text.rstrip().endswith(picture_marker(image.ref)), (
        "the marker follows the page's words, so the description lands in the "
        "chunk that talks about the figure"
    )


@pytest.mark.integration
def test_a_scan_of_text_only_asks_the_vision_stage_nothing(
    tmp_path: Path, fake_engine: Any
) -> None:
    """The page used to be sent whole, so every scan cost one model call."""
    fake_engine(
        boxes=[(150, 60, 1200, 200), (150, 300, 1200, 1200)],
        scores=[0.95, 0.97],
        names=["header", "text"],
    )
    path = write_sample(tmp_path, scanned_pdf_bytes(), "scan.pdf")
    result = an_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.ocr_enabled is True
    assert result.images == []
    assert not any(picture_refs(chunk.text) for chunk in result.chunks)
    assert any("Pasal 16" in chunk.text for chunk in result.chunks)


@pytest.mark.integration
def test_the_orientation_classifier_is_off() -> None:
    """It flipped upright archival lines and destroyed their words.

    It reported 180 degrees with 0.997 confidence on body lines that read
    perfectly with the classifier skipped, and raising the resolution made it
    worse rather than better. This is the guard against it coming back.
    """
    assert _RapidOcr._PARAMS["Global.use_cls"] is False


@pytest.mark.integration
def test_the_real_model_finds_the_figure_when_it_is_installed(tmp_path: Path) -> None:
    """One page through the real model, skipped unless it is on disk.

    The model is a 125 MB download, so the test asks for it by path rather
    than reaching for the network: set ``RETRIV_LAYOUT_MODEL_PATH`` to run it.
    """
    model_path = os.environ.get("RETRIV_LAYOUT_MODEL_PATH")
    if not model_path:
        pytest.skip("set RETRIV_LAYOUT_MODEL_PATH to run the real layout model")
    pytest.importorskip("rapid_layout")

    finder = LayoutFigureFinder(model_path=model_path)
    path = write_sample(tmp_path, scanned_pdf_bytes(), "scan.pdf")
    import pymupdf

    with pymupdf.open(path) as pdf:
        pixmap = pdf[0].get_pixmap(dpi=_LAYOUT_DPI)
    try:
        regions = finder.find(pixmap, dpi=_LAYOUT_DPI)
    except layout.LayoutUnavailable:
        pytest.skip("the layout model is not available on this machine")
    assert isinstance(regions, list)
    assert all(isinstance(region, LayoutRegion) for region in regions)
