import { ArrowRight, Plus, FileUp, LockKeyhole, Sparkles, UploadCloud } from 'lucide-react'
import type { ApiResource } from '../hooks/useApiResource'
import type { WorkspaceDocumentsData } from '../hooks/useWorkspaceDocuments'
import { formatBytes, isActiveStatus } from '../lib/visuals'
import type { DocumentUpload, Workspace } from '../types'
import { DocumentTable } from './DocumentTable'
import { Avatar } from './ui/Avatar'
import { RefreshButton } from './ui/RefreshButton'
import { WorkspaceAccessSummary } from './WorkspaceAccessSummary'

interface WorkspaceOverviewProps {
  workspace: Workspace
  token: string
  isAdmin: boolean
  documents: ApiResource<WorkspaceDocumentsData>
  searchQuery: string
  onUpload: () => void
  onViewDocuments: () => void
  onManageMembers: () => void
  onUnauthorized: () => void
  onDeleteDocument: (document: DocumentUpload) => void
}

export function WorkspaceOverview({
  workspace,
  token,
  isAdmin,
  documents,
  searchQuery,
  onUpload,
  onViewDocuments,
  onManageMembers,
  onUnauthorized,
  onDeleteDocument,
}: WorkspaceOverviewProps) {
  const items = documents.data?.items ?? []
  const total = documents.data?.total ?? 0
  const ready = items.filter((document) => document.status === 'ready').length
  const active = items.filter((document) => isActiveStatus(document.status)).length
  const storage = items.reduce((sum, document) => sum + document.size_bytes, 0)
  const restricted = workspace.membership_role === null
  const canManage = isAdmin || workspace.can_manage_members

  return (
    <div className="mx-auto max-w-[92rem] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <header className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 items-start gap-3.5">
          <Avatar name={workspace.name} seed={workspace.id} size="lg" className="mt-0.5" />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <p className="eyebrow">Workspace overview</p>
              <span className="stamp">{workspace.membership_role ?? 'catalog only'}</span>
            </div>
            <h1 className="mt-1.5 truncate font-display text-2xl font-medium text-ink">
              {workspace.name}
            </h1>
            <p className="mt-1.5 max-w-2xl text-sm leading-6 text-ink-muted">
              {restricted
                ? 'This workspace is visible in the catalogue, but its documents and context are available only to members.'
                : workspace.description ?? 'Documents, context and access for this workspace.'}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {workspace.membership_role === null ? null : (
            <button type="button" className="btn btn-secondary" onClick={onManageMembers}>
              {canManage ? 'Manage access' : 'View access'}
            </button>
          )}
          {workspace.can_upload ? (
            <button type="button" className="btn btn-primary" onClick={onUpload}>
              <FileUp aria-hidden="true" size={16} />
              Upload document
            </button>
          ) : null}
        </div>
      </header>

      <section className="mt-6" aria-label="Workspace summary">
        <div className="grid grid-cols-2 gap-px overflow-hidden rounded-card border border-line bg-line xl:grid-cols-4">
          <StatCell
            label="Documents"
            value={restricted ? '—' : String(total)}
            detail={restricted ? 'Members only' : 'in this workspace'}
          />
          <StatCell
            label="Ready"
            value={restricted ? '—' : String(ready)}
            detail="searchable context"
          />
          <StatCell
            label="In pipeline"
            value={restricted ? '—' : String(active)}
            detail="queued or processing"
          />
          <StatCell
            label="Storage"
            value={restricted ? '—' : formatBytes(storage)}
            detail="total file size"
          />
        </div>
      </section>

      <div className="mt-5 grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem] xl:items-start">
        <section aria-labelledby="recent-documents-title">
          <div className="mb-2.5 flex items-end justify-between gap-3">
            <div>
              <p className="eyebrow">Workspace content</p>
              <h2 id="recent-documents-title" className="mt-1 text-base font-semibold text-ink">
                Recent documents
              </h2>
            </div>
            <div className="flex items-center gap-1">
              {restricted ? null : (
                <RefreshButton
                  label="Refresh recent documents"
                  loading={documents.loading}
                  onRefresh={documents.reload}
                />
              )}
              <button
                type="button"
                className="inline-flex items-center gap-1 text-xs font-medium text-ink-muted transition-colors hover:text-ink"
                onClick={onViewDocuments}
              >
                View all
                <ArrowRight aria-hidden="true" size={13} />
              </button>
            </div>
          </div>
          {restricted ? (
            <div className="rounded-card border border-dashed border-line-strong bg-surface px-6 py-12 text-center">
              <LockKeyhole aria-hidden="true" className="mx-auto text-ink-faint" size={26} />
              <h2 className="mt-3 text-sm font-semibold text-ink">Documents are members-only</h2>
              <p className="mx-auto mt-1.5 max-w-sm text-sm leading-6 text-ink-muted">
                Ask a workspace owner for viewer, editor or owner access. A platform admin role
                alone does not grant document access.
              </p>
            </div>
          ) : (
            <DocumentTable
              documents={items}
              total={total}
              loading={documents.loading}
              error={documents.error}
              canDelete={workspace.can_delete}
              searchQuery={searchQuery}
              onDelete={onDeleteDocument}
              limit={7}
            />
          )}
        </section>

        <aside className="grid gap-3.5" aria-label="Workspace activity">
          <QuickUpload workspace={workspace} onUpload={onUpload} />
          <WorkspaceAccessSummary
            workspace={workspace}
            token={token}
            isAdmin={isAdmin}
            onManageMembers={onManageMembers}
            onUnauthorized={onUnauthorized}
          />
        </aside>
      </div>
    </div>
  )
}

function StatCell({
  label,
  value,
  detail,
}: {
  label: string
  value: string
  detail: string
}) {
  return (
    <div className="bg-surface px-5 py-4">
      <p className="text-xs text-ink-faint">{label}</p>
      <p className="mt-1.5 text-[1.75rem] font-medium leading-7 text-ink">{value}</p>
      <p className="mt-0.5 text-xs text-ink-muted">{detail}</p>
    </div>
  )
}

function QuickUpload({ workspace, onUpload }: { workspace: Workspace; onUpload: () => void }) {
  if (!workspace.can_upload) {
    return (
      <section className="rounded-card border border-line bg-surface p-5">
        <p className="eyebrow">Quick upload</p>
        <div className="mt-3 flex items-start gap-2.5 text-sm leading-6 text-ink-muted">
          <LockKeyhole aria-hidden="true" className="mt-1 shrink-0 text-ink-faint" size={16} />
          Your role can read this workspace but not add documents. Ask an owner to change your
          access.
        </div>
      </section>
    )
  }

  return (
    <button
      type="button"
      className="group rounded-card border border-dashed border-line-strong bg-surface p-5 text-left transition-colors hover:border-accent-ink hover:bg-accent-soft/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink"
      onClick={onUpload}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="eyebrow">Quick upload</p>
          <p className="mt-2 text-sm font-semibold text-ink">Add context to this workspace</p>
        </div>
        <span className="flex h-9 w-9 items-center justify-center rounded-control border border-line bg-sunken text-ink-muted transition-colors group-hover:border-accent-ink group-hover:text-accent-ink">
          <UploadCloud aria-hidden="true" size={17} />
        </span>
      </div>
      <p className="mt-1.5 text-xs leading-5 text-ink-muted">
        Drop a PDF, DOCX or CSV, or browse. Indexing continues in the background.
      </p>
    </button>
  )
}

export function EmptyWorkspace({ onCreate }: { onCreate?: () => void }) {
  return (
    <div className="mx-auto flex min-h-[60svh] max-w-lg flex-col items-center justify-center px-5 text-center">
      <span className="flex h-11 w-11 items-center justify-center rounded-card border border-line bg-surface text-accent-ink">
        <Sparkles aria-hidden="true" size={20} />
      </span>
      <h1 className="mt-4 text-xl font-semibold text-ink">No workspaces yet</h1>
      <p className="mt-2 text-sm leading-6 text-ink-muted">
        A workspace is the boundary for documents, context and membership. Create the first one to
        start uploading.
      </p>
      {onCreate === undefined ? null : (
        <button type="button" className="btn btn-primary mt-5" onClick={onCreate}>
          <Plus aria-hidden="true" size={16} />
          New workspace
        </button>
      )}
    </div>
  )
}
