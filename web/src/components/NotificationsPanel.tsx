import { useEffect, useRef } from 'react'
import { CircleAlert, FileText, LoaderCircle, RefreshCw, X } from 'lucide-react'
import type { ProcessingRun, Workspace } from '../types'

interface NotificationsPanelProps {
  activeRun: ProcessingRun | null
  recentRuns: ProcessingRun[]
  loading: boolean
  error: string | null
  workspaces: Workspace[]
  onRefresh: () => void
  onClose: () => void
}

const ACTIVE_STATUSES = new Set([
  'queued',
  'fetching',
  'extracting',
  'storing_artifact',
  'chunking',
  'embedding',
  'deleting',
  'retry_wait',
])

/**
 * The panel behind the bell: what is running now, and what ran recently.
 *
 * It was written inside the bell button's file, which made that file the
 * button, the panel, the fetch and the focus management at once.
 */
export function NotificationsPanel({
  activeRun,
  recentRuns,
  loading,
  error,
  workspaces,
  onRefresh,
  onClose,
}: NotificationsPanelProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeButtonRef.current?.focus()

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }

    window.addEventListener('keydown', closeOnEscape)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', closeOnEscape)
    }
  }, [onClose])

  const recentWithoutPinnedActive = recentRuns.filter((run) => run.id !== activeRun?.id)

  return (
    <div className="fixed inset-0 z-[60]">
      <button
        type="button"
        className="absolute inset-0 cursor-pointer bg-ink/40 backdrop-blur-[1px]"
        aria-label="Close processing notifications"
        onClick={onClose}
      />
      <aside
        id="processing-notifications-panel"
        role="dialog"
        aria-label="Processing notifications"
        aria-modal="true"
        className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col border-l border-line bg-paper shadow-raised"
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-line bg-surface px-5 py-4">
          <div>
            <p className="eyebrow">Last hour · refresh to update</p>
            <h2 className="mt-1 text-lg font-semibold tracking-[-0.015em] text-ink">
              Processing activity
            </h2>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              className="btn-icon"
              aria-label="Refresh processing activity"
              disabled={loading}
              onClick={onRefresh}
            >
              <RefreshCw
                aria-hidden="true"
                className={loading ? 'animate-spin' : undefined}
                size={18}
              />
            </button>
            <button
              ref={closeButtonRef}
              type="button"
              className="btn-icon"
              aria-label="Close processing notifications panel"
              onClick={onClose}
            >
              <X aria-hidden="true" size={18} />
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain p-4">
          {error === null ? null : (
            <p className="error-note" role="alert">
              <CircleAlert aria-hidden="true" className="mt-0.5 shrink-0" size={16} />
              {error}
            </p>
          )}
          {loading && activeRun === null && recentRuns.length === 0 ? (
            <div className="flex items-center gap-2 text-sm text-ink-muted" role="status">
              <LoaderCircle className="animate-spin" size={17} />Loading processing activity…
            </div>
          ) : null}

          <section aria-labelledby="active-processing-title">
            <h3 id="active-processing-title" className="eyebrow">
              Currently processing
            </h3>
            {activeRun === null ? (
              <p className="mt-2 rounded-card border border-line bg-surface px-4 py-3 text-sm text-ink-muted">
                No unfinished process across your workspaces.
              </p>
            ) : (
              <ProcessingRunCard run={activeRun} workspaces={workspaces} highlighted />
            )}
          </section>

          <section aria-labelledby="recent-processing-title">
            <div className="flex items-center justify-between gap-3">
              <h3 id="recent-processing-title" className="eyebrow">
                Recent activity
              </h3>
              <span className="font-mono text-meta text-ink-faint">
                {recentRuns.length} process{recentRuns.length === 1 ? '' : 'es'}
              </span>
            </div>
            {recentWithoutPinnedActive.length === 0 ? (
              <p className="mt-2 rounded-card border border-line bg-surface px-4 py-3 text-sm text-ink-muted">
                No other processes in the last hour.
              </p>
            ) : (
              <ul className="mt-2 space-y-2">
                {recentWithoutPinnedActive.map((run) => (
                  <li key={run.id}>
                    <ProcessingRunCard run={run} workspaces={workspaces} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </aside>
    </div>
  )
}

function ProcessingRunCard({
  run,
  workspaces,
  highlighted = false,
}: {
  run: ProcessingRun
  workspaces: Workspace[]
  highlighted?: boolean
}) {
  const workspaceName =
    workspaces.find((workspace) => workspace.id === run.workspace_id)?.name ?? 'Workspace'
  const filename = run.filename ?? `Document ${run.document_id.slice(0, 8)}`
  const progress =
    run.chunk_count > 0
      ? `${run.embedded_chunk_count}/${run.chunk_count} embeddings`
      : run.attempts > 1
        ? `Attempt ${run.attempts}`
        : null

  return (
    <article
      className={
        highlighted
          ? 'mt-2 rounded-card border border-accent-ink/30 bg-accent-soft p-4'
          : 'rounded-card border border-line bg-surface p-4'
      }
    >
      <div className="flex items-start gap-3">
        <FileText className="mt-0.5 shrink-0 text-accent-ink" aria-hidden="true" size={17} />
        <div className="min-w-0 flex-1">
          <p className="break-words text-sm font-medium text-ink">{filename}</p>
          <p className="mt-1 text-xs text-ink-muted">
            {workspaceName} ·{' '}
            {run.job_kind === 'delete' ? 'Document cleanup' : 'Document extraction'}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className={statusClass(run.status)}>{statusLabel(run.status)}</span>
            {progress === null ? null : (
              <span className="font-mono text-meta text-ink-faint">{progress}</span>
            )}
          </div>
          <time
            className="mt-2 block font-mono text-meta text-ink-faint"
            dateTime={run.created_at}
          >
            {new Date(run.created_at).toLocaleString()}
          </time>
        </div>
      </div>
    </article>
  )
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: 'Queued',
    fetching: 'Fetching',
    extracting: 'Extracting',
    storing_artifact: 'Saving artifact',
    chunking: 'Chunking',
    embedding: 'Embedding',
    deleting: 'Deleting',
    retry_wait: 'Waiting to retry',
    ready: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    unsupported_format: 'Unsupported format',
  }
  return labels[status] ?? status.replaceAll('_', ' ')
}

function statusClass(status: string): string {
  if (ACTIVE_STATUSES.has(status)) {
    return 'inline-flex items-center rounded-full border border-accent-ink/30 bg-accent-soft px-2 py-0.5 text-xs font-medium text-accent-ink'
  }
  if (status === 'ready') {
    return 'inline-flex items-center rounded-full border border-success/30 bg-success-soft px-2 py-0.5 text-xs font-medium text-success'
  }
  if (status === 'failed' || status === 'unsupported_format') {
    return 'inline-flex items-center rounded-full border border-danger/30 bg-danger-soft px-2 py-0.5 text-xs font-medium text-danger'
  }
  return 'inline-flex items-center rounded-full border border-line bg-sunken px-2 py-0.5 text-xs font-medium text-ink-muted'
}
