import { useEffect, useMemo, useState } from 'react'
import { Braces, LoaderCircle } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import { loadDocument } from '../lib/documentCache'
import type { DocumentUpload, Workspace } from '../types'

interface DocumentJsonPreviewProps {
  document: DocumentUpload
  workspace: Workspace
  onUnauthorized: () => void
}

interface JsonColumn {
  name: string
  type?: string
  nullable?: boolean
}

interface JsonTable {
  name?: string
  columns: JsonColumn[]
  records: Record<string, unknown>[]
}

interface JsonEnvelope {
  protocol: string
  version: number
  tables: JsonTable[]
}

const PREVIEW_JSON_ROWS = 500
const PREVIEW_JSON_COLUMNS = 50
const PREVIEW_JSON_CHARS = 200_000

function cellText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function isEnvelope(value: unknown): value is JsonEnvelope {
  if (value === null || typeof value !== 'object') return false
  const candidate = value as Partial<JsonEnvelope>
  return (
    candidate.protocol === 'retriv-json' &&
    candidate.version === 1 &&
    Array.isArray(candidate.tables) &&
    candidate.tables.length > 0 &&
    candidate.tables.every(
      (table) =>
        table !== null &&
        typeof table === 'object' &&
        Array.isArray((table as JsonTable).columns) &&
        Array.isArray((table as JsonTable).records),
    )
  )
}

/**
 * A retriv-json payload, shown as its declared tables plus the raw envelope.
 *
 * The protocol makes this view deterministic: the columns come from the file,
 * not from a guess about the first record. Rendering is capped because a JSON
 * file can still be a large export even when it follows the protocol.
 */
export function DocumentJsonPreview({
  document,
  workspace,
  onUnauthorized,
}: DocumentJsonPreviewProps) {
  const [envelope, setEnvelope] = useState<JsonEnvelope | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [activeTable, setActiveTable] = useState(0)
  const [view, setView] = useState<'table' | 'raw'>('table')

  useEffect(() => {
    let mounted = true
    loadDocument(document, () => api.fetchDocumentBlob(workspace.id, document.id))
      .then(async (file) => {
        const parsed: unknown = JSON.parse(await file.blob.text())
        if (!mounted) return
        if (!isEnvelope(parsed)) {
          setError('This file is not a retriv-json v1 payload.')
          return
        }
        setEnvelope(parsed)
      })
      .catch((caught: unknown) => {
        if (!mounted) return
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
      })
    return () => {
      mounted = false
    }
  }, [document, workspace.id, onUnauthorized])

  const table = envelope?.tables[activeTable] ?? null
  const rows = table?.records ?? []
  const columns = table?.columns ?? []
  const visibleColumns = columns.slice(0, PREVIEW_JSON_COLUMNS)
  const visibleRows = rows.slice(0, PREVIEW_JSON_ROWS)
  const raw = useMemo(
    () => (envelope === null ? '' : JSON.stringify(envelope, null, 2)),
    [envelope],
  )

  if (error !== null) {
    return (
      <p className="error-note" role="alert">
        {error}
      </p>
    )
  }

  if (envelope === null || table === null) {
    return (
      <div
        className="flex h-full items-center justify-center gap-2 text-sm text-ink-muted"
        role="status"
      >
        <LoaderCircle aria-hidden="true" className="animate-spin" size={17} />
        Reading JSON data…
      </div>
    )
  }

  const rowLabel = table.name === '' || table.name === undefined ? '(root)' : table.name

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line bg-surface px-4 py-2">
        <Braces aria-hidden="true" className="text-ink-faint" size={16} />
        <span className="text-xs font-medium text-ink">{rowLabel}</span>
        <span className="text-xs text-ink-muted">
          {rows.length.toLocaleString()} record{rows.length === 1 ? '' : 's'}
        </span>
        {envelope.tables.length > 1 ? (
          <div className="ml-auto flex flex-wrap gap-1">
            {envelope.tables.map((candidate, index) => (
              <button
                key={`${candidate.name ?? ''}-${index}`}
                type="button"
                className={
                  index === activeTable
                    ? 'rounded-control bg-ink px-2.5 py-1 text-xs font-medium text-paper'
                    : 'rounded-control px-2.5 py-1 text-xs font-medium text-ink-muted hover:bg-paper'
                }
                aria-pressed={index === activeTable}
                onClick={() => setActiveTable(index)}
              >
                {candidate.name || '(root)'}
              </button>
            ))}
          </div>
        ) : null}
        <div className="ml-auto flex gap-1">
          <button
            type="button"
            className={
              view === 'table'
                ? 'rounded-control bg-ink px-2.5 py-1 text-xs font-medium text-paper'
                : 'rounded-control px-2.5 py-1 text-xs font-medium text-ink-muted hover:bg-paper'
            }
            aria-pressed={view === 'table'}
            onClick={() => setView('table')}
          >
            Table
          </button>
          <button
            type="button"
            className={
              view === 'raw'
                ? 'rounded-control bg-ink px-2.5 py-1 text-xs font-medium text-paper'
                : 'rounded-control px-2.5 py-1 text-xs font-medium text-ink-muted hover:bg-paper'
            }
            aria-pressed={view === 'raw'}
            onClick={() => setView('raw')}
          >
            Raw
          </button>
        </div>
      </div>
      {view === 'raw' ? (
        <div className="min-h-0 flex-1 overflow-auto bg-surface">
          {raw.length > PREVIEW_JSON_CHARS ? (
            <p className="border-b border-line bg-paper px-4 py-1.5 text-xs text-ink-muted">
              Showing the first {PREVIEW_JSON_CHARS.toLocaleString()} characters. Download the file
              for the full payload.
            </p>
          ) : null}
          <pre className="whitespace-pre-wrap break-words p-4 text-xs leading-5 text-ink">
            {raw.slice(0, PREVIEW_JSON_CHARS)}
          </pre>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto bg-surface">
          {rows.length > PREVIEW_JSON_ROWS || columns.length > PREVIEW_JSON_COLUMNS ? (
            <p className="border-b border-line bg-paper px-4 py-1.5 text-xs text-ink-muted">
              Showing {visibleRows.length.toLocaleString()} of {rows.length.toLocaleString()}{' '}
              records and {visibleColumns.length} of {columns.length} columns. Download the file for
              all of it.
            </p>
          ) : null}
          <table className="min-w-full border-collapse text-left text-xs">
            <thead>
              <tr>
                {visibleColumns.map((column) => (
                  <th
                    key={column.name}
                    className="sticky top-0 z-10 border border-line bg-paper px-2.5 py-1.5 font-semibold text-ink"
                  >
                    {column.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((record, rowIndex) => (
                <tr key={rowIndex} className="hover:bg-paper">
                  {visibleColumns.map((column) => (
                    <td
                      key={column.name}
                      className="max-w-[42ch] border border-line px-2.5 py-1.5 align-top text-ink-muted"
                    >
                      {cellText(record[column.name])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
