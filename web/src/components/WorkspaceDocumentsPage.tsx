import { useDeferredValue, useEffect, useState } from 'react'
import { FileUp, LockKeyhole, Search } from 'lucide-react'
import type { ApiResource } from '../hooks/useApiResource'
import type { ProcessingRuns } from '../hooks/useProcessingRuns'
import type { WorkspaceDocumentsData } from '../hooks/useWorkspaceDocuments'
import { documentType, isFailedStatus, isReadyStatus, isActiveStatus } from '../lib/visuals'
import type { DocumentUpload, Workspace } from '../types'
import { DocumentTable } from './DocumentTable'
import { RefreshButton } from './ui/RefreshButton'

type StatusFilter = 'all' | 'ready' | 'active' | 'failed'

const TYPE_CHIPS = ['PDF', 'DOCX', 'XLSX', 'CSV', 'JSON', 'PPTX'] as const

interface WorkspaceDocumentsPageProps {
  workspace: Workspace
  documents: ApiResource<WorkspaceDocumentsData>
  runs?: ProcessingRuns
  focusDocumentId?: string | null
  canReindex?: boolean
  reindexingId?: string | null
  onReindex?: (document: DocumentUpload) => void
  canView?: boolean
  onView?: (document: DocumentUpload) => void
  onUpload: () => void
  onDeleteDocument: (document: DocumentUpload) => void
}

export function WorkspaceDocumentsPage({
  workspace,
  documents,
  runs,
  focusDocumentId,
  canReindex,
  reindexingId,
  onReindex,
  canView,
  onView,
  onUpload,
  onDeleteDocument,
}: WorkspaceDocumentsPageProps) {
  const items = documents.data?.items ?? []
  const total = documents.data?.total ?? 0
  const restricted = workspace.membership_role === null

  const [nameFilter, setNameFilter] = useState('')
  const [typeFilter, setTypeFilter] = useState<(typeof TYPE_CHIPS)[number] | 'all'>('all')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const deferredName = useDeferredValue(nameFilter.trim().toLowerCase())

  useEffect(() => {
    setTypeFilter('all')
    setStatusFilter('all')
    setNameFilter('')
  }, [workspace.id])

  const filtered = items.filter((document) => {
    if (typeFilter !== 'all') {
      const type = documentType(document.original_filename, document.detected_media_type)
      if (type !== typeFilter) return false
    }
    if (statusFilter === 'ready' && !isReadyStatus(document.status)) return false
    if (statusFilter === 'active' && !isActiveStatus(document.status)) return false
    if (statusFilter === 'failed' && !isFailedStatus(document.status)) return false
    if (deferredName !== '' && !document.original_filename.toLowerCase().includes(deferredName)) {
      return false
    }
    return true
  })

  return (
    <div className="mx-auto max-w-[92rem] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-2.5">
          <h1 className="font-condensed text-xl font-bold uppercase tracking-[0.05em] text-ink">
            Documents
          </h1>
          <span className="stamp">
            {restricted ? total : `${filtered.length} of ${total}`}
          </span>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {restricted ? null : (
            <RefreshButton
              label="Refresh document status"
              loading={documents.loading}
              onRefresh={documents.reload}
            />
          )}
          {workspace.can_upload ? (
            <button type="button" className="btn btn-primary shrink-0" onClick={onUpload}>
              <FileUp aria-hidden="true" size={16} />
              Upload document
            </button>
          ) : null}
        </div>
      </header>

      {restricted ? (
        <section className="mt-6 rounded-card border border-dashed border-line-strong bg-surface px-6 py-14 text-center">
          <LockKeyhole aria-hidden="true" className="mx-auto text-ink-faint" size={26} />
          <h2 className="mt-3 text-sm font-semibold text-ink">Documents are members-only</h2>
          <p className="mx-auto mt-1.5 max-w-sm text-sm leading-6 text-ink-muted">
            Ask a workspace owner for viewer, editor or owner access. A platform admin role alone
            does not grant document access.
          </p>
        </section>
      ) : (
        <>
          <div className="relative mt-5">
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-faint"
              size={15}
            />
            <input
              type="search"
              className="field pl-9"
              placeholder="Filter by name"
              aria-label="Filter documents by name"
              value={nameFilter}
              onChange={(event) => setNameFilter(event.target.value)}
            />
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by type">
              <button
                type="button"
                className={typeFilter === 'all' ? 'nav-link-active px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em]' : 'rounded-control border border-line bg-surface px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted transition-colors hover:border-line-strong hover:text-ink'}
                aria-pressed={typeFilter === 'all'}
                onClick={() => setTypeFilter('all')}
              >
                All
              </button>
              {TYPE_CHIPS.map((type) => (
                <button
                  key={type}
                  type="button"
                  className={typeFilter === type ? 'nav-link-active px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em]' : 'rounded-control border border-line bg-surface px-3 py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted transition-colors hover:border-line-strong hover:text-ink'}
                  aria-pressed={typeFilter === type}
                  onClick={() => setTypeFilter(type)}
                >
                  {type}
                </button>
              ))}
            </div>
            <select
              className="field ml-auto w-auto py-1.5 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted"
              aria-label="Filter by status"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
            >
              <option value="all">Status: all</option>
              <option value="ready">Status: ready</option>
              <option value="active">Status: processing</option>
              <option value="failed">Status: failed</option>
            </select>
          </div>

          <div className="mt-3">
            <DocumentTable
              documents={filtered}
              total={total}
              loading={documents.loading}
              error={documents.error}
              canDelete={workspace.can_delete}
              canReindex={canReindex}
              reindexingId={reindexingId}
              onReindex={onReindex}
              canView={canView}
              onView={onView}
              runs={runs}
              focusDocumentId={focusDocumentId}
              onDelete={onDeleteDocument}
            />
          </div>
        </>
      )}
    </div>
  )
}
