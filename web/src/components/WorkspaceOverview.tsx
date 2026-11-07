import type { ReactNode } from 'react'
import {
  ArrowRight,
  CheckCircle2,
  FileText,
  FileUp,
  HardDrive,
  LoaderCircle,
  LockKeyhole,
  Plus,
  Sparkles,
  UploadCloud,
} from 'lucide-react'
import type { ApiResource } from '../hooks/useApiResource'
import type { ProcessingRuns } from '../hooks/useProcessingRuns'
import type { WorkspaceDocumentsData } from '../hooks/useWorkspaceDocuments'
import {
  formatBytes,
  isActiveStatus,
  pipelineStage,
  type PipelineStage,
} from '../lib/visuals'
import type { DocumentUpload, Workspace } from '../types'
import { DocumentTable } from './DocumentTable'
import { Avatar } from './ui/Avatar'
import { RefreshButton } from './ui/RefreshButton'
import { WorkspaceAccessSummary } from './WorkspaceAccessSummary'

const STAGE_ORDER: PipelineStage[] = ['scan', 'extract', 'chunk', 'embed', 'index']

const TERMINAL_STATUSES = new Set(['ready', 'failed', 'cancelled', 'unsupported_format'])

function PipelineStages({ runs }: { runs: ProcessingRuns }) {
  const workspaceRuns = runs.runs.filter(
    (run) => run.job_kind !== 'delete' && !TERMINAL_STATUSES.has(run.status),
  )
  const counts = STAGE_ORDER.map(
    (stage) => workspaceRuns.filter((run) => pipelineStage(run.status) === stage).length,
  )
  const max = Math.max(1, ...counts)
  const liveStage = STAGE_ORDER.find((_, index) => counts[index] > 0)

  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-5" aria-label="Pipeline stages">
      {STAGE_ORDER.map((stage, index) => {
        const live = stage === liveStage
        return (
          <div key={stage} className="min-w-0">
            <p className={`eyebrow ${live ? 'text-accent' : ''}`}>{stage}</p>
            <p
              className={`mt-1 font-mono text-lg font-semibold leading-6 ${live ? 'text-accent' : 'text-ink'}`}
            >
              {counts[index]}
            </p>
            <div className="mt-2 h-[5px] overflow-hidden rounded-stamp border border-line bg-sunken">
              <span
                className={`block h-full ${live ? 'bg-accent' : 'bg-success'}`}
                style={{ width: `${Math.round((counts[index] / max) * 100)}%` }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}

interface WorkspaceOverviewProps {
  workspace: Workspace
  isAdmin: boolean
  documents: ApiResource<WorkspaceDocumentsData>
  runs: ProcessingRuns
  searchQuery: string
  canReindex?: boolean
  reindexingId?: string | null
  onReindex?: (document: DocumentUpload) => void
  canView?: boolean
  onView?: (document: DocumentUpload) => void
  onUpload: () => void
  onViewDocuments: () => void
  onManageMembers: () => void
  onUnauthorized: () => void
  onDeleteDocument: (document: DocumentUpload) => void
}

export function WorkspaceOverview({
  workspace,
  isAdmin,
  documents,
  runs,
  searchQuery,
  canReindex,
  reindexingId,
  onReindex,
  canView,
  onView,
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
            <h1 className="mt-1.5 truncate font-condensed text-xl font-bold uppercase tracking-[0.05em] text-ink">
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

      <section className="mt-5" aria-label="Workspace summary">
        <div className="inline-flex flex-wrap items-center gap-x-5 gap-y-1.5 rounded-card border border-line bg-surface px-4 py-2.5">
          <StatLine
            icon={<FileText aria-hidden="true" size={13} />}
            label="Documents"
            value={restricted ? '—' : String(total)}
          />
          <StatDivider />
          <StatLine
            icon={<CheckCircle2 aria-hidden="true" size={13} />}
            label="Ready"
            value={restricted ? '—' : String(ready)}
          />
          <StatDivider />
          <StatLine
            icon={<LoaderCircle aria-hidden="true" size={13} />}
            label="In pipeline"
            value={restricted ? '—' : String(active)}
          />
          <StatDivider />
          <StatLine
            icon={<HardDrive aria-hidden="true" size={13} />}
            label="Storage"
            value={restricted ? '—' : formatBytes(storage)}
          />
        </div>
      </section>

      {workspace.membership_role === null ? null : (
        <section className="mt-4 rounded-card border border-line bg-surface px-4 py-3.5 sm:px-5">
          <PipelineStages runs={runs} />
        </section>
      )}

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
              canReindex={canReindex}
              reindexingId={reindexingId}
              onReindex={onReindex}
              canView={canView}
              onView={onView}
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
            isAdmin={isAdmin}
            onManageMembers={onManageMembers}
            onUnauthorized={onUnauthorized}
          />
        </aside>
      </div>
    </div>
  )
}

/**
 * One figure in the summary line.
 *
 * The label sits beside the number instead of above it, and the explanatory
 * third line is gone: "Documents 12" and "Storage 9.2 MB" already say what
 * the old captions repeated at four times the height.
 */
function StatLine({
  icon,
  label,
  value,
}: {
  icon: ReactNode
  label: string
  value: string
}) {
  return (
    <p className="flex items-center gap-1.5">
      <span className="text-ink-faint">{icon}</span>
      <span className="text-xs text-ink-faint">{label}</span>
      <span className="font-mono text-sm font-medium text-ink">{value}</span>
    </p>
  )
}

function StatDivider() {
  return <span aria-hidden="true" className="hidden h-3.5 w-px bg-line sm:block" />
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
      className="group rounded-card border border-dashed border-line-strong bg-surface p-5 text-left transition-colors hover:border-accent hover:bg-accent-soft/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      onClick={onUpload}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="eyebrow">Quick upload</p>
          <p className="mt-2 text-sm font-semibold text-ink">Add context to this workspace</p>
        </div>
        <span className="flex h-9 w-9 items-center justify-center rounded-control border border-line bg-sunken text-ink-muted transition-colors group-hover:border-accent group-hover:text-accent">
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
      <span className="flex h-11 w-11 items-center justify-center rounded-card border border-line bg-surface text-ink-muted">
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
