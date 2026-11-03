"""Reading a workbook the way a reader sees it, not the way it is stored.

An ``.xlsx`` is a zip of parts: one worksheet part per sheet, drawing parts
that anchor pictures and charts to cells, and a workbook part that names the
sheets and says which are hidden. Three kinds of reading have to cooperate:

* cells come from openpyxl in streaming (``read_only``) mode, so a
  million-row sheet is never fully resident;
* merged ranges, pictures and charts are read from the parts directly,
  because streaming mode does not decode drawings and a full load would
  parse every cell twice;
* a header is *detected*, not assumed: exports open with a title row, blank
  lines and a "downloaded at" stamp before the real table starts, and the
  header itself may be two rows of merged cells whose upper level names a
  group of columns.

Every judgement is recorded -- skipped sheets carry a reason, so a caller
can show what was read and what was passed over instead of guessing.
"""

import io
import posixpath
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from defusedxml.ElementTree import fromstring
from openpyxl import load_workbook

from app.documents.validation import uniquify_columns
from app.extraction.base import ExtractedImage
from app.extraction.xlsx_charts import parse_chart, render_chart, resolve_chart

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
GRAPHIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"

HEADER_SCAN_ROWS = 25
MAX_MERGE_RANGES = 2_000
MAX_HEADER_DEPTH = 3
HEADER_DENSITY = 0.6

_MERGE_REF = re.compile(rb'<mergeCell ref="([A-Z]+\d+:[A-Z]+\d+)"\s*/>')

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_REENCODE_TO_PNG = {".bmp", ".tif", ".tiff"}


class WorkbookError(Exception):
    """The workbook could not be read at all."""


class WorkbookTooLargeError(WorkbookError):
    """The workbook exceeds one of the ingest caps."""


@dataclass(frozen=True)
class SheetTable:
    """One sheet's table: its composed columns and a stream of its rows."""

    name: str
    columns: list[str]
    header_row: int
    rows: Iterator[dict[str, str]]


@dataclass(frozen=True)
class SkippedSheet:
    name: str
    reason: str


@dataclass(frozen=True)
class SheetPicture:
    image: ExtractedImage
    sheet: str


@dataclass(frozen=True)
class WorkbookReading:
    tables: list[SheetTable]
    skipped: list[SkippedSheet]
    pictures: list[SheetPicture]
    visuals_skipped: int
    """Drawings nothing can be made of: shapes, unrenderable chart types,
    media formats no decoder here opens, and anything past the image cap."""


@dataclass
class _SheetSpec:
    name: str
    part: str
    visible: bool
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass
class _Drawing:
    pictures: list[tuple[str, int, int]] = field(default_factory=list)
    charts: list[str] = field(default_factory=list)
    shapes: int = 0


def _column_number(letters: str) -> int:
    number = 0
    for character in letters:
        number = number * 26 + (ord(character) - 64)
    return number


def _split_cell(cell: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", cell.strip())
    if match is None:
        raise ValueError(cell)
    return _column_number(match.group(1)), int(match.group(2))


def _parse_merge_ref(ref: str) -> tuple[int, int, int, int] | None:
    try:
        start, end = ref.split(":")
        start_column, start_row = _split_cell(start)
        end_column, end_row = _split_cell(end)
    except ValueError:
        return None
    return (
        min(start_row, end_row),
        min(start_column, end_column),
        max(start_row, end_row),
        max(start_column, end_column),
    )


def _rels_path(part: str) -> str:
    directory, name = posixpath.split(part)
    return f"{directory}/_rels/{name}.rels"


def _resolve_target(base_directory: str, target: str) -> str | None:
    if target.startswith("/"):
        return target[1:]
    if "://" in target:
        return None
    return posixpath.normpath(posixpath.join(base_directory, target))


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    path = _rels_path(part)
    if path not in archive.namelist():
        return {}
    mapping: dict[str, str] = {}
    try:
        nodes = fromstring(archive.read(path))
    except Exception:
        return {}
    for node in nodes:
        if node.tag != f"{{{PACKAGE_REL_NS}}}Relationship":
            continue
        identifier = node.get("Id")
        if identifier is None or node.get("TargetMode") == "External":
            continue
        resolved = _resolve_target(posixpath.dirname(part), node.get("Target", ""))
        if resolved is not None:
            mapping[identifier] = resolved
    return mapping


def _merge_ranges(archive: zipfile.ZipFile, part: str) -> list[tuple[int, int, int, int]]:
    """The sheet's merged ranges, read without loading the sheet's cells.

    ``mergeCells`` sits after ``sheetData`` in the part, so a parsed read
    would walk every cell first; a chunked scan over the raw bytes costs one
    linear pass and stops at the same place a parser would have.
    """
    ranges: list[tuple[int, int, int, int]] = []
    tail = b""
    with archive.open(part) as stream:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            window = tail + chunk
            for match in _MERGE_REF.finditer(window):
                parsed = _parse_merge_ref(match.group(1).decode("ascii"))
                if parsed is not None:
                    ranges.append(parsed)
                if len(ranges) >= MAX_MERGE_RANGES:
                    return ranges
            tail = window[-64:]
    return ranges


def _map_workbook(archive: zipfile.ZipFile) -> list[_SheetSpec]:
    try:
        root = fromstring(archive.read("xl/workbook.xml"))
    except KeyError as exc:
        raise WorkbookError("The workbook has no sheet list.") from exc
    except Exception as exc:
        raise WorkbookError("The workbook sheet list is not readable.") from exc
    targets = _relationships(archive, "xl/workbook.xml")
    sheets: list[_SheetSpec] = []
    for node in root.iterfind(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet"):
        name = node.get("name")
        relationship_id = node.get(f"{{{REL_NS}}}id")
        if not name or relationship_id is None:
            continue
        part = targets.get(relationship_id)
        if part is None:
            continue
        sheets.append(
            _SheetSpec(
                name=name,
                part=part,
                visible=node.get("state") not in {"hidden", "veryHidden"},
            )
        )
    return sheets


def _read_drawing(archive: zipfile.ZipFile, drawing_part: str) -> _Drawing:
    drawing = _Drawing()
    targets = _relationships(archive, drawing_part)
    try:
        root = fromstring(archive.read(drawing_part))
    except Exception:
        return drawing
    for picture in root.iterfind(f".//{{{DRAWING_NS}}}pic"):
        blip = picture.find(f".//{{{DRAWING_NS}}}blipFill/{{{GRAPHIC_NS}}}blip")
        if blip is None:
            continue
        embedded = blip.get(f"{{{REL_NS}}}embed")
        if not embedded:
            continue
        drawing.pictures.append((targets.get(embedded, ""), 0, 0))
    for frame in root.iterfind(f".//{{{DRAWING_NS}}}graphicFrame"):
        chart_node = frame.find(f".//{{{CHART_NS}}}chart")
        if chart_node is None:
            continue
        drawing.charts.append(targets.get(chart_node.get(f"{{{REL_NS}}}id", ""), ""))
    drawing.shapes = len(root.findall(f".//{{{DRAWING_NS}}}sp"))
    return drawing


def _sheet_drawing(archive: zipfile.ZipFile, spec: _SheetSpec) -> _Drawing:
    merged = _Drawing()
    for target in _relationships(archive, spec.part).values():
        if not target.startswith("xl/drawings/") or not target.endswith(".xml"):
            continue
        drawing = _read_drawing(archive, target)
        merged.pictures.extend(drawing.pictures)
        merged.charts.extend(drawing.charts)
        merged.shapes += drawing.shapes
    return merged


def _render_value(value: object) -> str:
    """One cell as the string the row store keeps, matching what a reader sees.

    Dates become ISO-8601, whole numbers lose the decimal point a float
    carries, and booleans read as Excel spells them. Everything downstream
    -- the SQL agent's casts, the catalogue's column names -- already speaks
    about cells as text, so no format introduces a second cell dialect.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    return str(value)


def _class_of(value: object) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (bool, int, float)):
        return "number"
    if isinstance(value, (datetime, date, time)):
        return "date"
    return "text"


def _row_width(row: list[object]) -> int:
    width = 0
    for index, value in enumerate(row):
        if value is not None and (not isinstance(value, str) or value.strip()):
            width = index + 1
    return width


def _is_header_row(row: list[object]) -> bool:
    width = _row_width(row)
    filled = [value for value in row if value is not None and str(value).strip()]
    if len(filled) < 2 or width == 0:
        return False
    if len(filled) < max(2, int(HEADER_DENSITY * width)):
        return False
    for value in row[:width]:
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return False
    return True


def _body_is_tabular(
    rows: list[list[object]], start: int, width: int
) -> bool:
    """Whether the rows under a candidate header look like data.

    A table's body repeats: each column holds one kind of thing. Two or more
    non-empty rows whose per-column types are consistent are the smallest
    honest evidence; anything less is a title block or a note, not a table.
    """
    checked = 0
    classes: list[set[str]] = [set() for _ in range(width)]
    for row in rows[start:]:
        values = list(row[:width]) + [None] * max(0, width - len(row))
        if not any(value is not None and str(value).strip() for value in values):
            continue
        checked += 1
        for column, value in enumerate(values):
            kind = _class_of(value)
            if kind is not None:
                classes[column].add(kind)
        if checked >= 5:
            break
    if checked < 2:
        return False
    return all(len(kinds) <= 1 for kinds in classes)


def _find_header(rows: list[list[object]]) -> int | None:
    for index, row in enumerate(rows):
        if not _is_header_row(row):
            continue
        width = _row_width(row)
        if _body_is_tabular(rows, index + 1, width):
            return index
    return None


def _compose_columns(
    rows: list[list[object]],
    header_index: int,
    width: int,
    merges: list[tuple[int, int, int, int]],
) -> list[str]:
    """The header's names, with merged parent levels folded into them.

    A two-row header stores ``Penjualan`` once, merged across three columns,
    and ``Q1 Q2 Q3`` under it. The leaf row alone would name three anonymous
    ``Q1``s; folding each leaf's merged ancestors in gives
    ``Penjualan / Q1`` -- a name that survives on its own in a catalogue. A
    column whose single label is merged down over both header rows keeps
    that label alone; a leaf with no label falls back to a numbered column.
    """
    header_row_number = header_index + 1

    def label_of(merge: tuple[int, int, int, int]) -> str:
        start_row, start_column, _, _ = merge
        source = rows[start_row - 1] if start_row - 1 < len(rows) else []
        if len(source) <= start_column - 1:
            return ""
        return _render_value(source[start_column - 1]).strip()

    def covers(merge: tuple[int, int, int, int], row: int, column: int) -> bool:
        start_row, start_column, end_row, end_column = merge
        return start_row <= row <= end_row and start_column <= column <= end_column

    header = list(rows[header_index]) + [None] * max(0, width - len(rows[header_index]))
    names: list[str] = []
    for column in range(width):
        chain: list[str] = []
        used_rows: set[int] = set()
        leaf = _render_value(header[column]).strip()
        if leaf:
            chain.append(leaf)
        elif (
            own := next((m for m in merges if covers(m, header_row_number, column + 1)), None)
        ) is not None:
            used_rows.add(own[0])
            if own_label := label_of(own):
                chain.append(own_label)
        for _ in range(MAX_HEADER_DEPTH - 1):
            nearest: tuple[int, int, int, int] | None = None
            for merge in merges:
                start_row, start_column, end_row, end_column = merge
                covers_column = start_column <= column + 1 <= end_column
                if (
                    end_row >= header_row_number
                    or start_row in used_rows
                    or not covers_column
                ):
                    continue
                if nearest is None or start_row > nearest[0]:
                    nearest = merge
            if nearest is None:
                break
            used_rows.add(nearest[0])
            if (label := label_of(nearest)) and label not in chain:
                chain.insert(0, label)
        names.append(" / ".join(chain) if chain else f"column {column + 1}")
    return uniquify_columns(names)


class _MergeFiller:
    """Propagates a merged region's value across the region it covers.

    A merged data cell stores its value in the top-left corner alone; the
    reader of the grid sees it filling the whole region. Merges arrive
    sorted, so keeping the live ones per row stays cheap no matter how many
    rows follow.
    """

    def __init__(self, merges: list[tuple[int, int, int, int]]) -> None:
        self._upcoming = sorted(merges)
        self._live: list[tuple[int, int, int, int]] = []

    def apply(self, sheet_row: int, row: list[object]) -> list[object]:
        while self._upcoming and self._upcoming[0][0] <= sheet_row:
            merge = self._upcoming.pop(0)
            if merge[2] >= sheet_row:
                self._live.append(merge)
        self._live = [merge for merge in self._live if merge[2] >= sheet_row]
        for start_row, start_column, _, end_column in self._live:
            if sheet_row == start_row:
                continue
            for column in range(start_column, end_column + 1):
                if column - 1 >= len(row):
                    continue
                cell = row[column - 1]
                if cell is None or (isinstance(cell, str) and not cell.strip()):
                    row[column - 1] = (
                        row[start_column - 1] if start_column - 1 < len(row) else None
                    )
        return row


def _picture_from_bytes(
    data: bytes, suffix: str, ref: str, page_number: int
) -> ExtractedImage | None:
    media_type = _MEDIA_TYPES.get(suffix)
    if media_type is None:
        if suffix not in _REENCODE_TO_PNG:
            return None
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as source:
                buffer = io.BytesIO()
                source.convert("RGBA").save(buffer, format="PNG")
            data, media_type = buffer.getvalue(), "image/png"
        except Exception:
            return None
    if not data:
        return None
    return ExtractedImage(
        ref=ref,
        page_number=page_number,
        media_type=media_type,
        data=data,
        describe_hint="picture",
    )


def _collect_pictures(
    archive: zipfile.ZipFile,
    sheets: list[_SheetSpec],
    *,
    max_images: int,
) -> tuple[list[SheetPicture], list[tuple[str, bytes]], int]:
    """Lift the workbook's embedded pictures and stage its chart parts.

    Pictures leave as their own bytes -- the author's pixels, not a
    re-encoding. Chart parts are read here, while the archive is open, but
    drawn later: whether a chart's series are cached or only referenced,
    resolving them needs the workbook's cells, which are not parsed yet.
    """
    pictures: list[SheetPicture] = []
    pending_charts: list[tuple[str, bytes]] = []
    skipped = 0
    counter = 0
    for ordinal, spec in enumerate(sheets, start=1):
        drawing = _sheet_drawing(archive, spec)
        skipped += drawing.shapes
        for media_part, _, _ in drawing.pictures:
            if counter >= max_images:
                skipped += 1
                continue
            if not media_part or media_part not in archive.namelist():
                skipped += 1
                continue
            suffix = posixpath.splitext(media_part)[1].lower()
            try:
                data = archive.read(media_part)
            except Exception:
                skipped += 1
                continue
            counter += 1
            image = _picture_from_bytes(data, suffix, f"picture-{counter}", ordinal)
            if image is None:
                skipped += 1
                continue
            pictures.append(SheetPicture(image=image, sheet=spec.name))
        for chart_part in drawing.charts:
            if counter >= max_images:
                skipped += 1
                continue
            if not chart_part or chart_part not in archive.namelist():
                skipped += 1
                continue
            counter += 1
            try:
                pending_charts.append((spec.name, archive.read(chart_part)))
            except Exception:
                skipped += 1
    return pictures, pending_charts, skipped


_CHART_REFERENCE = re.compile(
    r"^(?:'((?:[^']|'')*)'|([A-Za-z0-9_.\- ]+))!"
    r"\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?\s*$"
)


def _read_range(workbook: Any, default_sheet: str, reference: str) -> list[object]:
    """The cells one chart reference names, read back from the parsed book.

    Chart references are formulas -- ``'Data'!$B$2:$B$5`` -- and streaming
    mode answers them by walking to the range, which is cheap for the
    top-of-sheet ranges charts almost always point at.
    """
    match = _CHART_REFERENCE.match(reference)
    if match is None:
        return []
    sheet_name = match.group(1) or match.group(2) or default_sheet
    first = f"{match.group(3)}{match.group(4)}"
    last = f"{match.group(5)}{match.group(6)}" if match.group(5) else first
    try:
        sheet = workbook[sheet_name]
        cells = sheet[f"{first}:{last}"]
    except Exception:
        return []
    if not isinstance(cells, tuple):
        cells = ((cells,),)
    return [cell.value for row in cells for cell in row]


def _draw_charts(
    pending: list[tuple[str, bytes]], workbook: Any
) -> tuple[list[SheetPicture], int]:
    pictures: list[SheetPicture] = []
    skipped = 0
    for ordinal, (sheet_name, xml) in enumerate(pending, start=1):
        chart = parse_chart(xml)

        def fetch(reference: str, sheet: str = sheet_name) -> list[object]:
            return _read_range(workbook, sheet, reference)

        if chart is not None:
            chart = resolve_chart(chart, fetch)
        if chart is None:
            skipped += 1
            continue
        pictures.append(
            SheetPicture(
                image=ExtractedImage(
                    ref=f"chart-{ordinal}",
                    page_number=ordinal,
                    media_type="image/png",
                    data=render_chart(chart),
                    caption=chart.title,
                    describe_hint="picture",
                ),
                sheet=sheet_name,
            )
        )
    return pictures, skipped


def _stream_sheet(
    source: Iterator[tuple],
    buffered: list[list[object]],
    *,
    start: int,
    name: str,
    columns: list[str],
    width: int,
    filler: _MergeFiller,
    max_rows: int,
) -> Iterator[dict[str, str]]:
    """The data rows of one detected table, as column-named strings.

    Bound at call time per sheet: a workbook's tables are consumed lazily,
    long after this loop has moved on, so the sheet's own width, columns
    and merge filler travel with the generator instead of being read from
    the enclosing scope later.
    """
    sheet_row = start + 1
    count = 0
    for row in [*buffered[start:], *source]:
        if not any(value is not None and str(value).strip() for value in row):
            continue
        count += 1
        if count > max_rows:
            raise WorkbookTooLargeError(
                f'Sheet "{name}" has more than {max_rows} data rows.'
            )
        values = list(row[:width]) + [None] * max(0, width - len(row))
        filled = filler.apply(sheet_row, values)
        sheet_row += 1
        yield {columns[column]: _render_value(filled[column]) for column in range(width)}


def read_workbook(
    path: Path,
    *,
    max_sheets: int,
    max_rows: int,
    max_columns: int,
    max_images: int,
) -> WorkbookReading:
    """Read every tabular sheet, skip the rest, and lift the pictures.

    Caps are enforced where the work happens: the sheet count against the
    workbook map, rows and columns while a sheet streams, images while the
    drawings are lifted. A sheet with no detectable table is skipped with a
    reason rather than failing the workbook -- one prose summary sheet must
    not take the numbers down with it.
    """
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise WorkbookError("The workbook is not a readable archive.") from exc
    with archive:
        sheets = _map_workbook(archive)
        if len(sheets) > max_sheets:
            raise WorkbookTooLargeError(
                f"The workbook has {len(sheets)} sheets; at most {max_sheets} is accepted."
            )
        for spec in sheets:
            spec.merges = _merge_ranges(archive, spec.part)
        pictures, pending_charts, visuals_skipped = _collect_pictures(
            archive, sheets, max_images=max_images
        )

    tables: list[SheetTable] = []
    skipped: list[SkippedSheet] = []
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise WorkbookError("The workbook's cells could not be read.") from exc
    try:
        chart_pictures, chart_skipped = _draw_charts(pending_charts, workbook)
        pictures.extend(chart_pictures)
        visuals_skipped += chart_skipped
        for spec in sheets:
            if not spec.visible:
                skipped.append(SkippedSheet(name=spec.name, reason="hidden sheet"))
                continue
            sheet = workbook[spec.name]
            rows = sheet.iter_rows(values_only=True)
            buffer: list[list[object]] = []
            for index, row in enumerate(rows):
                buffer.append(list(row))
                if index + 1 >= HEADER_SCAN_ROWS:
                    break
            header_index = _find_header(buffer)
            if header_index is None:
                skipped.append(
                    SkippedSheet(
                        name=spec.name,
                        reason=f"no table found in the first {HEADER_SCAN_ROWS} rows",
                    )
                )
                continue
            width = _row_width(buffer[header_index])
            if width > max_columns:
                raise WorkbookTooLargeError(
                    f'Sheet "{spec.name}" has {width} columns; at most {max_columns} is accepted.'
                )
            columns = _compose_columns(buffer, header_index, width, spec.merges)
            filler = _MergeFiller(spec.merges)

            tables.append(
                SheetTable(
                    name=spec.name,
                    columns=columns,
                    header_row=header_index + 1,
                    rows=_stream_sheet(
                        rows,
                        buffer,
                        start=header_index + 1,
                        name=spec.name,
                        columns=columns,
                        width=width,
                        filler=filler,
                        max_rows=max_rows,
                    ),
                )
            )
    finally:
        workbook.close()
    if not tables:
        raise WorkbookError("The workbook has no tabular sheet to store.")
    return WorkbookReading(
        tables=tables,
        skipped=skipped,
        pictures=pictures,
        visuals_skipped=visuals_skipped,
    )
