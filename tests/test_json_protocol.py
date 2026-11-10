import json
from pathlib import Path

import pytest

from app.documents.json_protocol import (
    JsonLimits,
    JsonProtocolError,
    json_schema,
    validate_json_protocol,
)

SCHEMA_PATH = Path("docs/schemas/retriv-json-v1.schema.json")


def limits(**overrides: int) -> JsonLimits:
    values = {
        "max_bytes": 1024 * 1024,
        "max_depth": 64,
        "max_nodes": 10_000,
        "max_rows": 100,
        "max_tables": 5,
        "max_columns": 10,
        "max_value_bytes": 1024,
        "max_row_bytes": 4096,
        "max_number_digits": 100,
    }
    values.update(overrides)
    return JsonLimits(**values)


def payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "protocol": "retriv-json",
        "version": 1,
        "tables": [
            {
                "name": "orders",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "total", "type": "number"},
                    {"name": "customer", "type": "object"},
                ],
                "records": [
                    {"id": 1001, "total": 42.5, "customer": {"name": "A"}},
                    {"id": 1002, "total": 17, "customer": None},
                ],
            }
        ],
    }
    value.update(overrides)
    return value


def table(
    name: str,
    *,
    columns: list[dict[str, object]] | None = None,
    records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "columns": columns or [{"name": "id"}],
        "records": records or [{"id": 1}],
    }


def encoded(value: object) -> bytes:
    return json.dumps(value).encode()


def error_code(value: object, *, cap: JsonLimits | None = None) -> str:
    with pytest.raises(JsonProtocolError) as caught:
        validate_json_protocol(encoded(value), cap or limits())
    return caught.value.code


def test_a_valid_payload_preserves_types_and_fills_missing_values() -> None:
    parsed = validate_json_protocol(encoded(payload()), limits())

    assert len(parsed.tables) == 1
    table = parsed.tables[0]
    assert table.name == "orders"
    assert table.columns == ["id", "total", "customer"]
    assert table.rows == [
        {"id": 1001, "total": 42.5, "customer": {"name": "A"}},
        {"id": 1002, "total": 17, "customer": None},
    ]
    assert table.schema == [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "total", "type": "number", "nullable": True},
        {"name": "customer", "type": "object", "nullable": True},
    ]


def test_multiple_tables_are_supported() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "id"}],
                "records": [{"id": 1}],
            },
            {
                "name": "customers",
                "columns": [{"name": "name"}],
                "records": [{"name": "A"}],
            },
        ]
    )
    parsed = validate_json_protocol(encoded(value), limits())
    assert [table.name for table in parsed.tables] == ["orders", "customers"]


def test_a_single_table_may_use_the_root_name() -> None:
    value = payload(
        tables=[
            {
                "name": "",
                "columns": [{"name": "id"}],
                "records": [{"id": 1}],
            }
        ]
    )
    parsed = validate_json_protocol(encoded(value), limits())
    assert parsed.tables[0].name == ""


def test_missing_columns_are_filled_with_null() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "id"}, {"name": "note"}],
                "records": [{"id": 1}],
            }
        ]
    )
    parsed = validate_json_protocol(encoded(value), limits())
    assert parsed.tables[0].rows == [{"id": 1, "note": None}]


def test_the_schema_file_is_generated_from_the_models() -> None:
    assert json.loads(SCHEMA_PATH.read_text()) == json_schema()


def test_a_bom_is_accepted() -> None:
    parsed = validate_json_protocol(b"\xef\xbb\xbf" + encoded(payload()), limits())
    assert len(parsed.tables) == 1


@pytest.mark.parametrize(
    "value, code",
    [
        ([1, 2], "invalid_json_protocol"),
        ({"version": 1, "tables": []}, "invalid_json_protocol"),
        (
            {"protocol": "retriv-json", "version": 2, "tables": []},
            "invalid_json_protocol",
        ),
        (
            {
                "protocol": "retriv-json",
                "version": 1,
                "tables": [],
                "extra": True,
            },
            "invalid_json_protocol",
        ),
    ],
)
def test_the_envelope_is_strict(value: object, code: str) -> None:
    assert error_code(value) == code


def test_duplicate_json_keys_are_rejected() -> None:
    raw = b'{"protocol":"retriv-json","version":1,"tables":[],"protocol":"retriv-json"}'
    with pytest.raises(JsonProtocolError) as caught:
        validate_json_protocol(raw, limits())
    assert caught.value.code == "json_duplicate_key"


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_non_finite_numbers_are_rejected(number: str) -> None:
    raw = (
        '{"protocol":"retriv-json","version":1,"tables":['
        '{"columns":[{"name":"value"}],"records":[{"value":'
        + number
        + "}]}]}"
    ).encode()
    with pytest.raises(JsonProtocolError) as caught:
        validate_json_protocol(raw, limits())
    assert caught.value.code == "invalid_json_number"


@pytest.mark.parametrize(
    "value, code",
    [
        (payload(tables=[table(""), table("other")]), "invalid_json_protocol"),
        (payload(tables=[table("a"), table("a")]), "invalid_json_protocol"),
        (
            payload(tables=[table("a", columns=[{"name": "id"}, {"name": "id"}])]),
            "invalid_json_protocol",
        ),
        (payload(tables=[table("bad'name")]), "invalid_json_protocol"),
        (payload(tables=[table("a", columns=[{"name": "bad\\name"}])]), "invalid_json_protocol"),
    ],
)
def test_table_and_column_names_are_safe_and_unique(value: object, code: str) -> None:
    assert error_code(value) == code


def test_undeclared_record_keys_are_rejected() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "id"}],
                "records": [{"id": 1, "extra": True}],
            }
        ]
    )
    assert error_code(value) == "invalid_json_protocol"


@pytest.mark.parametrize(
    "column_type, record_value",
    [
        ("string", 1),
        ("integer", 1.5),
        ("integer", True),
        ("number", "1"),
        ("boolean", 1),
        ("object", []),
        ("array", {}),
        ("null", 1),
    ],
)
def test_declared_types_are_enforced(column_type: str, record_value: object) -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "value", "type": column_type}],
                "records": [{"value": record_value}],
            }
        ]
    )
    assert error_code(value) == "invalid_json_protocol"


def test_a_non_nullable_column_rejects_null() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "id", "nullable": False}],
                "records": [{"id": None}],
            }
        ]
    )
    assert error_code(value) == "invalid_json_protocol"


def test_empty_records_are_rejected() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "id"}],
                "records": [],
            }
        ]
    )
    assert error_code(value) == "invalid_json_protocol"


def test_empty_columns_are_rejected() -> None:
    value = payload(tables=[{"name": "orders", "columns": [], "records": []}])
    assert error_code(value) == "invalid_json_protocol"


def test_the_row_cap_is_enforced() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "id"}],
                "records": [{"id": 1}, {"id": 2}],
            }
        ]
    )
    assert error_code(value, cap=limits(max_rows=1)) == "too_many_rows"


def test_the_column_cap_is_enforced() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "a"}, {"name": "b"}],
                "records": [{"a": 1, "b": 2}],
            }
        ]
    )
    assert error_code(value, cap=limits(max_columns=1)) == "too_many_columns"


def test_the_table_cap_is_enforced() -> None:
    value = payload(
        tables=[
            {"name": "a", "columns": [{"name": "id"}], "records": [{"id": 1}]},
            {"name": "b", "columns": [{"name": "id"}], "records": [{"id": 1}]},
        ]
    )
    assert error_code(value, cap=limits(max_tables=1)) == "too_many_tables"


def test_the_depth_cap_is_enforced() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "value"}],
                "records": [{"value": [[[[1]]]]}],
            }
        ]
    )
    assert error_code(value, cap=limits(max_depth=3)) == "json_too_deep"


def test_the_node_cap_is_enforced() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "value"}],
                "records": [{"value": [1, 2, 3, 4]}],
            }
        ]
    )
    assert error_code(value, cap=limits(max_nodes=10)) == "json_too_complex"


def test_the_value_size_cap_is_enforced() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "value"}],
                "records": [{"value": "x" * 20}],
            }
        ]
    )
    assert error_code(value, cap=limits(max_value_bytes=10)) == "json_value_too_large"


def test_the_row_size_cap_is_enforced() -> None:
    value = payload(
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "value"}],
                "records": [{"value": "x" * 100}],
            }
        ]
    )
    assert (
        error_code(value, cap=limits(max_value_bytes=1024, max_row_bytes=50))
        == "json_row_too_large"
    )


def test_the_byte_cap_is_enforced() -> None:
    value = payload()
    with pytest.raises(JsonProtocolError) as caught:
        validate_json_protocol(encoded(value), limits(max_bytes=10))
    assert caught.value.code == "json_too_large"


def test_invalid_utf8_and_nul_are_rejected() -> None:
    with pytest.raises(JsonProtocolError) as caught:
        validate_json_protocol(b"\xff\xfe{}", limits())
    assert caught.value.code == "invalid_json"

    with pytest.raises(JsonProtocolError) as caught:
        validate_json_protocol(b'{"protocol":"retriv-json","version":1,"tables":[]}\x00', limits())
    assert caught.value.code == "invalid_json"


def test_a_scalar_root_is_rejected() -> None:
    assert error_code(1) == "invalid_json_protocol"
