import type { DocumentUpload } from '../types'
import { clearDocxMarkupCache } from './docxPreview'

/**
 * Bytes and object URLs for documents already fetched in this tab.
 *
 * The key is the document's sha256, not its id: the bytes behind a hash never
 * change, so a cached entry can never show a stale file -- a reindex produces
 * a new hash only if the bytes differ, and the same file re-uploaded to
 * another workspace shares the entry.
 *
 * Both forms are kept because the two viewers need different things: a PDF
 * goes into an iframe by URL, while a DOCX has to be parsed from its bytes.
 * Re-opening a document you have already looked at costs zero requests; the
 * entry survives until it is evicted (an LRU by count and by bytes) or the tab
 * signs out.
 */
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

/** The file for a document already in the cache, or null if it is not. */
export function cachedDocument(document: DocumentUpload): DocumentFile | null {
  const entry = entries.get(document.sha256)
  if (entry === undefined) return null
  // Touch: re-insert so the most recently used entry is last in insertion order.
  entries.delete(document.sha256)
  entries.set(document.sha256, entry)
  return { url: entry.url, blob: entry.blob }
}

/**
 * The file for a document, fetching it at most once.
 *
 * Concurrent callers for the same document share one fetch (an in-flight map),
 * which is what keeps a double click from costing two streams out of the
 * object store, and keeps the preview and the download button from each
 * pulling the same bytes.
 */
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

/** Drop every cached file and every cached render. Called on sign-out. */
export function clearDocumentCache(): void {
  for (const entry of entries.values()) URL.revokeObjectURL(entry.url)
  entries.clear()
  inFlight.clear()
  clearDocxMarkupCache()
}
