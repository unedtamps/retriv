"""Tables, serialized one grid row per line so a table can be split on rows.

A table read as one line of text -- ``row, column = value`` for every cell,
all run together -- carries a cell's row label and column label with its
value, but gives a splitter no boundary to cut on. When a table is longer
than the chunk budget the cut lands inside a cell, and chunks end with
fragments like ``Page No. =``.

A table is not a paragraph. Its rows are the natural unit, and a row is the
unit a reader thinks in. So the table is serialized one row per line, and the
chunker's line-based packer cuts on row boundaries instead. The header is
repeated in every part: a part of a table that does not say which columns it
holds is a table of anonymous numbers.

Nothing here decides what a table *means*. The column labels are the ones
the detector produced, so a table whose header was not detected keeps the
positional labels it has rather than being given a header this module guessed
at. The first column is taken as the row label because that is the shape the
detectors produce, and a table whose first column is really a data column
keeps that reading rather than being corrected here.
"""

HEADER_SEPARATOR = " | "
"""Between the cells of the header line. A body line never contains it."""

VALUE_SEPARATOR = " = "
"""Between a cell's label and its value. Only a body line contains it."""


def clean_cell(value: object) -> str:
    """One cell as flat text: newlines and markdown line breaks become spaces."""
    text = str(value if value is not None else "")
    return " ".join(text.replace("<br>", " ").split())


def row_wise_table_text(
    header: list[str],
    rows: list[list[str]],
    *,
    has_header: bool,
) -> str:
    """Render a table as a header line followed by one line per row.

    With a header, the first row of ``rows`` is column labels: the header
    line joins them, every body line reads ``row label, column = value``.
    Without one, positional labels stand in and no header line is written.
    Empty cells are skipped: a cell with no value says nothing a reader can
    use. A row whose every cell is empty produces no line at all.
    """
    if has_header:
        labels = [clean_cell(cell) for cell in header[1:]]
        header_line = HEADER_SEPARATOR.join(clean_cell(cell) for cell in header)
    else:
        labels = [str(index + 1) for index in range(len(header) - 1)]
        header_line = ""
        rows = [header, *rows]

    body: list[str] = []
    for row in rows:
        row_label = clean_cell(row[0]) if row else ""
        cells: list[str] = []
        for offset, value in enumerate(row[1:]):
            text = clean_cell(value)
            if not text:
                continue
            label = labels[offset] if offset < len(labels) else str(offset + 1)
            if row_label:
                cells.append(f"{row_label}, {label} = {text}")
            else:
                cells.append(f"{label} = {text}")
        if cells:
            body.append(". ".join(cells))
        elif row_label:
            body.append(row_label)

    if not body:
        return header_line
    if not header_line:
        return "\n".join(body)
    return "\n".join([header_line, *body])


def markdown_table_cells(table_text: str) -> tuple[list[str], list[list[str]]] | None:
    """Parse a markdown pipe table into header and rows, dropping dead columns.

    A markdown table is a line of ``| a | b |`` per row between separator
    lines of dashes. Columns whose every cell is empty are layout artifacts
    of ruled tables -- separators between real columns -- and are dropped so
    they do not become anonymous columns in the serialized text. ``None``
    when the block does not actually parse as a table.
    """
    lines = [line.strip() for line in table_text.strip().splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 2 or not all(line.startswith("|") for line in lines):
        return None
    rows = [split_markdown_row(line) for line in lines]
    rows = [row for index, row in enumerate(rows) if index != 1 or is_separator_row(lines[1])]
    if len(rows) < 2:
        return None
    header, body = rows[0], rows[1:]
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header, body = padded[0], padded[1:]
    live = [
        column
        for column in range(width)
        if any(clean_cell(row[column]) for row in body) or clean_cell(header[column])
    ]
    if not live:
        return None
    return (
        [header[column] for column in live],
        [[row[column] for column in live] for row in body],
    )


def split_markdown_row(line: str) -> list[str]:
    """One markdown table line as cells, without the outer pipes."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [clean_cell(cell) for cell in text.split("|")]


def is_separator_row(line: str) -> bool:
    """Whether a markdown table line is the dashes separator under a header."""
    stripped = line.strip().strip("|").strip()
    return bool(stripped) and set(stripped) <= set("-: ")


def split_header_and_body(table_text: str) -> tuple[list[str], list[str]]:
    """Split serialized table text into the header line and the lines after it.

    The header is found by its shape rather than by remembering what the
    serializer wrote: it holds ``|`` and no ``=``, and the line after it
    holds ``=``. Whatever precedes the header is a preamble -- a caption --
    and stays in the first part only, which is where a caption belongs.
    """
    lines = table_text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if (
            HEADER_SEPARATOR in line
            and VALUE_SEPARATOR not in line
            and VALUE_SEPARATOR in lines[index + 1]
        ):
            return lines[: index + 1], lines[index + 1 :]
    return [], lines
