export interface Accent {
  dot: string
  chip: string
  soft: string
  text: string
  border: string
}

const ACCENTS: Accent[] = [
  {
    dot: 'bg-blue',
    chip: 'border-blue/20 bg-blue-soft text-blue',
    soft: 'bg-blue-soft',
    text: 'text-blue',
    border: 'border-blue/25',
  },
  {
    dot: 'bg-teal',
    chip: 'border-teal/20 bg-teal-soft text-teal',
    soft: 'bg-teal-soft',
    text: 'text-teal',
    border: 'border-teal/25',
  },
  {
    dot: 'bg-amber',
    chip: 'border-amber/20 bg-amber-soft text-amber',
    soft: 'bg-amber-soft',
    text: 'text-amber',
    border: 'border-amber/25',
  },
  {
    dot: 'bg-coral',
    chip: 'border-coral/20 bg-coral-soft text-coral',
    soft: 'bg-coral-soft',
    text: 'text-coral',
    border: 'border-coral/25',
  },
  {
    dot: 'bg-violet',
    chip: 'border-violet/20 bg-violet-soft text-violet',
    soft: 'bg-violet-soft',
    text: 'text-violet',
    border: 'border-violet/25',
  },
]

export function workspaceAccent(seed: string): Accent {
  let hash = 0
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash * 31 + seed.charCodeAt(index)) >>> 0
  }
  return ACCENTS[hash % ACCENTS.length]
}

export function initials(name: string): string {
  const normalized = name.trim()
  if (normalized === '') return '··'
  const words = normalized.split(/[\s._-]+/).filter(Boolean)
  if (words.length > 1) {
    return `${words[0][0]}${words[1][0]}`.toUpperCase()
  }
  return normalized.slice(0, 2).toUpperCase()
}

const DOCUMENT_STATUS_LABELS: Record<string, string> = {
  ready: 'Ready',
  processing: 'Processing',
  queued: 'Queued',
  extracting: 'Extracting',
  embedding: 'Embedding',
  deleting: 'Deleting',
  failed: 'Failed',
  cancelled: 'Cancelled',
  unsupported_format: 'Unsupported',
}

export function documentStatusLabel(status: string): string {
  return DOCUMENT_STATUS_LABELS[status] ?? status.replaceAll('_', ' ')
}

const RUN_STATUS_LABELS: Record<string, string> = {
  fetching: 'Fetching',
  describing: 'Describing',
  storing_artifact: 'Storing',
  chunking: 'Chunking',
  syncing: 'Syncing',
  retry_wait: 'Retry wait',
}

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? documentStatusLabel(status)
}

export type PipelineStage = 'scan' | 'extract' | 'chunk' | 'embed' | 'index'

export function pipelineStage(status: string): PipelineStage {
  if (status === 'fetching') return 'scan'
  if (status === 'extracting' || status === 'describing' || status === 'storing_artifact') {
    return 'extract'
  }
  if (status === 'chunking') return 'chunk'
  if (status === 'embedding') return 'embed'
  return 'index'
}

export function formatClock(value: string): string {
  return new Date(value).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export function formatDuration(
  startedAt: string | null,
  completedAt: string | null,
): string | null {
  if (startedAt === null || completedAt === null) return null
  const seconds = Math.max(
    0,
    Math.round((new Date(completedAt).getTime() - new Date(startedAt).getTime()) / 1000),
  )
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`
}

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export function documentStatusTone(status: string): StatusTone {
  if (status === 'ready') return 'success'
  if (status === 'failed' || status === 'cancelled' || status === 'unsupported_format') {
    return 'danger'
  }
  if (status === 'deleting' || status === 'retry_wait') return 'warning'
  if (
    [
      'queued',
      'fetching',
      'extracting',
      'describing',
      'storing_artifact',
      'chunking',
      'embedding',
      'syncing',
      'processing',
    ].includes(status)
  ) {
    return 'info'
  }
  return 'neutral'
}

export const STATUS_TONE_CLASSES: Record<StatusTone, string> = {
  success: 'bg-success-soft text-success',
  warning: 'bg-warn-soft text-warn',
  danger: 'bg-danger-soft text-danger',
  info: 'bg-accent-soft text-accent-ink',
  neutral: 'bg-sunken text-ink-muted',
}

export function formatBytes(size: number): string {
  if (size <= 0) return '0 KB'
  if (size < 1024 * 1024) {
    return `${Math.max(1, Math.ceil(size / 1024))} KB`
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

export function formatShortDate(value: string): string {
  return new Date(value).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

export function documentType(filename: string, mediaType: string): string {
  const extension = filename.split('.').pop()
  if (extension !== undefined && extension !== filename) return extension.toUpperCase()
  if (mediaType.includes('pdf')) return 'PDF'
  if (mediaType.includes('csv')) return 'CSV'
  if (mediaType.includes('wordprocessingml')) return 'DOCX'
  return 'FILE'
}

export function isReadyStatus(status: string): boolean {
  return status === 'ready'
}

export function isActiveStatus(status: string): boolean {
  return ['queued', 'extracting', 'embedding', 'processing'].includes(status)
}

export function isFailedStatus(status: string): boolean {
  return ['failed', 'cancelled', 'unsupported_format'].includes(status)
}
