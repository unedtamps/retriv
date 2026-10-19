import { useState } from 'react'
import { FileText, Trash2 } from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { DocumentUpload, Workspace } from '../types'
import { ConfirmDialog } from './Dialog'
import { EmptyState } from './ui/Feedback'
import { useApiResource } from '../hooks/useApiResource'

interface WorkspaceDocumentsProps {
  workspace: Workspace
  token: string
  canDelete: boolean
  onChanged: () => Promise<void>
  onUnauthorized: () => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
}

const DOCUMENT_STATUS_LABELS: Record<string, string> = {
  ready: 'Ready',
  processing: 'Processing',
  deleting: 'Deleting',
  failed: 'Failed',
  cancelled: 'Cancelled',
  unsupported_format: 'Unsupported format',
}

/**
 * The documents in a workspace, and the right to delete one.
 *
 * The list and the delete are one concern, so they are one component: the
 * drawer used to hold both alongside membership management and the access
 * summary, and was the largest file in the app.
 */
export function WorkspaceDocuments({
  workspace,
  token,
  canDelete,
  onChanged,
  onUnauthorized,
  onNotice,
}: WorkspaceDocumentsProps) {
  const [deleteTarget, setDeleteTarget] = useState<DocumentUpload | null>(null)
  const [deleting, setDeleting] = useState(false)

  const documents = useApiResource(
    (signal) =>
      api.listDocuments(token, workspace.id, { limit: 100, signal }),
    [token, workspace.id],
    { onUnauthorized },
  )

  async function deleteDocument() {
    if (deleteTarget === null) return
    setDeleting(true)
    try {
      await api.deleteDocument(token, workspace.id, deleteTarget.id)
      // Dropped locally rather than left for a refresh: the API answers 204
      // as soon as the delete is accepted, and the list no longer reports a
      // document that is being deleted, so it will not come back.
      documents.setData((current) =>
        current === null
          ? current
          : {
              ...current,
              items: current.items.filter((item) => item.id !== deleteTarget.id),
              total: Math.max(0, current.total - 1),
            },
      )
      onNotice(`${deleteTarget.original_filename} deleted from ${workspace.name}.`)
      setDeleteTarget(null)
      await onChanged()
    } catch (caught) {
      if (isUnauthorized(caught)) onUnauthorized()
      else onNotice(getErrorMessage(caught), 'error')
    } finally {
      setDeleting(false)
    }
  }

  const items = documents.data?.items ?? []
  const total = documents.data?.total ?? 0

  return (
    <section className="border-b border-line px-5 py-5 sm:px-6">
      <div className="mb-3 flex items-center justify-between gap-4">
        <div>
          <p className="eyebrow">Workspace content</p>
          <h3 className="mt-1 text-base font-semibold text-ink">Documents</h3>
        </div>
        <span className="font-mono text-meta text-ink-faint">{total} total</span>
      </div>

      {documents.error === null ? null : (
        <p className="error-note mb-3" role="alert">
          {documents.error}
        </p>
      )}

      {documents.loading && items.length === 0 ? (
        <div className="space-y-2" aria-label="Loading documents">
          {Array.from({ length: 2 }, (_, index) => (
            <div key={index} className="h-14 animate-pulse rounded-card bg-sunken" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState title="No documents in this workspace yet" />
      ) : (
        <ul className="divide-y divide-line rounded-card border border-line">
          {items.map((document) => (
            <li key={document.id} className="flex items-center justify-between gap-3 px-4 py-3">
              <div className="flex min-w-0 items-center gap-3">
                <FileText
                  aria-hidden="true"
                  className="shrink-0 text-ink-faint"
                  size={17}
                />
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-ink">
                    {document.original_filename}
                  </p>
                  <p className="mt-0.5 font-mono text-meta text-ink-faint">
                    {documentLabel(document.status)}
                    {/* A CSV is rows, not chunks. Without this it reads as
                        a bare "Ready" with nothing to show for it. */}
                    {document.row_count > 0 ? ` · ${document.row_count} rows` : ''} ·{' '}
                    {new Date(document.created_at).toLocaleString()}
                  </p>
                </div>
              </div>
              {canDelete ? (
                <button
                  type="button"
                  className="btn-icon shrink-0 text-danger hover:text-danger"
                  aria-label={`Delete ${document.original_filename}`}
                  disabled={deleting}
                  onClick={() => setDeleteTarget(document)}
                >
                  <Trash2 aria-hidden="true" size={16} />
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this document?"
        description={`${deleteTarget?.original_filename ?? 'This document'} and its indexed context will be permanently removed from ${workspace.name}. This cannot be undone.`}
        confirmLabel="Delete document"
        busy={deleting}
        destructive
        onClose={() => setDeleteTarget(null)}
        onConfirm={deleteDocument}
      />
    </section>
  )
}

function documentLabel(status: string): string {
  return DOCUMENT_STATUS_LABELS[status] ?? status.replaceAll('_', ' ')
}
