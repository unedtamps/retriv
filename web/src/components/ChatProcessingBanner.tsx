import type { ProcessingRun } from '../types'
import { runStatusLabel } from '../lib/visuals'

const STAGE_PROGRESS: Record<string, number> = {
  queued: 4,
  fetching: 10,
  awaiting_parser: 22,
  extracting: 42,
  describing: 55,
  storing_artifact: 68,
  chunking: 80,
  embedding: 92,
  syncing: 98,
}

/**
 * The active pipeline run, kept visible above the transcript.
 *
 * It is a status strip, not a second progress dashboard: the document, the
 * stage, and the count when the run has one. Embedding is the only stage with
 * a truthful numeric count; the earlier stages get a coarse stage bar.
 */
export function ChatProcessingBanner({ run }: { run: ProcessingRun }) {
  const hasCounts = run.chunk_count > 0
  const percent = hasCounts
    ? Math.min(100, Math.round((run.embedded_chunk_count / run.chunk_count) * 100))
    : (STAGE_PROGRESS[run.status] ?? 8)
  const countLabel = hasCounts
    ? `${run.embedded_chunk_count}/${run.chunk_count}`
    : run.attempts > 1
      ? `attempt ${run.attempts}`
      : ''
  const label = runStatusLabel(run.status).toLowerCase()
  const reference = `DOC-${run.document_id.replaceAll('-', '').slice(-4).toUpperCase()}`

  return (
    <div className="chat-progress-banner" role="status" aria-label="Document processing">
      <span className="chat-doc-chip">{reference}</span>
      <div className="chat-progress-main">
        <p className="chat-progress-title">
          {run.filename ?? 'Document'} · {label}
        </p>
        <div className="chat-progress-meta">
          {run.job_kind === 'delete' ? 'Cleanup' : 'Extraction'}
        </div>
        <div className="chat-progress-track" aria-hidden="true">
          <div className="chat-progress-fill" style={{ width: `${percent}%` }} />
        </div>
      </div>
      {countLabel === '' ? null : <span className="chat-progress-count">{countLabel}</span>}
    </div>
  )
}
