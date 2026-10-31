import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Bell, FileText, LoaderCircle } from 'lucide-react'
import type { ProcessingRun, Workspace } from '../types'
import type { ProcessingRuns } from '../hooks/useProcessingRuns'
import { documentStatusTone, runStatusLabel, STATUS_TONE_CLASSES } from '../lib/visuals'
import { RefreshButton } from './ui/RefreshButton'

interface ProcessingNotificationsProps {
  workspaces: Workspace[]
  runs: ProcessingRuns
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The bell, and the floating panel it opens.
 *
 * The panel is a small popover anchored under the bell, not a drawer: the
 * feed only ever holds what is running right now, so a whole column of the
 * screen was never its proportion. The open state is owned by the shell so
 * this panel and the pipeline popover are mutually exclusive — opening one
 * closes the other on the same click. The runs state is owned by the
 * dashboard and passed in, so both show one shared picture.
 */
export function ProcessingNotifications({
  workspaces,
  runs,
  open,
  onOpenChange,
}: ProcessingNotificationsProps) {
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null)
  const bellButtonRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  function changeOpen(next: boolean) {
    if (next) {
      const rect = bellButtonRef.current?.getBoundingClientRect()
      if (rect) setAnchor({ top: rect.bottom + 8, right: window.innerWidth - rect.right })
    }
    onOpenChange(next)
  }

  useEffect(() => {
    if (!open) return
    panelRef.current?.focus()

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') onOpenChange(false)
    }
    function closeOnPointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (panelRef.current?.contains(target) || bellButtonRef.current?.contains(target)) return
      onOpenChange(false)
    }

    window.addEventListener('keydown', closeOnEscape)
    document.addEventListener('pointerdown', closeOnPointerDown)
    return () => {
      window.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('pointerdown', closeOnPointerDown)
    }
  }, [open, onOpenChange])

  const count = runs.runs.length

  return (
    <>
      <button
        ref={bellButtonRef}
        type="button"
        className="relative inline-flex h-9 w-9 items-center justify-center rounded-control text-ink-muted transition-colors hover:bg-sunken hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink"
        aria-label={
          count === 0
            ? 'Open processing notifications'
            : `Open processing notifications, ${count} active ${count === 1 ? 'run' : 'runs'}`
        }
        aria-controls="processing-activity-panel"
        aria-expanded={open}
        onClick={() => changeOpen(!open)}
      >
        <Bell aria-hidden="true" size={17} />
        {count === 0 ? null : (
          <span
            aria-hidden="true"
            className="absolute -right-0.5 -top-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-accent px-1 font-mono text-[0.55rem] leading-none text-white"
          >
            {count > 99 ? '99+' : count}
          </span>
        )}
      </button>
      {open
        ? createPortal(
            <div
              ref={panelRef}
              tabIndex={-1}
              role="dialog"
              aria-label="Processing activity"
              style={anchor ?? undefined}
              className={`popover-enter fixed z-[60] w-96 rounded-card border border-line bg-surface shadow-raised outline-none`}
            >
              <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
                <div className="min-w-0">
                  <p className="text-[0.9375rem] font-medium text-ink">Processing activity</p>
                  <p className="mt-0.5 text-xs text-ink-muted">
                    {count === 0
                      ? 'Nothing is running right now'
                      : `${count} in flight across your workspaces`}
                  </p>
                </div>
                <RefreshButton
                  label="Refresh processing activity"
                  loading={runs.loading}
                  onRefresh={runs.reload}
                />              </div>

              <div className="max-h-96 space-y-2 overflow-y-auto overscroll-contain p-3">
                {runs.error === null ? null : (
                  <p className="error-note" role="alert">
                    {runs.error}
                  </p>
                )}
                {runs.loading && count === 0 ? (
                  <div
                    className="flex items-center gap-2 px-1 py-2 text-sm text-ink-muted"
                    role="status"
                  >
                    <LoaderCircle className="animate-spin" size={17} />
                    Loading processing activity…
                  </div>
                ) : count === 0 ? (
                  <p className="rounded-card border border-line bg-paper px-4 py-3 text-sm text-ink-muted">
                    No unfinished processes across your workspaces.
                  </p>
                ) : (
                  runs.runs.map((run) => (
                    <ProcessingRunCard key={run.id} run={run} workspaces={workspaces} />
                  ))
                )}
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}

function ProcessingRunCard({
  run,
  workspaces,
}: {
  run: ProcessingRun
  workspaces: Workspace[]
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
    <article className="rounded-card border border-line bg-surface p-3.5">
      <div className="flex items-start gap-3">
        <FileText className="mt-0.5 shrink-0 text-accent-ink" aria-hidden="true" size={17} />
        <div className="min-w-0 flex-1">
          <p className="break-words text-sm font-medium text-ink">{filename}</p>
          <p className="mt-1 text-xs text-ink-muted">
            {workspaceName} ·{' '}
            {run.job_kind === 'delete' ? 'Document cleanup' : 'Document extraction'}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className={`stamp ${STATUS_TONE_CLASSES[documentStatusTone(run.status)]}`}>
              {runStatusLabel(run.status)}
            </span>
            {progress === null ? null : (
              <span className="font-mono text-meta text-ink-faint">{progress}</span>
            )}
          </div>
          <time
            className="mt-1.5 block font-mono text-meta text-ink-faint"
            dateTime={run.created_at}
          >
            {new Date(run.created_at).toLocaleTimeString()}
          </time>
        </div>
      </div>
    </article>
  )
}
