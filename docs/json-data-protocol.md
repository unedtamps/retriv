# Retriv JSON data protocol v1

Retriv accepts JSON as **data**, not as prose. A JSON upload or POST body declares
its tables, columns and records explicitly; the server does not infer a table from
an arbitrary tree. That keeps the catalogue, the SQL surface and the citations
deterministic.

A payload is accepted only if it follows this envelope:

```json
{
  "protocol": "retriv-json",
  "version": 1,
  "tables": [
    {
      "name": "orders",
      "columns": [
        {"name": "id", "type": "integer", "nullable": false},
        {"name": "customer", "type": "object"},
        {"name": "total", "type": "number", "nullable": false},
        {"name": "note", "type": "string"}
      ],
      "records": [
        {
          "id": 1001,
          "customer": {"name": "A"},
          "total": 42.5,
          "note": null
        }
      ]
    }
  ]
}
```

The generated JSON Schema is
[`schemas/retriv-json-v1.schema.json`](schemas/retriv-json-v1.schema.json).

## Envelope

| Field | Rule |
|---|---|
| `protocol` | Required, exactly `"retriv-json"` |
| `version` | Required, integer `1` |
| `tables` | Required, 1–20 tables |
| top-level extras | Rejected |

## Tables

| Field | Rule |
|---|---|
| `name` | Optional for a single table (defaults to the root table `""`); required for multiple tables; unique; at most 100 characters |
| `columns` | Required, 1–512; names unique within the table |
| `records` | Required array of objects |

Names may contain Unicode letters/digits, spaces, `_`, `.` and `-`. Quotes,
backslashes, semicolons, control characters and leading/trailing whitespace are
rejected so the name can be used safely in the SQL the agent writes.

A single table may be unnamed. The root table is stored with `sheet = ''`, the
same way a CSV is stored. Multiple tables use their declared names as `sheet`.

## Columns

| Field | Rule |
|---|---|
| `name` | Required, unique, at most 256 characters, same safe-name rules as table names |
| `type` | Optional: `any` (default), `string`, `number`, `integer`, `boolean`, `object`, `array`, `null` |
| `nullable` | Optional boolean, default `true` |

Types are checked at ingestion. `object` and `array` validate the top-level value
only; nested structure is kept as JSONB and can be queried with `data->'field'`.
Numbers must be finite; booleans are not accepted as integers/numbers.

## Records

- Every record must be a JSON object.
- Every key must be declared in `columns`; undeclared keys are rejected.
- Missing keys become `null`. A missing or null value in a `nullable: false`
  column is rejected.
- Record order is preserved as `row_number`.
- At least one record is required across all tables.

## Limits

| Limit | Default |
|---|---:|
| JSON file or body | 25 MiB (`RETRIV_MAX_JSON_BYTES`) |
| Tables | 20 |
| Columns per table | 512 |
| Total records | 1,000,000 |
| Nesting depth | 64 |
| Total values | 2,000,000 |
| String value | 64 KiB |
| Serialised record | 1 MiB |

The parser rejects duplicate object keys, `NaN`, `Infinity`, `-Infinity`,
non-finite floats, oversized integers, NUL bytes and non-UTF-8 input.

## Uploading

A `.json` file upload uses the normal document endpoint:

```bash
curl -X POST "http://localhost:9000/v1/workspaces/$WORKSPACE_ID/documents" \
  -H "Authorization: Bearer $JWT" \
  -F "file=@orders.json;type=application/json"
```

## Posting data directly

The same envelope can be posted without creating a file first:

```bash
curl -X POST "http://localhost:9000/v1/workspaces/$WORKSPACE_ID/documents/json?filename=orders.json" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  --data-binary @orders.json
```

The body is validated, staged as a canonical JSON document, scanned, stored and
enqueued exactly like an uploaded file. Deduplication is by SHA-256; posting the
same bytes again returns the existing document with `200` and `duplicate=true`.
New content returns `202`. The direct endpoint requires the dashboard JWT and
the workspace `upload` permission; API keys remain read-only.

`filename` is optional and defaults to `data.json`. A name without a suffix gets
`.json`; a different suffix is rejected. `Content-Type` must be
`application/json`.

## Querying

JSON documents are row-stored, not embedded. They appear in
`list_queryable_files`, are queryable with `query_rows`, and are not readable with
`read_document` or `search_context`.

```sql
select data->'customer'->>'name' as customer,
       (data->>'total')::numeric as total
from document_rows
where document_id = '<id from the catalogue>'
  and sheet = 'orders'
```

A single unnamed table uses `sheet = ''` (or omits the sheet filter). A result
must come from one document and one table, the same rule workbook sheets follow.
`data @> '{"customer":{"name":"A"}}'` uses the JSONB GIN index.

## Versioning

Version `1` is the current contract. An unknown `version` is rejected with a
clear error. A future version will get its own schema and parser; the manifest
records the protocol version that produced a run.
