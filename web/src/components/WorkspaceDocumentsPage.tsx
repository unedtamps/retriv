import { FileUp, LockKeyhole } from 'lucide-react'
import type { ApiResource } from '../hooks/useApiResource'
import type { WorkspaceDocumentsData } from '../hooks/useWorkspaceDocuments'
import { formatBytes } from '../lib/visuals'
import type { DocumentUpload, Workspace } from '../types'
import { DocumentTable } from './DocumentTable'
import { Avatar } from './ui/Avatar'
import { RefreshButton } from './ui/RefreshButton'

interface WorkspaceDocumentsPageProps {
  workspace: Workspace
  documents: ApiResource<WorkspaceDocumentsData>
  searchQuery: string
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
  searchQuery,
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
  const storage = items.reduce((sum, document) => sum + document.size_bytes, 0)
  const restricted = workspace.membership_role === null

  return (
    <div className="mx-auto max-w-[92rem] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3.5">
          <Avatar name={workspace.name} seed={workspace.id} size="lg" className="mt-0.5" />
          <div className="min-w-0">
            <p className="eyebrow">Workspace documents</p>
            <h1 className="mt-1.5 truncate font-condensed text-xl font-bold uppercase tracking-[0.05em] text-ink">
              {workspace.name}
            </h1>
            <p className="mt-1.5 text-sm leading-6 text-ink-muted">
              {restricted
                ? 'Membership is required to read the files in this workspace.'
                : `${total} document${total === 1 ? '' : 's'} · ${formatBytes(storage)} in this view · ${workspace.membership_role} access`}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
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

      <div className="mt-6">
        {restricted ? (
          <div className="rounded-card border border-dashed border-line-strong bg-surface px-6 py-14 text-center">
            <LockKeyhole aria-hidden="true" className="mx-auto text-ink-faint" size={26} />
            <h2 className="mt-3 text-sm font-semibold text-ink">Documents are members-only</h2>
            <p className="mx-auto mt-1.5 max-w-sm text-sm leading-6 text-ink-muted">
              Ask a workspace owner for viewer, editor or owner access. A platform admin role alone
              does not grant document access.
            </p>
          </div>
        ) : (
          <DocumentTable
            documents={items}
            total={total}
            loading={documents.loading}
            error={documents.error}
            canDelete={workspace.can_delete}
            canReindex={canReindex}
            reindexingId={reindexingId}
            onReindex={onReindex}
            canView={canView}
            onView={onView}
            searchQuery={searchQuery}
            focusDocumentId={focusDocumentId}
            onDelete={onDeleteDocument}
          />
        )}
      </div>
    </div>
  )
}
