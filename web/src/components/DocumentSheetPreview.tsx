import { useEffect, useRef, useState } from 'react'
import { FileSpreadsheet, LoaderCircle } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import { cachedDocument } from '../lib/documentCache'
import {
  PREVIEW_ROW_LIMIT,
  SheetTooLargeError,
  cachedSheetMarkup,
  renderSheetPreview,
  sanitizeSheetMarkup,
  type SheetPreview,
} from '../lib/sheetPreview'
import type { DocumentUpload, Workspace } from '../types'

interface DocumentSheetPreviewProps {
  document: DocumentUpload
  workspace: Workspace
  onUnauthorized: () => void
}

/**
 * A CSV or a workbook, shown as the table it holds.
 *
 * CSV and XLSX are fetched as bytes and parsed in the browser, which is what
 * keeps the preview faithful: the same header row, the same cell values, the
 * same merges a reader sees when the file is opened locally. The cost is that
 * the parse happens on the user's machine, so a workbook past
 * `PREVIEW_ROW_LIMIT` is refused before anything is parsed rather than after
 * the tab has already spent a couple of seconds and a few hundred megabytes on
 * it.
 */
export function DocumentSheetPreview({
  document,
  workspace,
  onUnauthorized,
}: DocumentSheetPreviewProps) {
  const [preview, setPreview] = useState<SheetPreview | null>(null)
  const [tooLarge, setTooLarge] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sheet, setSheet] = useState<string | null>(null)
  const hostRef = useRef<HTMLDivElement | null>(null)

  const format = document.detected_media_type === 'text/csv' ? 'csv' : 'xlsx'

  useEffect(() => {
    const cached = cachedSheetMarkup(`${document.id}:${format}`)
    if (cached !== null) {
      setPreview(cached)
      setSheet(cached.activeSheet)
      return
    }
    let mounted = true
    const file = cachedDocument(document)
    const load = async (): Promise<SheetPreview> => {
      const blob =
        file?.blob ??
        (await api.fetchDocumentBlob(workspace.id, document.id))
      return renderSheetPreview(`${document.id}:${format}`, blob, format)
    }
    load()
      .then((next) => {
        if (!mounted) return
        setPreview(next)
        setSheet(next.activeSheet)
      })
      .catch((caught: unknown) => {
        if (!mounted) return
        if (caught instanceof SheetTooLargeError) {
          setTooLarge(caught.rowCount)
          return
        }
        if (isUnauthorized(caught)) onUnauthorized()
        else setError(getErrorMessage(caught))
      })
    return () => {
      mounted = false
    }
  }, [document, workspace.id, format, onUnauthorized])

  const activeSheet = sheet ?? preview?.activeSheet ?? null
  const markup =
    preview === null || activeSheet === null
      ? null
      : activeSheet === preview.activeSheet
        ? preview.markup
        : null

  useEffect(() => {
    if (markup === null || hostRef.current === null) return
    hostRef.current.innerHTML = sanitizeSheetMarkup(markup)
  }, [markup])

  if (error !== null) {
    return (
      <p className="error-note" role="alert">
        {error}
      </p>
    )
  }

  if (tooLarge !== null) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 px-8 text-center">
        <FileSpreadsheet aria-hidden="true" className="text-ink-faint" size={28} strokeWidth={1.5} />
        <p className="text-sm font-medium text-ink">Too large for an inline preview</p>
        <p className="max-w-md text-sm leading-6 text-ink-muted">
          This spreadsheet holds {tooLarge.toLocaleString()} rows. Previews stop at{' '}
          {PREVIEW_ROW_LIMIT.toLocaleString()} rows because parsing and drawing more than that in
          the browser freezes the tab. Download the file to open it in a spreadsheet program, or use
          the search tools to query its rows.
        </p>
      </div>
    )
  }

  if (preview === null || markup === null) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm text-ink-muted" role="status">
        <LoaderCircle aria-hidden="true" className="animate-spin" size={17} />
        Parsing spreadsheet…
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {preview.sheetNames.length > 1 ? (
        <div className="flex shrink-0 flex-wrap gap-1 border-b border-line bg-surface px-4 py-2">
          {preview.sheetNames.map((name) => (
            <button
              key={name}
              type="button"
              className={
                name === activeSheet
                  ? 'rounded-control bg-ink px-2.5 py-1 text-xs font-medium text-paper'
                  : 'rounded-control px-2.5 py-1 text-xs font-medium text-ink-muted hover:bg-paper'
              }
              aria-pressed={name === activeSheet}
              onClick={() => setSheet(name)}
            >
              {name}
            </button>
          ))}
        </div>
      ) : null}
      {preview.truncated ? (
        <p className="shrink-0 border-b border-line bg-paper px-4 py-1.5 text-xs text-ink-muted">
          Showing the first {PREVIEW_ROW_LIMIT.toLocaleString()} of{' '}
          {preview.rowCount.toLocaleString()} rows. Download the file for all of it.
        </p>
      ) : null}
      <div className="min-h-0 flex-1 overflow-auto bg-surface" data-testid="sheet-preview">
        <div ref={hostRef} className="retriv-sheet" />
      </div>
      <style>{SHEET_STYLES}</style>
    </div>
  )
}

const SHEET_STYLES = `
.retriv-sheet {
  display: inline-block;
  min-width: 100%;
  padding: 12px 16px 24px;
  font-variant-numeric: tabular-nums;
}
.retriv-sheet table {
  border-collapse: collapse;
  font-size: 0.8125rem;
  line-height: 1.45;
  background: var(--color-surface, #fff);
}
.retriv-sheet td,
.retriv-sheet th {
  border: 1px solid var(--color-line, #d8dee7);
  padding: 4px 10px;
  vertical-align: top;
  max-width: 42ch;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.retriv-sheet tr:first-child td {
  position: sticky;
  top: 0;
  z-index: 1;
  font-weight: 600;
  background: var(--color-paper, #f4f7fa);
  color: var(--color-ink, #16202c);
}
.retriv-sheet tr:hover td {
  background: var(--color-paper, #f8fafc);
}
`
