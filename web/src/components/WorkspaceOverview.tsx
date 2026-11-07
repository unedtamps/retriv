import { useMemo, type ReactNode } from 'react'
import { ArrowRight, FileUp, LockKeyhole, Plus, Sparkles } from 'lucide-react'
import { api } from '../api'
import type { ApiResource } from '../hooks/useApiResource'
import { useApiResource } from '../hooks/useApiResource'
import type { WorkspaceDocumentsData } from '../hooks/useWorkspaceDocuments'
import {
  formatBytes,
  formatClock,
  formatDuration,
  formatShortDate,
  runStatusLabel,
} from '../lib/visuals'
import { loadChatHistory } from '../lib/chatHistoryStore'
import type { ProcessingRun, Workspace } from '../types'
import { Avatar } from './ui/Avatar'

interface WorkspaceOverviewProps {
  workspace: Workspace
  documents: ApiResource<WorkspaceDocumentsData>
  onUpload: () => void
  onOpenMembers: () => void
  onOpenChat: () => void
  onUnauthorized: () => void
}

const ACTIVE_BUCKETS: Array<{ match: string[]; stage: 'queued' | 'fetching' | 'extracting' }> = [
  { match: ['queued'], stage: 'queued' },
  { match: ['fetching', 'awaiting_parser', 'retry_wait'], stage: 'fetching' },
  {
    match: ['extracting', 'describing', 'storing_artifact', 'chunking', 'embedding', 'syncing'],
    stage: 'extracting',
  },
]

const TERMINAL = new Set(['ready', 'failed', 'cancelled', 'unsupported_format'])

function runStateChip(status: string): string {
  if (status === 'ready') return 'border-success/50 bg-surface text-success'
  if (TERMINAL.has(status)) return 'border-danger/50 bg-surface text-danger'
  if (status === 'queued') return 'border-line bg-surface text-ink-muted'
  if (status === 'fetching' || status === 'awaiting_parser' || status === 'retry_wait') {
    return 'border-blue/40 bg-surface text-blue'
  }
  return 'border-accent/50 bg-surface text-accent'
}

function runDotClass(status: string): string {
  if (status === 'ready') return 'bg-success'
  if (TERMINAL.has(status)) return 'bg-danger'
  if (status === 'queued') return 'bg-ink-faint'
  if (status === 'fetching' || status === 'awaiting_parser' || status === 'retry_wait') {
    return 'bg-blue'
  }
  return 'bg-accent'
}

function StatPane({
  label,
  value,
  meta,
}: {
  label: string
  value: string
  meta: string
}) {
  return (
    <div className="rounded-card border border-line bg-surface px-4 py-3.5">
      <p className="eyebrow">{label}</p>
      <p className="mt-1.5 font-mono text-[1.375rem] font-semibold leading-7 text-ink">{value}</p>
      <p className="mt-1 truncate text-xs text-ink-muted">{meta}</p>
    </div>
  )
}

function PaneHead({ label, children }: { label: string; children?: ReactNode }) {
  return (
    <div className="flex items-center gap-2.5 border-b border-line px-4 py-2.5">
      <p className="eyebrow">{label}</p>
      <span className="ml-auto">{children}</span>
    </div>
  )
}

function TickBar({ share, tone }: { share: number; tone: 'go' | 'urgent' | 'idle' }) {
  const width = Math.max(0, Math.min(100, Math.round(share * 100)))
  return (
    <div className="relative h-[5px] overflow-hidden rounded-stamp border border-line bg-sunken">
      <span
        className={`block h-full ${tone === 'urgent' ? 'bg-accent' : tone === 'go' ? 'bg-success' : 'bg-transparent'}`}
        style={{ width: `${width}%` }}
      />
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-0"
        style={{
          backgroundImage:
            'repeating-linear-gradient(90deg, transparent 0 11px, var(--color-line) 11px 12px)',
        }}
      />
    </div>
  )
}

export function WorkspaceOverview({
  workspace,
  documents,
  onUpload,
  onOpenMembers,
  onOpenChat,
  onUnauthorized,
}: WorkspaceOverviewProps) {
  const items = documents.data?.items ?? []
  const total = documents.data?.total ?? 0
  const ready = items.filter((document) => document.status === 'ready').length
  const storage = items.reduce((sum, document) => sum + document.size_bytes, 0)
  const restricted = workspace.membership_role === null
  const canManage = workspace.can_manage_members

  const weekNew = items.filter(
    (document) => Date.now() - new Date(document.created_at).getTime() < 7 * 24 * 3600 * 1000,
  ).length

  const runsToday = useApiResource(
    (signal) => api.listProcessingActivity({ today: true, signal }),
    [workspace.id],
    { onUnauthorized },
  )
  const workspaceRuns = useMemo(
    () =>
      (runsToday.data?.items ?? [])
        .filter((run) => run.workspace_id === workspace.id && run.job_kind !== 'delete')
        .sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
    [runsToday.data, workspace.id],
  )
  const bucketOf = (status: string): 'queued' | 'fetching' | 'extracting' | null => {
    for (const bucket of ACTIVE_BUCKETS) {
      if (bucket.match.includes(status)) return bucket.stage
    }
    return null
  }
  const stageCounts = { queued: 0, fetching: 0, extracting: 0 }
  for (const run of workspaceRuns) {
    const stage = bucketOf(run.status)
    if (stage !== null) stageCounts[stage] += 1
  }
  const liveStage =
    (['queued', 'fetching', 'extracting'] as const).find(
      (stage) => stageCounts[stage] > 0,
    ) ?? null
  const queueDepth = stageCounts.queued
  const newestRunAt = workspaceRuns[0]?.created_at ?? null
  const runsMeta = newestRunAt === null
    ? 'no activity'
    : new Date(newestRunAt).toDateString() === new Date().toDateString()
      ? 'today'
      : formatShortDate(newestRunAt)

  const questions = useMemo(() => {
    const history = loadChatHistory()
    return history.conversations
      .filter((session) => session.items.some((item) => item.kind === 'user'))
      .sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1))
      .slice(0, 5)
      .map((session) => {
        const first = session.items.find((item) => item.kind === 'user')
        return {
          id: session.id,
          question: first?.text ?? session.title,
          when:
            new Date(session.updatedAt).toDateString() === new Date().toDateString()
              ? formatClock(session.updatedAt)
              : formatShortDate(session.updatedAt),
        }
      })
  }, [])

  return (
    <div className="mx-auto max-w-[92rem] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
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
          {restricted ? null : (
            <button type="button" className="btn btn-secondary" onClick={onOpenMembers}>
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

      <section className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="Workspace summary">
        <StatPane
          label="Documents"
          value={restricted ? '—' : String(total)}
          meta={restricted ? 'members only' : weekNew > 0 ? `+${weekNew} this week` : 'no new this week'}
        />
        <StatPane
          label="Ready"
          value={restricted ? '—' : String(ready)}
          meta="indexed · searchable"
        />
        <StatPane
          label="Processing"
          value={restricted ? '—' : String(total - ready - items.filter((d) => ['failed', 'cancelled', 'unsupported_format'].includes(d.status)).length)}
          meta={processingMeta(items)}
        />
        <StatPane
          label="Storage"
          value={restricted ? '—' : formatBytes(storage)}
          meta="in this view"
        />
      </section>

      {restricted ? (
        <section className="mt-4 rounded-card border border-dashed border-line-strong bg-surface px-6 py-14 text-center">
          <LockKeyhole aria-hidden="true" className="mx-auto text-ink-faint" size={26} />
          <h2 className="mt-3 text-sm font-semibold text-ink">Documents are members-only</h2>
          <p className="mx-auto mt-1.5 max-w-sm text-sm leading-6 text-ink-muted">
            Ask a workspace owner for viewer, editor or owner access. A platform admin role
            alone does not grant document access.
          </p>
        </section>
      ) : (
        <>
          <section className="mt-3 overflow-hidden rounded-card border border-line bg-surface" aria-label="Pipeline">
            <PaneHead label="Pipeline">
              <span className="font-mono text-[0.65rem] uppercase tracking-[0.08em] text-ink-faint">
                queue depth {queueDepth}
              </span>
            </PaneHead>
            <div className="grid grid-cols-2 gap-x-6 gap-y-4 px-4 py-4 sm:grid-cols-4 sm:px-5">
              <PipelineStage
                label="Queued"
                count={stageCounts.queued}
                share={total > 0 ? stageCounts.queued / total : 0}
                tone={liveStage === 'queued' ? 'urgent' : 'idle'}
              />
              <PipelineStage
                label="Fetching"
                count={stageCounts.fetching}
                share={total > 0 ? stageCounts.fetching / total : 0}
                tone={liveStage === 'fetching' ? 'urgent' : 'idle'}
              />
              <PipelineStage
                label="Extracting"
                count={stageCounts.extracting}
                share={total > 0 ? stageCounts.extracting / total : 0}
                tone={liveStage === 'extracting' ? 'urgent' : 'idle'}
              />
              <PipelineStage label="Ready" count={ready} share={total > 0 ? ready / total : 0} tone="go" />
            </div>
          </section>

          <div className="mt-3 grid gap-3 xl:grid-cols-2 xl:items-start">
            <section className="overflow-hidden rounded-card border border-line bg-surface" aria-label="Recent runs">
              <PaneHead label="Recent runs">
                <span className="font-mono text-[0.65rem] uppercase tracking-[0.08em] text-ink-faint">
                  {runsMeta}
                </span>
              </PaneHead>
              {workspaceRuns.length === 0 ? (
                <p className="px-4 py-8 text-center text-sm leading-6 text-ink-muted">
                  No runs yet. Upload a document and the pipeline will report here.
                </p>
              ) : (
                <div className="max-h-72 overflow-y-auto">
                  <table className="table">
                    <thead>
                      <tr>
                        <th scope="col">Run</th>
                        <th scope="col">Document</th>
                        <th scope="col">State</th>
                        <th scope="col">Took</th>
                      </tr>
                    </thead>
                    <tbody>
                      {workspaceRuns.map((run: ProcessingRun) => (
                        <tr key={run.id}>
                          <td>
                            <span className="flex items-center gap-2">
                              <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 rounded-full ${runDotClass(run.status)}`} />
                              <span className="font-mono text-[0.7rem] text-ink-muted">
                                run-{run.id.slice(0, 4)}
                              </span>
                            </span>
                          </td>
                          <td className="max-w-[14rem]">
                            <span className="block truncate text-sm font-medium text-ink">
                              {run.filename ?? `Document ${run.document_id.slice(0, 8)}`}
                            </span>
                          </td>
                          <td>
                            <span className={`stamp whitespace-nowrap ${runStateChip(run.status)}`}>
                              {runStatusLabel(run.status)}
                            </span>
                          </td>
                          <td className="font-mono text-meta text-ink-muted">
                            {formatDuration(run.started_at, run.completed_at) ?? '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="overflow-hidden rounded-card border border-line bg-surface" aria-label="Recently answered">
              <PaneHead label="Recently answered">
                <button
                  type="button"
                  className="inline-flex items-center gap-1 font-condensed text-[0.6875rem] font-bold uppercase tracking-[0.09em] text-ink-muted transition-colors hover:text-accent"
                  onClick={onOpenChat}
                >
                  Open chat
                  <ArrowRight aria-hidden="true" size={12} />
                </button>
              </PaneHead>
              {questions.length === 0 ? (
                <p className="px-4 py-8 text-center text-sm leading-6 text-ink-muted">
                  No questions yet. Answers you get in the chat are listed here.
                </p>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Question</th>
                      <th scope="col">When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {questions.map((entry) => (
                      <tr key={entry.id}>
                        <td className="max-w-[22rem]">
                          <span className="block truncate text-sm font-medium text-ink">
                            {entry.question}
                          </span>
                        </td>
                        <td className="whitespace-nowrap font-mono text-meta text-ink-muted">
                          {entry.when}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <p className="border-t border-line px-4 py-2 font-mono text-[0.65rem] text-ink-faint">
                kept in this browser — the server stores no questions
              </p>
            </section>
          </div>
        </>
      )}
    </div>
  )
}

function PipelineStage({
  label,
  count,
  share,
  tone,
}: {
  label: string
  count: number
  share: number
  tone: 'go' | 'urgent' | 'idle'
}) {
  return (
    <div className="min-w-0">
      <p className={`eyebrow ${tone === 'urgent' ? 'text-accent' : ''}`}>{label}</p>
      <p
        className={`mt-1 font-mono text-lg font-semibold leading-6 ${tone === 'urgent' ? 'text-accent' : 'text-ink'}`}
      >
        {count}
      </p>
      <div className="mt-2">
        <TickBar share={share} tone={tone} />
      </div>
    </div>
  )
}

function processingMeta(items: Array<{ status: string }>): string {
  const extracting = items.filter((d) =>
    ['extracting', 'describing', 'storing_artifact', 'chunking', 'embedding', 'syncing'].includes(d.status),
  ).length
  const queued = items.filter((d) => d.status === 'queued').length
  const fetching = items.filter((d) => ['fetching', 'awaiting_parser', 'processing'].includes(d.status)).length
  const parts: string[] = []
  if (extracting > 0) parts.push(`${extracting} extracting`)
  if (fetching > 0) parts.push(`${fetching} fetching`)
  if (queued > 0) parts.push(`${queued} queued`)
  return parts.length > 0 ? parts.join(' · ') : 'idle'
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
