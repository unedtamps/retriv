import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Activity } from 'lucide-react'
import type { ProcessingRuns } from '../hooks/useProcessingRuns'
import {
  documentStatusTone,
  formatClock,
  formatDuration,
  pipelineStage,
  runStatusLabel,
  STATUS_TONE_CLASSES,
} from '../lib/visuals'

const PIPELINE_STAGES: Array<{ id: 'scan' | 'extract' | 'chunk' | 'embed' | 'index'; label: string }> = [
  { id: 'scan', label: 'Scan' },
  { id: 'extract', label: 'Extract' },
  { id: 'chunk', label: 'Chunk' },
  { id: 'embed', label: 'Embed' },
  { id: 'index', label: 'Index' },
]

interface PipelinePopoverProps {
  runs: ProcessingRuns
  workspaceId: string | null
}

/**
 * The pipeline status beside the bell: a quiet icon that opens a small
 * popover with the stage flow and the recent runs of the selected workspace.
 */
export function PipelinePopover({ runs, workspaceId }: PipelinePopoverProps) {
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  function changeOpen(next: boolean) {
    if (next) {
      const rect = buttonRef.current?.getBoundingClientRect()
      if (rect) setAnchor({ top: rect.bottom + 8, right: window.innerWidth - rect.right })
    }
    setOpen(next)
  }

  useEffect(() => {
    if (!open) return
    panelRef.current?.focus()

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    function closeOnPointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (panelRef.current?.contains(target) || buttonRef.current?.contains(target)) return
      setOpen(false)
    }

    window.addEventListener('keydown', closeOnEscape)
    document.addEventListener('pointerdown', closeOnPointerDown)
    return () => {
      window.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('pointerdown', closeOnPointerDown)
    }
  }, [open])

  const workspaceRuns = runs.runs.filter(
    (run) =>
      run.job_kind !== 'access' && (workspaceId === null || run.workspace_id === workspaceId),
  )
  const stageCounts = new Map(PIPELINE_STAGES.map((stage) => [stage.id, 0]))
  for (const run of workspaceRuns) {
    if (run.status === 'retry_wait') continue
    stageCounts.set(pipelineStage(run.status), (stageCounts.get(pipelineStage(run.status)) ?? 0) + 1)
  }
  const activeStage = workspaceRuns.length > 0 ? pipelineStage(workspaceRuns[0].status) : null
  const running = workspaceRuns.length > 0
  const displayRuns = workspaceRuns.slice(0, 5)

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={`inline-flex h-9 w-9 items-center justify-center rounded-control transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink ${
          running
            ? 'bg-accent-soft text-accent-ink hover:bg-accent-soft/70'
            : 'text-ink-muted hover:bg-sunken hover:text-ink'
        }`}
        aria-label="Pipeline status"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => changeOpen(!open)}
      >
        <Activity aria-hidden="true" size={17} />
      </button>
      {open
        ? createPortal(
            <div
              ref={panelRef}
              tabIndex={-1}
              role="dialog"
              aria-label="Pipeline status"
              style={anchor ?? undefined}
              className={`popover-enter fixed z-[60] w-80 rounded-card border border-line bg-surface p-4 shadow-raised outline-none`}
            >
              <div className="flex items-center justify-between gap-2">
                <p className="text-[0.9375rem] font-medium text-ink">Pipeline</p>
                <span className={`stamp ${running ? 'bg-accent text-white' : ''}`}>
                  {running ? 'Running' : 'Idle'}
                </span>
              </div>
              <p className="mt-0.5 text-xs text-ink-muted">
                {running
                  ? `${workspaceRuns.length} ${workspaceRuns.length === 1 ? 'document' : 'documents'} in indexing`
                  : 'Nothing is being processed'}
              </p>

              <div className="mt-4 flex items-center" aria-hidden="true">
                {PIPELINE_STAGES.map((stage, index) => (
                  <div key={stage.id} className="flex min-w-0 flex-1 items-center">
                    {index > 0 ? <span className="h-px w-1.5 shrink-0 bg-line-strong" /> : null}
                    <div
                      className={`min-w-0 flex-1 rounded-control px-1 py-2 text-center ${
                        activeStage === stage.id ? 'bg-accent-soft' : ''
                      }`}
                    >
                      <p
                        className={`truncate text-[0.6875rem] leading-4 ${
                          activeStage === stage.id ? 'text-accent-ink' : 'text-ink-muted'
                        }`}
                      >
                        {stage.label}
                      </p>
                      <p
                        className={`mt-0.5 text-lg font-medium leading-6 ${
                          activeStage === stage.id ? 'text-accent-ink' : 'text-ink'
                        }`}
                      >
                        {stageCounts.get(stage.id) ?? 0}
                      </p>
                    </div>
                  </div>
                ))}
              </div>

              <div className="mt-3 border-t border-line pt-1">
                <p className="py-2 text-right font-mono text-[0.6rem] text-ink-faint">
                  {workspaceRuns.length} in progress
                </p>
                {runs.error === null ? null : (
                  <p className="pb-2 text-xs leading-5 text-danger" role="alert">
                    {runs.error}
                  </p>
                )}
                {workspaceRuns.length === 0 && !runs.loading ? (
                  <p className="py-2 text-xs leading-5 text-ink-muted">
                    Nothing in flight right now.
                  </p>
                ) : (
                  displayRuns.map((run, index) => {
                    const tone = documentStatusTone(run.status)
                    const duration = formatDuration(run.started_at, run.completed_at)
                    return (
                      <div
                        key={run.id}
                        className={`flex h-10 items-center gap-3 text-xs ${
                          index < displayRuns.length - 1 ? 'border-b border-line' : ''
                        }`}
                      >
                        <span className="min-w-0 flex-1 truncate text-[0.8125rem] text-ink">
                          {run.filename ?? 'Unknown document'}
                        </span>
                        <span className={`stamp shrink-0 ${STATUS_TONE_CLASSES[tone]}`}>
                          {runStatusLabel(run.status)}
                        </span>
                        <span className="hidden w-12 shrink-0 text-right font-mono text-[0.65rem] text-ink-faint sm:block">
                          {run.started_at === null ? '—' : formatClock(run.started_at)}
                        </span>
                        <span className="w-14 shrink-0 text-right font-mono text-[0.65rem] text-ink-faint">
                          {duration ?? '—'}
                        </span>
                      </div>
                    )
                  })
                )}
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
