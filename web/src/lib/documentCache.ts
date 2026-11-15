import type { DocumentUpload } from '../types'
import { clearDocxMarkupCache } from './docxPreview'
import { clearSheetMarkupCache } from './sheetPreview'

export interface DocumentFile {
  url: string
  blob: Blob
}

interface CachedDocument extends DocumentFile {
  sizeBytes: number
}

const MAX_ENTRIES = 8
const MAX_TOTAL_BYTES = 64 * 1024 * 1024

const entries = new Map<string, CachedDocument>()
const inFlight = new Map<string, Promise<DocumentFile>>()

function totalBytes(): number {
  let total = 0
  for (const entry of entries.values()) total += entry.sizeBytes
  return total
}

function evictUntilWithinBudget(): void {
  while (entries.size > MAX_ENTRIES || totalBytes() > MAX_TOTAL_BYTES) {
    const oldest = entries.keys().next()
    if (oldest.done === true) return
    const entry = entries.get(oldest.value)
    if (entry !== undefined) URL.revokeObjectURL(entry.url)
    entries.delete(oldest.value)
  }
}

export function cachedDocument(document: DocumentUpload): DocumentFile | null {
  const entry = entries.get(document.sha256)
  if (entry === undefined) return null
  // Touch: re-insert so the most recently used entry is last in insertion order.
  entries.delete(document.sha256)
  entries.set(document.sha256, entry)
  return { url: entry.url, blob: entry.blob }
}

export async function loadDocument(
  document: DocumentUpload,
  fetcher: () => Promise<Blob>,
): Promise<DocumentFile> {
  const cached = cachedDocument(document)
  if (cached !== null) return cached

  const pending = inFlight.get(document.sha256)
  if (pending !== undefined) return pending

  const request = fetcher()
    .then((blob) => {
      const url = URL.createObjectURL(blob)
      entries.set(document.sha256, { url, blob, sizeBytes: blob.size })
      evictUntilWithinBudget()
      return { url, blob }
    })
    .finally(() => {
      inFlight.delete(document.sha256)
    })

  inFlight.set(document.sha256, request)
  return request
}

export function clearDocumentCache(): void {
  for (const entry of entries.values()) URL.revokeObjectURL(entry.url)
  entries.clear()
  inFlight.clear()
  clearDocxMarkupCache()
  clearSheetMarkupCache()
}
