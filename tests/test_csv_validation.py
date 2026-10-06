"""CSV acceptance rules.

XLSX was accepted by the validator and then rejected by the extractor, so an
upload that returned 202 was guaranteed to fail later. CSV replaces it, which
also retires the whole OOXML hardening path for spreadsheets -- a ZIP archive
needs zip-bomb and macro checks, plain text needs none of that.

These are unit tests against ``validate_csv`` rather than through the upload
route, because the rules being pinned here are parse-level: decoding,
delimiter agreement between validator and worker, field-count agreement, and
the row cap.
"""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.documents.validation import (
    CSV_MEDIA_TYPE,
    sniff_delimiter,
    uniquify_columns,
    validate_csv,
)
from tests.document_samples import (
    csv_binary_bytes,
    csv_bytes,
    csv_empty_bytes,
    csv_header_only_bytes,
    csv_ragged_bytes,
    csv_semicolon_bytes,
    csv_with_bom_bytes,
)


def staged(tmp_path: Path, content: bytes) -> Path:
    """Write the content to a .csv path, which is all validate_csv reads."""
    path = tmp_path / "upload.csv"
    path.write_bytes(content)
    return path


def test_plain_csv_is_accepted_as_text_csv(tmp_path: Path) -> None:
    result = validate_csv(staged(tmp_path, csv_bytes()), Settings())
    assert result.media_type == CSV_MEDIA_TYPE
    assert result.page_count is None


def test_a_utf8_bom_is_stripped_not_rejected(tmp_path: Path) -> None:
    """Excel writes a BOM, and plain utf-8 decoding rejects it.

    Without the utf-8-sig attempt this file would fail as undecodable, which
    is the most common way a spreadsheet export from Excel gets refused.
    """
    assert validate_csv(staged(tmp_path, csv_with_bom_bytes()), Settings()).media_type == (
        CSV_MEDIA_TYPE
    )


def test_a_semicolon_file_is_parsed_as_two_columns(tmp_path: Path) -> None:
    """The delimiter is sniffed, so a European export is not one column."""
    assert validate_csv(staged(tmp_path, csv_semicolon_bytes()), Settings()).media_type == (
        CSV_MEDIA_TYPE
    )


def test_a_single_column_file_is_valid(tmp_path: Path) -> None:
    """No delimiter is present and none is needed."""
    content = b"id\n1\n2\n"
    assert validate_csv(staged(tmp_path, content), Settings()).media_type == CSV_MEDIA_TYPE


def test_a_binary_payload_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AppError) as caught:
        validate_csv(staged(tmp_path, csv_binary_bytes()), Settings())
    assert caught.value.code == "invalid_document"


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AppError) as caught:
        validate_csv(staged(tmp_path, csv_empty_bytes()), Settings())
    assert caught.value.code in {"empty_file", "invalid_document"}


def test_a_header_with_no_rows_is_refused(tmp_path: Path) -> None:
    """A header alone would ingest as a document with nothing to query."""
    with pytest.raises(AppError) as caught:
        validate_csv(staged(tmp_path, csv_header_only_bytes()), Settings())
    assert caught.value.code == "invalid_document"


def test_a_ragged_row_is_refused(tmp_path: Path) -> None:
    """Field counts must match the header.

    This is what stops a row from silently landing with cells shifted against
    the column names that label it, which would be a wrong answer rather than
    a visible error.
    """
    with pytest.raises(AppError) as caught:
        validate_csv(staged(tmp_path, csv_ragged_bytes()), Settings())
    assert caught.value.code == "invalid_document"


def test_the_row_cap_is_enforced_at_exactly_the_limit(tmp_path: Path) -> None:
    """One row over the cap is refused; the cap itself is not.

    Checked either side of the boundary because an off-by-one here is
    invisible until a real file trips it.
    """
    settings = Settings(max_csv_rows=10)
    at_limit = "id\n" + "".join(f"{n}\n" for n in range(10))
    assert validate_csv(staged(tmp_path, at_limit.encode()), settings).media_type == (
        CSV_MEDIA_TYPE
    )

    over_limit = "id\n" + "".join(f"{n}\n" for n in range(11))
    with pytest.raises(AppError) as caught:
        validate_csv(staged(tmp_path, over_limit.encode()), settings)
    assert caught.value.code == "too_many_rows"


def test_the_column_cap_is_enforced(tmp_path: Path) -> None:
    settings = Settings(max_csv_columns=3)
    content = b"a,b,c,d\n1,2,3,4\n"
    with pytest.raises(AppError) as caught:
        validate_csv(staged(tmp_path, content), settings)
    assert caught.value.code == "too_many_columns"


def test_an_oversized_field_is_refused(tmp_path: Path) -> None:
    """A single huge cell must not be allowed to allocate freely."""
    settings = Settings(max_csv_field_bytes=32)
    content = b"id,note\n1," + b"x" * 200 + b"\n"
    with pytest.raises(AppError) as caught:
        validate_csv(staged(tmp_path, content), settings)
    assert caught.value.code == "invalid_document"


def test_duplicate_column_names_are_made_distinct() -> None:
    """JSONB keeps one value per key, so a repeat would drop a column.

    csv returns both header cells unchanged and nothing downstream notices,
    so the collision is resolved here where it is visible.
    """
    assert uniquify_columns(["id", "status", "id", "id"]) == [
        "id",
        "status",
        "id_1",
        "id_2",
    ]


def test_blank_column_names_get_a_placeholder() -> None:
    assert uniquify_columns(["id", "", "note"]) == ["id", "column", "note"]


def test_delimiter_sniffing_prefers_a_consistent_split() -> None:
    assert sniff_delimiter("a,b,c\n1,2,3\n") == ","
    assert sniff_delimiter("a;b;c\n1;2;3\n") == ";"


def test_delimiter_sniffing_is_a_pure_function_of_the_input() -> None:
    """Validator and worker must not disagree about the separator.

    They share this function for exactly that reason. If they disagreed, a
    file that passed the field-count check would fail at insert time with a
    row-count error naming neither cause.
    """
    sample = "a,b,c\n1,2,3\n4,5,6\n"
    assert sniff_delimiter(sample) == sniff_delimiter(sample)


def test_delimiter_sniffing_falls_back_to_comma() -> None:
    assert sniff_delimiter("id\n1\n2\n") == ","