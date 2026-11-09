"""The ``retriv-json`` v1 data protocol.

JSON files are data, not prose. Instead of inferring tables from an arbitrary
tree, an upload declares its tables, columns, types and records up front. The
same envelope is used by a ``.json`` file upload and by the direct JSON-body
ingestion endpoint, so validation and storage cannot diverge between them.

The parser is intentionally strict. Python's ``json`` module accepts duplicate
object names (last one wins) and ``NaN``/``Infinity`` by default; both would
turn a data error into silently different data. This module rejects them before
the envelope is validated.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import Settings

JSON_PROTOCOL = "retriv-json"
JSON_PROTOCOL_VERSION = 1

ColumnType = Literal[
    "any",
    "string",
    "number",
    "integer",
    "boolean",
    "object",
    "array",
    "null",
]

_TABLE_NAME_MAX = 100
_COLUMN_NAME_MAX = 256
_UNSAFE_NAME_CHARACTERS = frozenset("'\"\\;`")


class JsonProtocolError(ValueError):
    """The bytes do not follow the ``retriv-json`` protocol."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class JsonLimits:
    """The caps one protocol parse must stay inside."""

    max_bytes: int
    max_depth: int
    max_nodes: int
    max_rows: int
    max_tables: int
    max_columns: int
    max_value_bytes: int
    max_row_bytes: int
    max_number_digits: int = 1_000

    @classmethod
    def from_settings(cls, settings: Settings) -> JsonLimits:
        return cls(
            max_bytes=settings.max_json_bytes,
            max_depth=settings.max_json_depth,
            max_nodes=settings.max_json_nodes,
            max_rows=settings.max_json_rows,
            max_tables=settings.max_json_tables,
            max_columns=settings.max_csv_columns,
            max_value_bytes=settings.max_json_value_bytes,
            max_row_bytes=settings.max_json_row_bytes,
        )


@dataclass(frozen=True)
class JsonTableRows:
    """One validated table, with records already normalised to column order.

    ``schema`` keeps the declared type/nullability beside the row data for the
    run manifest; the row store itself only needs the names and values.
    """

    name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    schema: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class JsonData:
    """The validated product of one JSON payload."""

    tables: list[JsonTableRows]


class JsonColumnSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    type: ColumnType = "any"
    nullable: bool = True


class JsonTableSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = ""
    columns: list[JsonColumnSchema]
    records: list[dict[str, Any]]


class JsonEnvelopeSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal["retriv-json"]
    version: Literal[1]
    tables: list[JsonTableSchema]


def json_schema() -> dict[str, Any]:
    """The protocol's JSON Schema, generated from the same models we validate."""

    schema = JsonEnvelopeSchema.model_json_schema()
    schema["title"] = f"{JSON_PROTOCOL} v{JSON_PROTOCOL_VERSION}"
    return schema


def validate_json_protocol(data: bytes, limits: JsonLimits) -> JsonData:
    """Parse and validate one protocol payload, returning normalised rows."""

    text = _decode(data, limits)
    value = _parse(text, limits)
    _walk(value, limits)
    _check_envelope_keys(value)
    try:
        envelope = JsonEnvelopeSchema.model_validate(value)
    except ValidationError as exc:
        raise _from_validation_error(exc) from exc
    return _build_data(envelope, limits)


def _decode(data: bytes, limits: JsonLimits) -> str:
    if not data:
        raise JsonProtocolError("invalid_json", "The JSON payload is empty.")
    if len(data) > limits.max_bytes:
        raise JsonProtocolError(
            "json_too_large",
            f"The JSON payload exceeds the {limits.max_bytes}-byte limit.",
        )
    if b"\x00" in data:
        raise JsonProtocolError("invalid_json", "The JSON payload contains a NUL byte.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise JsonProtocolError("invalid_json", "The JSON payload must be UTF-8 text.") from exc
    if not text.strip():
        raise JsonProtocolError("invalid_json", "The JSON payload is empty.")
    return text


def _parse(text: str, limits: JsonLimits) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            parse_int=lambda value: _limited_int(value, limits),
        )
    except JsonProtocolError:
        raise
    except json.JSONDecodeError as exc:
        raise JsonProtocolError(
            "invalid_json",
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}.",
        ) from exc
    except RecursionError as exc:
        raise JsonProtocolError(
            "json_too_deep",
            f"The JSON payload is nested more than {limits.max_depth} levels deep.",
        ) from exc
    except (OverflowError, ValueError) as exc:
        raise JsonProtocolError(
            "invalid_json_number", "The JSON payload contains an invalid number."
        ) from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise JsonProtocolError(
                "json_duplicate_key",
                f"The JSON payload contains the duplicate key {key!r}.",
            )
        result[key] = item
    return result


def _reject_constant(value: str) -> Any:
    raise JsonProtocolError(
        "invalid_json_number",
        f"{value} is not a valid JSON number. Use null instead of NaN/Infinity.",
    )


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise JsonProtocolError(
            "invalid_json_number",
            f"The number {value!r} is not finite and cannot be stored as JSON.",
        )
    return number


def _limited_int(value: str, limits: JsonLimits) -> int:
    digits = value.lstrip("-")
    if not digits.isdigit() or len(digits) > limits.max_number_digits:
        raise JsonProtocolError(
            "invalid_json_number",
            f"The integer {value[:32]!r} exceeds the {limits.max_number_digits}-digit limit.",
        )
    return int(value)


def _walk(value: Any, limits: JsonLimits) -> None:
    """Bound depth, node count and string size before schema validation."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes:
            raise JsonProtocolError(
                "json_too_complex",
                f"The JSON payload contains more than {limits.max_nodes} values.",
            )
        if depth > limits.max_depth:
            raise JsonProtocolError(
                "json_too_deep",
                f"The JSON payload is nested more than {limits.max_depth} levels deep.",
            )
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise JsonProtocolError("invalid_json", "JSON object keys must be strings.")
                _check_string(key, limits, "key")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            _check_string(item, limits, "string")
        elif isinstance(item, float) and not math.isfinite(item):
            raise JsonProtocolError(
                "invalid_json_number",
                "The JSON payload contains a non-finite number.",
            )


def _check_string(value: str, limits: JsonLimits, kind: str) -> None:
    if "\x00" in value:
        raise JsonProtocolError("invalid_json", f"A JSON {kind} contains a NUL character.")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise JsonProtocolError("invalid_json", f"A JSON {kind} is not valid Unicode.") from exc
    if size > limits.max_value_bytes:
        raise JsonProtocolError(
            "json_value_too_large",
            f"A JSON {kind} is {size} bytes; the limit is {limits.max_value_bytes}.",
        )


def _check_envelope_keys(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise JsonProtocolError(
            "invalid_json_protocol",
            "The top-level JSON value must be an object with protocol, version and tables.",
        )
    if value.get("protocol") != JSON_PROTOCOL:
        raise JsonProtocolError(
            "invalid_json_protocol",
            f'The protocol field must be "{JSON_PROTOCOL}".',
        )
    version = value.get("version")
    if version != JSON_PROTOCOL_VERSION:
        raise JsonProtocolError(
            "invalid_json_protocol",
            f"Unsupported protocol version {version!r}; this server supports version "
            f"{JSON_PROTOCOL_VERSION}.",
        )
    extra = set(value) - {"protocol", "version", "tables"}
    if extra:
        names = ", ".join(sorted(str(name) for name in extra))
        raise JsonProtocolError(
            "invalid_json_protocol",
            f"Unknown top-level field(s): {names}. Expected protocol, version and tables.",
        )


def _from_validation_error(exc: ValidationError) -> JsonProtocolError:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error.get("loc", ()))
    message = str(error.get("msg", "is invalid"))
    detail = f"{location}: {message}" if location else message
    return JsonProtocolError("invalid_json_protocol", detail)


def _build_data(envelope: JsonEnvelopeSchema, limits: JsonLimits) -> JsonData:
    table_count = len(envelope.tables)
    if table_count > limits.max_tables:
        raise JsonProtocolError(
            "too_many_tables",
            f"The payload has {table_count} tables; the limit is {limits.max_tables}.",
        )

    table_names: set[str] = set()
    total_rows = 0
    tables: list[JsonTableRows] = []
    for table_index, table in enumerate(envelope.tables):
        _check_table_name(table.name, table_index, table_count)
        if table.name in table_names:
            raise JsonProtocolError(
                "invalid_json_protocol",
                f"tables[{table_index}].name: duplicate table name {table.name!r}.",
            )
        table_names.add(table.name)

        if not table.columns:
            raise JsonProtocolError(
                "invalid_json_protocol",
                f"tables[{table_index}].columns must contain at least one column.",
            )
        if len(table.columns) > limits.max_columns:
            raise JsonProtocolError(
                "too_many_columns",
                f"tables[{table_index}] has {len(table.columns)} columns; "
                f"the limit is {limits.max_columns}.",
            )

        column_names: list[str] = []
        schema: list[dict[str, Any]] = []
        columns_by_name: dict[str, JsonColumnSchema] = {}
        for column_index, column in enumerate(table.columns):
            _check_column_name(column.name, table_index, column_index)
            if column.name in columns_by_name:
                raise JsonProtocolError(
                    "invalid_json_protocol",
                    f"tables[{table_index}].columns[{column_index}].name: "
                    f"duplicate column name {column.name!r}.",
                )
            columns_by_name[column.name] = column
            column_names.append(column.name)
            schema.append(
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                }
            )

        rows: list[dict[str, Any]] = []
        for record_index, record in enumerate(table.records):
            base = f"tables[{table_index}].records[{record_index}]"
            if not isinstance(record, dict):
                raise JsonProtocolError(
                    "invalid_json_protocol",
                    f"{base}: every record must be a JSON object.",
                )
            extra = set(record) - set(columns_by_name)
            if extra:
                names = ", ".join(sorted(str(name) for name in extra))
                raise JsonProtocolError(
                    "invalid_json_protocol",
                    f"{base}: undeclared key(s) {names}. Add them to columns or remove them.",
                )
            row: dict[str, Any] = {}
            for column in table.columns:
                value = record.get(column.name)
                if value is None:
                    if not column.nullable:
                        raise JsonProtocolError(
                            "invalid_json_protocol",
                            f"{base}.{column.name}: null is not allowed (nullable=false).",
                        )
                else:
                    _check_column_type(value, column.type, f"{base}.{column.name}")
                row[column.name] = value
            _check_row_size(row, limits, base)
            rows.append(row)
            total_rows += 1
            if total_rows > limits.max_rows:
                raise JsonProtocolError(
                    "too_many_rows",
                    f"The payload has more than {limits.max_rows} records.",
                )

        tables.append(
            JsonTableRows(
                name=table.name,
                columns=column_names,
                rows=rows,
                schema=schema,
            )
        )

    if total_rows == 0:
        raise JsonProtocolError(
            "invalid_json_protocol",
            "The payload has no records. At least one record is required.",
        )
    return JsonData(tables=tables)


def _check_table_name(name: str, table_index: int, table_count: int) -> None:
    if not name:
        if table_count == 1:
            return
        raise JsonProtocolError(
            "invalid_json_protocol",
            f"tables[{table_index}].name: every table needs a name when there is more than one.",
        )
    if not _valid_name(name, _TABLE_NAME_MAX):
        raise JsonProtocolError(
            "invalid_json_protocol",
            f"tables[{table_index}].name: {name!r} is not a safe name of at most "
            f"{_TABLE_NAME_MAX} characters.",
        )


def _check_column_name(name: str, table_index: int, column_index: int) -> None:
    if not _valid_name(name, _COLUMN_NAME_MAX):
        raise JsonProtocolError(
            "invalid_json_protocol",
            f"tables[{table_index}].columns[{column_index}].name: {name!r} is not a "
            f"safe name of at most {_COLUMN_NAME_MAX} characters.",
        )


def _valid_name(value: str, max_length: int) -> bool:
    if not value or len(value) > max_length or value != value.strip():
        return False
    if any(character in _UNSAFE_NAME_CHARACTERS for character in value):
        return False
    return not any(ord(character) < 32 or ord(character) == 127 for character in value)


def _check_column_type(value: Any, column_type: ColumnType, path: str) -> None:
    if column_type == "any":
        return
    if column_type == "string":
        valid = isinstance(value, str)
    elif column_type == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif column_type == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif column_type == "boolean":
        valid = isinstance(value, bool)
    elif column_type == "object":
        valid = isinstance(value, dict)
    elif column_type == "array":
        valid = isinstance(value, list)
    else:  # null
        valid = value is None
    if not valid:
        expected = "null" if column_type == "null" else column_type
        raise JsonProtocolError(
            "invalid_json_protocol",
            f"{path}: expected {expected}, got {type(value).__name__}.",
        )


def _check_row_size(row: dict[str, Any], limits: JsonLimits, path: str) -> None:
    try:
        size = len(
            json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise JsonProtocolError(
            "invalid_json_protocol", f"{path}: the record is not serialisable."
        ) from exc
    if size > limits.max_row_bytes:
        raise JsonProtocolError(
            "json_row_too_large",
            f"{path}: the record is {size} bytes; the limit is {limits.max_row_bytes}.",
        )
