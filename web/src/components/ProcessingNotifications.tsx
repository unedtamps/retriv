import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import {
  Bell,
  CircleAlert,
  FileText,
  LoaderCircle,
  RefreshCw,
  X,
} from 'lucide-react'
import { api, getErrorMessage, isUnauthorized } from '../api'
import type { ProcessingRun, Workspace } from '../types'

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

interface ProcessingNotificationsProps {
  token: string
  workspaces: Workspace[]
  onUnauthorized: () => void
  onOpenChange: (open: boolean) => void
}

export function ProcessingNotifications({
  token,
  workspaces,
  onUnauthorized,
  onOpenChange,
}: ProcessingNotificationsProps) {
  const [open, setOpen] = useState(false)
  const [activeRun, setActiveRun] = useState<ProcessingRun | null>(null)
  const [recentRuns, setRecentRuns] = useState<ProcessingRun[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Bumped by the Refresh button to re-run the load. There is no polling
  // timer: an idle page used to re-request both endpoints every ten
  // seconds forever, which is a request per user per ten seconds for
  // data that usually has not changed. Fetch once on mount so the bell
  // badge has a count, then only when someone asks for it again.
  const [refreshKey, setRefreshKey] = useState(0)
  const bellButtonRef = useRef<HTMLButtonElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()

    async function load(): Promise<void> {
      try {
        const [activeResponse, recentResponse] = await Promise.all([
          api.getActiveProcessingRun(token, controller.signal),
          api.listRecentProcessingRuns(token, controller.signal),
        ])
        if (!cancelled) {
          setActiveRun(activeResponse.item)
          setRecentRuns(recentResponse.items)
          setError(null)
        }
      } catch (caught) {
        if (cancelled || (caught instanceof Error && caught.name === 'AbortError')) return
        if (isUnauthorized(caught)) {
          onUnauthorized()
        } else {
          setError(getErrorMessage(caught))
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void load()
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [token, refreshKey, onUnauthorized])

  useEffect(() => {
    if (!open) return

    const previousOverflow = document.body.style.overflow
    const bellButton = bellButtonRef.current
    document.body.style.overflow = 'hidden'
    closeButtonRef.current?.focus()

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setOpen(false)
        onOpenChange(false)
      }
    }

    window.addEventListener('keydown', closeOnEscape)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', closeOnEscape)
      window.requestAnimationFrame(() => bellButton?.focus())
    }
  }, [open, onOpenChange])

  function changeOpen(nextOpen: boolean) {
    setOpen(nextOpen)
    onOpenChange(nextOpen)
  }

  const notificationCount = new Set([
    ...recentRuns.map((run) => run.id),
    ...(activeRun === null ? [] : [activeRun.id]),
  ]).size
  const recentWithoutPinnedActive = recentRuns.filter((run) => run.id !== activeRun?.id)

  return (
    <>
      <button
        ref={bellButtonRef}
        type="button"
        className="icon-button relative"
        aria-label={notificationCount === 0
          ? 'Open processing notifications'
          : `Open processing notifications, ${notificationCount} recent runs`}
        aria-controls="processing-notifications-panel"
        aria-expanded={open}
        onClick={() => changeOpen(true)}
      >
        <Bell aria-hidden="true" size={18} />
        {notificationCount === 0 ? null : (
          <span
            aria-hidden="true"
            className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-emerald-800 px-1 font-mono text-[0.58rem] leading-none text-white"
          >
            {notificationCount > 99 ? '99+' : notificationCount}
          </span>
        )}
      </button>
      {open ? createPortal(
        <div className="fixed inset-0 z-[60]">
          <button
            type="button"
            className="absolute inset-0 cursor-pointer bg-[#17231f]/45 backdrop-blur-[1px]"
            aria-label="Close processing notifications"
            onClick={() => changeOpen(false)}
          />
          <aside
            id="processing-notifications-panel"
            role="dialog"
            aria-label="Processing notifications"
            aria-modal="true"
            className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col border-l border-stone-300 bg-[#f4f1e8] shadow-[-24px_0_80px_rgba(0,0,0,0.2)]"
          >
            <header className="flex shrink-0 items-start justify-between gap-4 border-b border-stone-300 bg-white px-5 py-5 sm:px-6">
              <div>
                <p className="section-eyebrow">Last hour · refresh to update</p>
                <h2 className="mt-2 text-xl font-semibold tracking-[-0.03em] text-stone-950">
                  Processing activity
                </h2>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  className="icon-button"
                  aria-label="Refresh processing activity"
                  disabled={loading}
                  onClick={() => {
                    setLoading(true)
                    setRefreshKey((value) => value + 1)
                  }}
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
                  className="icon-button"
                  aria-label="Close processing notifications panel"
                  onClick={() => changeOpen(false)}
                >
                  <X aria-hidden="true" size={18} />
                </button>
              </div>
            </header>

            <div className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain p-4 sm:p-5">
              {error === null ? null : (
                <p className="form-error flex items-start gap-2" role="alert">
                  <CircleAlert aria-hidden="true" className="mt-0.5 shrink-0" size={16} />
                  {error}
                </p>
              )}
              {loading && activeRun === null && recentRuns.length === 0 ? (
                <div className="flex items-center gap-2 text-sm text-stone-500" role="status">
                  <LoaderCircle className="animate-spin" size={17} />Loading processing activity…
                </div>
              ) : null}

              <section aria-labelledby="active-processing-title">
                <h3 id="active-processing-title" className="section-eyebrow">Currently processing</h3>
                {activeRun === null ? (
                  <p className="mt-2 border border-stone-200 bg-white px-4 py-3 text-sm text-stone-500">
                    No unfinished process across your workspaces.
                  </p>
                ) : (
                  <ProcessingRunCard
                    run={activeRun}
                    workspaces={workspaces}
                    highlighted
                  />
                )}
              </section>

              <section aria-labelledby="recent-processing-title">
                <div className="flex items-center justify-between gap-3">
                  <h3 id="recent-processing-title" className="section-eyebrow">Recent activity</h3>
                  <span className="font-mono text-[0.62rem] text-stone-500">
                    {recentRuns.length} process{recentRuns.length === 1 ? '' : 'es'}
                  </span>
                </div>
                {recentWithoutPinnedActive.length === 0 ? (
                  <p className="mt-2 border border-stone-200 bg-white px-4 py-3 text-sm text-stone-500">
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
        </div>,
        document.body,
      ) : null}
    </>
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
  const workspaceName = workspaces.find((workspace) => workspace.id === run.workspace_id)?.name
    ?? 'Workspace'
  const filename = run.filename ?? `Document ${run.document_id.slice(0, 8)}`
  const progress = run.chunk_count > 0
    ? `${run.embedded_chunk_count}/${run.chunk_count} embeddings`
    : run.attempts > 1
      ? `Attempt ${run.attempts}`
      : null

  return (
    <article className={highlighted
      ? 'mt-2 border border-emerald-800/30 bg-emerald-50 p-4'
      : 'border border-stone-200 bg-white p-4'}>
      <div className="flex items-start gap-3">
        <FileText className="mt-0.5 shrink-0 text-emerald-900" aria-hidden="true" size={17} />
        <div className="min-w-0 flex-1">
          <p className="break-words text-sm font-medium text-stone-900">{filename}</p>
          <p className="mt-1 text-xs text-stone-500">
            {workspaceName} · {run.job_kind === 'delete' ? 'Document cleanup' : 'Document extraction'}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className={statusClass(run.status)}>{statusLabel(run.status)}</span>
            {progress === null ? null : <span className="font-mono text-[0.62rem] text-stone-500">{progress}</span>}
          </div>
          <time className="mt-2 block font-mono text-[0.62rem] text-stone-500" dateTime={run.created_at}>
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
    return 'inline-flex border border-blue-200 bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-900'
  }
  if (status === 'ready') {
    return 'inline-flex border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-900'
  }
  if (status === 'failed' || status === 'unsupported_format') {
    return 'inline-flex border border-red-200 bg-red-50 px-2 py-0.5 text-xs font-medium text-red-900'
  }
  return 'inline-flex border border-stone-200 bg-stone-50 px-2 py-0.5 text-xs font-medium text-stone-700'
}
