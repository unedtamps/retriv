import { useDeferredValue, useEffect, useRef, useState } from 'react'
import { Eye, FileText, RotateCcw, Search, Trash2 } from 'lucide-react'
import type { DocumentUpload } from '../types'
import {
  documentStatusLabel,
  documentStatusTone,
  documentType,
  formatBytes,
  formatShortDate,
  isActiveStatus,
  isFailedStatus,
  isReadyStatus,
  STATUS_TONE_CLASSES,
} from '../lib/visuals'

export type DocumentFilter = 'all' | 'ready' | 'active' | 'failed'

interface DocumentTableProps {
  documents: DocumentUpload[]
  total: number
  loading: boolean
  error: string | null
  canDelete: boolean
  canReindex?: boolean
  reindexingId?: string | null
  onReindex?: (document: DocumentUpload) => void
  canView?: boolean
  onView?: (document: DocumentUpload) => void
  searchQuery: string
  focusDocumentId?: string | null
  onDelete: (document: DocumentUpload) => void
  limit?: number
}

const FILTERS: Array<{ id: DocumentFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'ready', label: 'Ready' },
  { id: 'active', label: 'Processing' },
  { id: 'failed', label: 'Failed' },
]

export function DocumentTable({
  documents,
  total,
  loading,
  error,
  canDelete,
  canReindex = false,
  reindexingId = null,
  onReindex,
  canView = false,
  onView,
  searchQuery,
  focusDocumentId,
  onDelete,
  limit,
}: DocumentTableProps) {
  const [filter, setFilter] = useState<DocumentFilter>('all')
  const tableRef = useRef<HTMLTableElement>(null)
  // The typed query reaches this table on every keystroke. Filtering on it
  // directly makes the rows vanish and reappear faster than the eye can
  // follow, so the table settles on a short delay of its own.
  const [settledQuery, setSettledQuery] = useState(() => searchQuery.trim().toLowerCase())
  useEffect(() => {
    const timer = window.setTimeout(
      () => setSettledQuery(searchQuery.trim().toLowerCase()),
      420,
    )
    return () => window.clearTimeout(timer)
  }, [searchQuery])
  const deferredQuery = useDeferredValue(settledQuery)
  const filtered = documents.filter((document) => {
    const matchesQuery =
      deferredQuery === '' ||
      `${document.original_filename} ${documentType(
        document.original_filename,
        document.detected_media_type,
      )}`
        .toLowerCase()
        .includes(deferredQuery)
    if (!matchesQuery) return false
    if (filter === 'ready') return isReadyStatus(document.status)
    if (filter === 'active') return isActiveStatus(document.status)
    if (filter === 'failed') return isFailedStatus(document.status)
    return true
  })
  const visible = limit === undefined ? filtered : filtered.slice(0, limit)

  useEffect(() => {
    if (focusDocumentId === null || focusDocumentId === undefined) return
    const row = tableRef.current?.querySelector<HTMLElement>(
      `[data-document-id="${focusDocumentId}"]`,
    )
    if (row === null || row === undefined) return
    row.scrollIntoView({ block: 'center', behavior: 'smooth' })
    row.classList.add('document-focus')
    const timer = window.setTimeout(() => row.classList.remove('document-focus'), 2000)
    return () => {
      window.clearTimeout(timer)
      row.classList.remove('document-focus')
    }
  }, [focusDocumentId, visible.length])

  return (
    <div className="overflow-hidden rounded-card border border-line bg-surface">
        <div className="flex flex-wrap items-end justify-between gap-3 border-b border-line px-4 py-2 sm:px-5">
          <div className="flex flex-wrap items-center gap-5" role="group" aria-label="Filter documents">
            {FILTERS.map((option) => {
              const count =
                option.id === 'all'
                  ? documents.length
                  : option.id === 'ready'
                    ? documents.filter((document) => isReadyStatus(document.status)).length
                    : option.id === 'active'
                      ? documents.filter((document) => isActiveStatus(document.status)).length
                      : documents.filter((document) => isFailedStatus(document.status)).length
              return (
                <button
                  key={option.id}
                  type="button"
                  className={`relative px-0.5 pb-2 pt-1.5 text-[0.8125rem] transition-colors ${
                    filter === option.id
                      ? 'font-medium text-ink'
                      : 'text-ink-muted hover:text-ink'
                  }`}
                  aria-pressed={filter === option.id}
                  onClick={() => setFilter(option.id)}
                >
                  {option.label}
                  <span className="ml-1.5 font-mono text-[0.6rem] text-ink-faint">{count}</span>
                  {filter === option.id ? (
                    <span
                      aria-hidden="true"
                      className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-ink"
                    />
                  ) : null}
                </button>
              )
            })}
          </div>
          <p className="hidden pb-2 font-mono text-[0.65rem] text-ink-faint sm:block">
            {filtered.length === total ? total : `${filtered.length} of ${total}`} documents
          </p>
        </div>

      {error === null ? null : (
        <p className="border-b border-line px-5 py-3 text-sm text-danger" role="alert">
          {error}
        </p>
      )}

      {loading && documents.length === 0 ? (
        <div className="space-y-2 p-4" aria-label="Loading documents">
          {Array.from({ length: 4 }, (_, index) => (
            <div key={index} className="h-14 animate-pulse rounded-card bg-sunken" />
          ))}
        </div>
      ) : visible.length === 0 ? (
        <div className="flex flex-col items-center px-5 py-12 text-center">
          <Search aria-hidden="true" className="text-ink-faint" size={24} />
          <p className="mt-3 text-sm font-medium text-ink">
            {documents.length === 0 ? 'No documents in this workspace yet' : 'No documents match'}
          </p>
          <p className="mt-1 max-w-sm text-sm leading-6 text-ink-muted">
            {documents.length === 0
              ? 'Upload a PDF, DOCX or CSV to make its content available to this workspace.'
              : 'Try a different search or filter to find the file you need.'}
          </p>
        </div>
      ) : (
        <div className="relative overflow-x-auto">
          <table ref={tableRef} className="w-full min-w-[38rem] border-collapse text-left">
            <caption className="sr-only">Documents</caption>
            <thead>
              <tr className="border-b border-line text-left">
                <th className="px-4 py-2.5 text-xs font-medium text-ink-muted sm:px-5">
                  Document
                </th>
                <th className="px-3 py-2.5 text-xs font-medium text-ink-muted">Status</th>
                <th className="hidden px-3 py-2.5 text-xs font-medium text-ink-muted sm:table-cell">
                  Added
                </th>
                <th className="hidden px-3 py-2.5 text-xs font-medium text-ink-muted md:table-cell">
                  Size
                </th>
                <th className="w-12 px-3 py-2.5">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((document) => {
                const type = documentType(document.original_filename, document.detected_media_type)
                const tone = documentStatusTone(document.status)
                return (
                  <tr
                    key={document.id}
                    data-document-id={document.id}
                    className="group border-b border-line last:border-b-0"
                  >
                    <td className="px-4 py-3 sm:px-5">
                      <div className="flex min-w-0 items-center gap-3">
                        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-control border border-line bg-sunken text-ink-muted">
                          <FileText aria-hidden="true" size={16} />
                        </span>
                        <div className="min-w-0">
                          <div className="flex min-w-0 items-center gap-2">
                            {canView && onView !== undefined ? (
                              <button
                                type="button"
                                className="truncate text-left text-sm font-medium text-ink underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:no-underline disabled:text-ink-faint"
                                aria-label={`View ${document.original_filename}`}
                                disabled={document.status === 'deleting'}
                                onClick={() => onView(document)}
                              >
                                {document.original_filename}
                              </button>
                            ) : (
                              <p className="truncate text-sm font-medium text-ink">
                                {document.original_filename}
                              </p>
                            )}
                            <span className="stamp shrink-0">{type}</span>
                          </div>
                          {document.row_count > 0 ? (
                            <p className="mt-0.5 font-mono text-[0.62rem] text-ink-faint">
                              {document.row_count} rows
                            </p>
                          ) : null}
                        </div>
                      </div>
                    </td>
                    <td className="px-3 py-3">
                      <span className={`stamp ${STATUS_TONE_CLASSES[tone]}`}>
                        {documentStatusLabel(document.status)}
                      </span>
                    </td>
                    <td className="hidden px-3 py-3 font-mono text-meta text-ink-muted sm:table-cell">
                      {formatShortDate(document.created_at)}
                    </td>
                    <td className="hidden px-3 py-3 font-mono text-meta text-ink-muted md:table-cell">
                      {formatBytes(document.size_bytes)}
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex items-center justify-end gap-1">
                        {canView && onView !== undefined ? (
                          <button
                            type="button"
                            className="btn-icon h-7 w-7 border-transparent bg-transparent text-ink-faint opacity-100 hover:border-line hover:text-ink disabled:opacity-40"
                            aria-label={`Preview ${document.original_filename}`}
                            disabled={document.status === 'deleting'}
                            onClick={() => onView(document)}
                          >
                            <Eye aria-hidden="true" size={14} />
                          </button>
                        ) : null}
                        {canReindex && onReindex !== undefined ? (
                          <button
                            type="button"
                            className="btn-icon h-7 w-7 border-transparent bg-transparent text-ink-faint opacity-100 hover:border-line hover:text-ink disabled:opacity-40"
                            aria-label={`Reindex ${document.original_filename}`}
                            disabled={reindexingId === document.id || isActiveStatus(document.status)}
                            title={
                              isActiveStatus(document.status)
                                ? 'A run is already in progress'
                                : 'Re-run extraction with the current pipeline'
                            }
                            onClick={() => onReindex(document)}
                          >
                            <RotateCcw
                              aria-hidden="true"
                              size={14}
                              className={reindexingId === document.id ? 'animate-spin' : undefined}
                            />
                          </button>
                        ) : null}
                        {canDelete ? (
                          <button
                            type="button"
                            className="btn-icon h-7 w-7 border-transparent bg-transparent text-ink-faint opacity-100 hover:border-line hover:text-danger disabled:opacity-40"
                            aria-label={`Delete ${document.original_filename}`}
                            onClick={() => onDelete(document)}
                          >
                            <Trash2 aria-hidden="true" size={14} />
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
