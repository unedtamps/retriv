"""What on this page is a figure, answered by a layout model.

A scanned page is one bitmap, so the extractor has nothing to point at. The
first attempt at this answered the question by hand -- subtract the OCR text
boxes from the render, then test what is left for size, ink, line-ness and
shape -- and it did not generalise. A bar chart is twelve pieces and each one
is too wide; a tinted page is one piece covering everything; a badge is a
small frame. Every document family added a case and a constant, and the
constants then fought each other: the gap that joins a bar chart's bars also
joins the stamp below the figure.

PP-DocLayout is trained for this exact question, and answers it once: what
kind of region is each part of the page, and where is it. `image` and `chart`
are the figures. `table`, `header`, `footer`, `figure_title` and the rest come
back too, which is how a table stops being mistaken for a figure at all.

It runs on the render alone -- no OCR -- so a page's layout costs about a
third of a second against the four its words cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import pymupdf

FIGURE_KINDS = frozenset({"image", "chart"})
"""The classes worth a vision call. A table's cells are already text; a seal
or a header image says nothing a question would ask about."""

LAYOUT_DPI = 150
"""The resolution the layout model reads a page at."""

DEFAULT_MIN_SCORE = 0.5


class LayoutUnavailable(Exception):
    """The layout model could not be loaded."""


@dataclass(frozen=True)
class LayoutRegion:
    """One region of a page: what it is, how sure, and where -- in page points."""

    kind: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float

    def as_rect(self) -> pymupdf.Rect:
        return pymupdf.Rect(self.x0, self.y0, self.x1, self.y1)


class FigureFinder(Protocol):
    """Whatever answers "where are the figures on this page"."""

    def find(self, pixmap: pymupdf.Pixmap, *, dpi: float) -> list[LayoutRegion]: ...


@dataclass(frozen=True)
class LayoutFigureFinder:
    """The layout model, as the extractor uses it."""

    model_path: str | None = None
    min_score: float = DEFAULT_MIN_SCORE

    def find(self, pixmap: pymupdf.Pixmap, *, dpi: float) -> list[LayoutRegion]:
        """The figures on one rendered page, in reading order."""
        engine = _engine(self.model_path)
        result = engine(pixmap.tobytes("png"))
        scale = 72.0 / dpi
        figures: list[LayoutRegion] = []
        for box, score, kind in zip(
            result.boxes, result.scores, result.class_names, strict=True
        ):
            if kind not in FIGURE_KINDS or float(score) < self.min_score:
                continue
            figures.append(
                LayoutRegion(
                    kind=str(kind),
                    score=float(score),
                    x0=float(box[0]) * scale,
                    y0=float(box[1]) * scale,
                    x1=float(box[2]) * scale,
                    y1=float(box[3]) * scale,
                )
            )
        figures.sort(key=lambda region: (round(region.y0, 1), round(region.x0, 1)))
        return figures


_ENGINE: Any = None
_ENGINE_KEY: str | None = None


def _engine(model_path: str | None) -> Any:
    """The model, loaded once per process and shared by every page.

    ``model_path`` points at an ONNX file; left empty, the package resolves
    and downloads its own copy the first time a page is read, the way the OCR
    models already arrive.
    """
    global _ENGINE, _ENGINE_KEY
    if _ENGINE is not None and model_path == _ENGINE_KEY:
        return _ENGINE
    try:
        from rapid_layout import ModelType, RapidLayout

        options: dict[str, Any] = {"model_type": ModelType.PP_DOC_LAYOUTV3}
        if model_path:
            options["model_dir_or_path"] = model_path
        _ENGINE = RapidLayout(**options)
    except Exception as exc:  # pragma: no cover - depends on the installed model
        raise LayoutUnavailable(
            f"The layout model could not be loaded ({exc}). It is baked into the "
            "worker image at build time; a worker without it cannot read a "
            "scanned page's figures."
        ) from exc
    _ENGINE_KEY = model_path
    return _ENGINE
