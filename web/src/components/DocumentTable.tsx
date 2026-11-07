import { useEffect, useRef } from 'react'
import { Eye, FileText, RotateCcw, Trash2 } from 'lucide-react'
import type { ProcessingRuns } from '../hooks/useProcessingRuns'
import type { DocumentUpload, ProcessingRun } from '../types'
import {
  documentStatusLabel,
  documentStatusTone,
  documentType,
  formatBytes,
  formatShortDate,
  isActiveStatus,
  STATUS_TONE_CLASSES,
} from '../lib/visuals'

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
  runs?: ProcessingRuns
  focusDocumentId?: string | null
  onDelete: (document: DocumentUpload) => void
}

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
  runs,
  focusDocumentId,
  onDelete,
}: DocumentTableProps) {
  const tableRef = useRef<HTMLTableElement>(null)
  const activeRunByDocument = new Map<string, ProcessingRun>()
  if (runs !== undefined) {
    for (const run of runs.runs) {
      if (run.job_kind === 'delete') continue
      if (activeRunByDocument.has(run.document_id)) continue
      activeRunByDocument.set(run.document_id, run)
    }
  }

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
  }, [focusDocumentId, documents.length])

  return (
    <div className="overflow-hidden rounded-card border border-line bg-surface">
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
      ) : documents.length === 0 ? (
        <div className="flex flex-col items-center px-5 py-12 text-center">
          <FileText aria-hidden="true" className="text-ink-faint" size={24} />
          <p className="mt-3 text-sm font-medium text-ink">
            {total === 0 ? 'No documents in this workspace yet' : 'No documents match'}
          </p>
          <p className="mt-1 max-w-sm text-sm leading-6 text-ink-muted">
            {total === 0
              ? 'Upload a PDF, DOCX or CSV to make its content available to this workspace.'
              : 'Try a different filter to find the file you need.'}
          </p>
        </div>
      ) : (
        <div className="relative overflow-x-auto">
          <table ref={tableRef} className="w-full min-w-[44rem] border-collapse text-left">
            <caption className="sr-only">Documents</caption>
            <thead>
              <tr className="border-b border-line text-left">
                <th className="px-4 py-2 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted sm:px-5">
                  Ref
                </th>
                <th className="px-3 py-2 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted">
                  Name
                </th>
                <th className="hidden px-3 py-2 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted sm:table-cell">
                  Type
                </th>
                <th className="hidden px-3 py-2 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted md:table-cell">
                  Size
                </th>
                <th className="hidden px-3 py-2 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted md:table-cell">
                  Updated
                </th>
                <th className="px-3 py-2 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted">
                  State
                </th>
                <th className="w-12 px-3 py-2">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {documents.map((document) => {
                const type = documentType(document.original_filename, document.detected_media_type)
                const activeRun = activeRunByDocument.get(document.id)
                const progress =
                  activeRun !== undefined && activeRun.chunk_count > 0
                    ? `${activeRun.embedded_chunk_count}/${activeRun.chunk_count}`
                    : null
                return (
                  <tr
                    key={document.id}
                    data-document-id={document.id}
                    className="group border-b border-line last:border-b-0"
                  >
                    <td className="px-4 py-3 align-middle sm:px-5">
                      <span className="stamp whitespace-nowrap">{document.id.slice(0, 8)}</span>
                    </td>
                    <td className="px-3 py-3 align-middle">
                      <div className="min-w-0">
                        {canView && onView !== undefined ? (
                          <button
                            type="button"
                            className="block max-w-[16rem] truncate text-left text-sm font-semibold text-ink underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:no-underline disabled:text-ink-faint"
                            aria-label={`View ${document.original_filename}`}
                            disabled={document.status === 'deleting'}
                            onClick={() => onView(document)}
                          >
                            {document.original_filename}
                          </button>
                        ) : (
                          <p className="max-w-[16rem] truncate text-sm font-semibold text-ink">
                            {document.original_filename}
                          </p>
                        )}
                        {document.row_count > 0 ? (
                          <p className="mt-0.5 font-mono text-[0.62rem] text-ink-faint">
                            {document.row_count} rows
                          </p>
                        ) : null}
                      </div>
                    </td>
                    <td className="hidden px-3 py-3 align-middle sm:table-cell">
                      <span className="stamp">{type}</span>
                    </td>
                    <td className="hidden whitespace-nowrap px-3 py-3 font-mono text-meta text-ink-muted md:table-cell">
                      {formatBytes(document.size_bytes)}
                    </td>
                    <td className="hidden whitespace-nowrap px-3 py-3 font-mono text-meta text-ink-muted md:table-cell">
                      {formatShortDate(document.created_at)}
                    </td>
                    <td className="px-3 py-3 align-middle">
                      <span
                        className={`stamp whitespace-nowrap ${
                          document.status === 'failed' ? 'border-transparent bg-danger text-white' : STATUS_TONE_CLASSES[documentStatusTone(document.status)]
                        }`}
                      >
                        {documentStatusLabel(document.status)}
                      </span>
                      {progress !== null ? (
                        <p className="mt-1 font-mono text-[0.62rem] text-ink-muted">{progress}</p>
                      ) : null}
                    </td>
                    <td className="px-3 py-3 align-middle">
                      <div className="flex items-center justify-end gap-1">
                        {canView && onView !== undefined ? (
                          <button
                            type="button"
                            className="flex h-7 w-7 items-center justify-center rounded-stamp border border-transparent text-ink-faint transition-colors hover:border-line hover:text-ink disabled:opacity-40"
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
                            className="flex h-7 w-7 items-center justify-center rounded-stamp border border-transparent text-ink-faint transition-colors hover:border-line hover:text-ink disabled:opacity-40"
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
                            className="flex h-7 w-7 items-center justify-center rounded-stamp border border-transparent text-ink-faint transition-colors hover:border-line hover:text-danger disabled:opacity-40"
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
