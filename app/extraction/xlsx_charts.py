"""Drawing a native chart again, from the numbers behind it.

A spreadsheet chart is not a picture: it is a recipe -- a type, some series
references, a title -- and the cells it points at. A parser that wants the
graph a reader sees has to draw that recipe.

The chart part may carry its resolved values next to its references (Excel
writes a ``numCache``), and then the recipe is complete on its own. Files
written by tools that skip the cache carry only the references, so the
caller hands in a way to read the referenced cells and the series are
resolved through it. Either way the re-render needs no formula evaluation.

The render is deliberately plain -- one subplot, standard colors, the title
and series names the author wrote. It exists so the vision stage can read
the graph's shape and values, not to reproduce the original styling.
"""

import io
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from defusedxml.ElementTree import fromstring

C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

_RENDERABLE = {
    "barChart",
    "lineChart",
    "areaChart",
    "pieChart",
    "doughnutChart",
    "scatterChart",
}

_CHART_MAX_POINTS = 5_000
"""One series is drawn with at most this many points.

A series cached with more points than that produces an unreadable ink block
and a vision call that describes nothing; the cap keeps the render bounded
and the description honest about what a reader could see."""


@dataclass(frozen=True)
class ChartSeries:
    name: str = ""
    categories: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    x_values: list[float] = field(default_factory=list)
    name_ref: str = ""
    category_ref: str = ""
    value_ref: str = ""
    x_ref: str = ""


@dataclass(frozen=True)
class ChartData:
    kind: str
    title: str
    series: list[ChartSeries]
    stacked: bool = False
    horizontal: bool = False


def parse_chart(xml: bytes) -> ChartData | None:
    """The chart a chart part describes, or ``None`` when it cannot be drawn.

    Charts outside the renderable set -- radar, surface, stock, the exotic
    combinations -- return ``None`` and the caller counts them as skipped
    rather than describing a wrong picture. Series may come back without
    values when the file cached none; ``resolve_chart`` fills or drops them.
    """
    try:
        root = fromstring(xml)
    except ET.ParseError:
        return None
    plot_area = root.find(f".//{{{C_NS}}}plotArea")
    if plot_area is None:
        return None
    kind_element = next(
        (child for child in plot_area if child.tag.rsplit("}", 1)[-1] in _RENDERABLE),
        None,
    )
    if kind_element is None:
        return None
    kind = kind_element.tag.rsplit("}", 1)[-1]
    series = [
        parsed
        for element in kind_element.findall(f"{{{C_NS}}}ser")
        if (parsed := _parse_series(element, kind)) is not None
    ]
    if not series:
        return None
    grouping = kind_element.find(f"{{{C_NS}}}grouping")
    stacked = grouping is not None and grouping.get("val") in {"stacked", "percentStacked"}
    direction = kind_element.find(f"{{{C_NS}}}barDir")
    horizontal = direction is not None and direction.get("val") == "bar"
    return ChartData(
        kind=kind,
        title=_chart_title(root),
        series=series,
        stacked=stacked,
        horizontal=horizontal,
    )


def resolve_chart(
    chart: ChartData, fetch: Callable[[str], list[object]]
) -> ChartData | None:
    """Fill series that carry references but no cached values.

    ``fetch`` reads one reference's cells, in whatever way the caller can.
    A series left with no values after this -- an unresolvable reference, a
    range of empty cells -- is dropped; a chart whose every series dropped
    returns ``None``, which the caller counts as a visual it cannot draw.
    """
    resolved: list[ChartSeries] = []
    for series in chart.series:
        values = series.values or _to_numbers(fetch(series.value_ref))
        if not values:
            continue
        categories = series.categories or _to_texts(fetch(series.category_ref))
        if not categories and chart.kind != "scatterChart":
            categories = [str(index + 1) for index in range(len(values))]
        x_values = series.x_values or (
            _to_numbers(fetch(series.x_ref)) if series.x_ref else []
        )
        if chart.kind == "scatterChart" and x_values:
            width = min(len(x_values), len(values))
            values, x_values = values[:width], x_values[:width]
        else:
            width = min(len(categories), len(values))
            categories, values = categories[:width], values[:width]
        resolved.append(
            replace(
                series,
                name=series.name
                or (
                    ", ".join(_to_texts(fetch(series.name_ref))) if series.name_ref else ""
                ),
                categories=categories,
                values=values,
                x_values=x_values,
            )
        )
    if not resolved:
        return None
    return replace(chart, series=resolved)


def _chart_title(root: ET.Element) -> str:
    texts = [node.text or "" for node in root.findall(f".//{{{C_NS}}}title//{{{A_NS}}}t")]
    return " ".join(" ".join(texts).split())


def _parse_series(element: ET.Element, kind: str) -> ChartSeries | None:
    if kind == "scatterChart":
        values = _cache_numbers(element, "val")
        x_values = _cache_numbers(element, "xVal")
        value_ref = _formula(element, "val")
        x_ref = _formula(element, "xVal")
        if not values and not value_ref:
            return None
        if not values and not x_values and not (value_ref and x_ref):
            return None
        return ChartSeries(
            name=_cache_texts(element, "tx"),
            name_ref=_formula(element, "tx"),
            values=values,
            value_ref=value_ref,
            x_values=x_values,
            x_ref=x_ref,
        )
    values = _cache_numbers(element, "val")
    value_ref = _formula(element, "val")
    if not values and not value_ref:
        return None
    return ChartSeries(
        name=_cache_texts(element, "tx"),
        name_ref=_formula(element, "tx"),
        categories=_cache_list(element, "cat"),
        category_ref=_formula(element, "cat"),
        values=values,
        value_ref=value_ref,
    )


def _formula(element: ET.Element, part: str) -> str:
    node = element.find(f"{{{C_NS}}}{part}")
    if node is None:
        return ""
    formula = node.findtext(f".//{{{C_NS}}}f")
    return formula.strip() if formula else ""


def _cache_numbers(element: ET.Element, part: str) -> list[float]:
    node = element.find(f"{{{C_NS}}}{part}")
    if node is None:
        return []
    points: list[float] = []
    for value in node.iterfind(f".//{{{C_NS}}}v"):
        try:
            points.append(float(value.text or ""))
        except ValueError:
            continue
        if len(points) >= _CHART_MAX_POINTS:
            break
    return points


def _cache_texts(element: ET.Element, part: str) -> str:
    return " | ".join(_cache_list(element, part))


def _cache_list(element: ET.Element, part: str) -> list[str]:
    node = element.find(f"{{{C_NS}}}{part}")
    if node is None:
        return []
    return [point.text or "" for point in node.iterfind(f".//{{{C_NS}}}pt/{{{C_NS}}}v")]


def _to_numbers(cells: list[object]) -> list[float]:
    numbers: list[float] = []
    for cell in cells:
        if isinstance(cell, bool) or not isinstance(cell, (int, float)):
            continue
        numbers.append(float(cell))
        if len(numbers) >= _CHART_MAX_POINTS:
            break
    return numbers


def _to_texts(cells: list[object]) -> list[str]:
    return [str(cell) for cell in cells if cell is not None and str(cell).strip()]


def render_chart(chart: ChartData) -> bytes:
    """The chart as a PNG a vision model can read."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(8.0, 4.5), dpi=110)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(1, 1, 1)
    if chart.kind in {"pieChart", "doughnutChart"}:
        _draw_pie(axes, chart)
    elif chart.kind == "barChart" and chart.horizontal:
        _draw_axes_chart(axes, chart)
        axes.grid(True, axis="x", alpha=0.3)
    else:
        _draw_axes_chart(axes, chart)
        axes.grid(True, axis="y", alpha=0.3)
    if chart.title:
        axes.set_title(chart.title)
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    return buffer.getvalue()


def _draw_pie(axes: Any, chart: ChartData) -> None:
    series = chart.series[0]
    pairs = sorted(
        zip(series.categories, series.values, strict=False), key=lambda pair: -pair[1]
    )
    labels = [f"{category or 'unnamed'}: {value:g}" for category, value in pairs]
    sizes = [value for _, value in pairs]
    colors = [f"C{index % 10}" for index in range(len(sizes))]
    wedges = axes.pie(
        sizes,
        colors=colors,
        wedgeprops={"width": 0.45 if chart.kind == "doughnutChart" else 1.0},
    )
    axes.legend(
        wedges[0],
        labels,
        loc="center left",
        bbox_to_anchor=(0.98, 0.5),
        fontsize=8,
    )


def _draw_axes_chart(axes: Any, chart: ChartData) -> None:
    if chart.kind == "scatterChart":
        for index, series in enumerate(chart.series):
            label = series.name or f"series {index + 1}"
            x = series.x_values or list(range(1, len(series.values) + 1))
            axes.scatter(x, series.values, s=18, label=label, color=f"C{index % 10}")
        axes.legend(fontsize=8)
        return
    categories = chart.series[0].categories
    horizontal = chart.kind == "barChart" and chart.horizontal
    for index, series in enumerate(chart.series):
        label = series.name or f"series {index + 1}"
        bottom = _stack_bottom(chart, index) if chart.stacked else None
        if horizontal:
            axes.barh(categories, series.values, label=label, bottom=bottom)
        elif chart.kind == "lineChart":
            axes.plot(categories, series.values, marker="o", markersize=3, label=label)
        elif chart.kind == "areaChart":
            axes.plot(categories, series.values, label=label)
            axes.fill_between(range(len(categories)), series.values, alpha=0.35)
        else:
            axes.bar(categories, series.values, label=label, bottom=bottom)
    axes.legend(fontsize=8)
    if len(categories) > 12:
        axes.tick_params(axis="x", labelsize=7, rotation=45)


def _stack_bottom(chart: ChartData, index: int) -> list[float]:
    return [
        sum(series.values[position] for series in chart.series[:index])
        for position in range(len(chart.series[index].values))
    ]
