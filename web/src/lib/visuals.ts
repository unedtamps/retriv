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

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export function documentStatusTone(status: string): StatusTone {
  if (status === 'ready') return 'success'
  if (status === 'failed' || status === 'cancelled' || status === 'unsupported_format') {
    return 'danger'
  }
  if (status === 'deleting') return 'warning'
  if (status === 'queued' || status === 'extracting' || status === 'embedding' || status === 'processing') {
    return 'info'
  }
  return 'neutral'
}

export const STATUS_TONE_CLASSES: Record<StatusTone, string> = {
  success: 'border-success/20 bg-success-soft text-success',
  warning: 'border-warn/20 bg-warn-soft text-warn',
  danger: 'border-danger/20 bg-danger-soft text-danger',
  info: 'border-blue/20 bg-blue-soft text-blue',
  neutral: 'border-line-strong bg-sunken text-ink-muted',
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
